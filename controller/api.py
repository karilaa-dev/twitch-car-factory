"""JSON API for the staff-only Twitch farm control room.

The API deliberately delegates writes to the existing forms and service layer.
This keeps the worker protocol, audit behavior, encrypted credentials, and
validation rules identical while the browser presentation is React-owned.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import wraps
import json
import logging
from typing import Any, Callable
import uuid

from django.contrib.auth import login, logout
from django.core import signing
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import transaction
from django.db.models import Prefetch, Q
from django.http import Http404, HttpRequest, JsonResponse, QueryDict, StreamingHttpResponse
from django.middleware.csrf import get_token
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.debug import sensitive_post_parameters
from django.views.csrf import csrf_failure as django_csrf_failure

from .forms import (
    AccountChannelSelectionForm,
    AccountCreateForm,
    AccountEditForm,
    FarmConfigurationForm,
    LegacyImportConfirmForm,
    LegacyImportUploadForm,
    PresetAssignmentForm,
    PresetForm,
    StaffAuthenticationForm,
)
from .models import (
    AccountCredential,
    AccountChannelSelection,
    ActionLog,
    ChannelPreset,
    FarmConfiguration,
    LegacyImportDraft,
    MinerAccount,
    MinerCommand,
    MinerIncident,
    MinerInstanceState,
    MinerRun,
    WorkerLease,
)
from .runtime_logs import (
    ACCOUNT_LOG_ARCHIVE_BYTES,
    MAX_LOG_BYTES,
    MAX_LOG_LINES,
    LogStorageError,
    iter_run_gzip,
    read_account_live,
    read_combined_live,
    read_run_log_page,
    summarize_run_log,
)
from .services import (
    create_account,
    delete_preset,
    enqueue_all,
    enqueue_command,
    normalize_channels,
    request_tv_authentication,
    save_preset,
    set_account_channel_selection,
    update_account,
    update_farm_configuration,
)
from .twitch_lookup import TwitchLookupStatus, lookup_twitch_names


logger = logging.getLogger(__name__)
ApiView = Callable[..., JsonResponse]
LOG_HISTORY_CURSOR_SALT = "twitch-farm.log-history.v1"


def _success(data: Any, *, notices: list[dict[str, str]] | None = None, status: int = 200):
    return JsonResponse(
        {"data": data, "notices": notices or []},
        status=status,
        json_dumps_params={"separators": (",", ":")},
    )


def _error(
    code: str,
    message: str,
    *,
    fields: dict[str, list[str]] | None = None,
    status: int = 400,
):
    return JsonResponse(
        {"error": {"code": code, "message": message, "fields": fields or {}}},
        status=status,
        json_dumps_params={"separators": (",", ":")},
    )


def _validation_error(exc: ValidationError):
    if hasattr(exc, "message_dict"):
        fields = {key: list(values) for key, values in exc.message_dict.items()}
    else:
        fields = {"__all__": list(exc.messages)}
    return _error(
        "validation_error",
        "The submitted data is invalid.",
        fields=fields,
        status=400,
    )


def _form_error(form, *, aliases: Mapping[str, str] | None = None):
    aliases = aliases or {}
    fields = {
        ("__all__" if key == "__all__" else aliases.get(key, key)): [
            item["message"] for item in values
        ]
        for key, values in form.errors.get_json_data(escape_html=False).items()
    }
    return _error(
        "validation_error",
        "The submitted data is invalid.",
        fields=fields,
        status=400,
    )


def _parse_json(request: HttpRequest) -> dict[str, Any]:
    try:
        value = json.loads(request.body or b"{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("Request body must be valid JSON.") from exc
    if not isinstance(value, dict):
        raise ValidationError("Request body must be a JSON object.")
    return value


def _form_data(payload: dict[str, Any], *, list_fields: tuple[str, ...] = ()) -> QueryDict:
    data = QueryDict(mutable=True)
    for key, value in payload.items():
        if key in list_fields:
            values = value if isinstance(value, list) else []
            data.setlist(key, [str(item) for item in values])
        elif isinstance(value, bool):
            if value:
                data[key] = "on"
        elif value is not None:
            data[key] = str(value)
    return data


def _channel_form_data(payload: dict[str, Any]) -> dict[str, Any]:
    value = dict(payload)
    channels = value.pop("channels", None)
    if channels is not None:
        if not isinstance(channels, list):
            raise ValidationError({"channels": ["Channels must be an ordered array."]})
        value["custom_channels"] = "\n".join(str(item) for item in channels)
    return value


def _notices(form) -> list[dict[str, str]]:
    names = list(getattr(form, "unverified_channel_names", ()))
    if not names:
        return []
    return [
        {
            "level": "warning",
            "message": "Saved, but Twitch could not verify these channels right now: "
            + ", ".join(names)
            + ".",
        }
    ]


def api_endpoint(*methods: str, staff: bool = True):
    allowed = {method.upper() for method in methods}

    def decorator(view: ApiView) -> ApiView:
        @wraps(view)
        def wrapped(request: HttpRequest, *args, **kwargs):
            if request.method not in allowed:
                response = _error(
                    "method_not_allowed",
                    f"Use one of: {', '.join(sorted(allowed))}.",
                    status=405,
                )
                response["Allow"] = ", ".join(sorted(allowed))
                return response
            if staff and (not request.user.is_authenticated or not request.user.is_staff):
                return _error(
                    "authentication_required",
                    "Staff authentication is required.",
                    status=401,
                )
            try:
                return view(request, *args, **kwargs)
            except (Http404, ObjectDoesNotExist):
                return _error("not_found", "The requested resource was not found.", status=404)
            except ValidationError as exc:
                return _validation_error(exc)
            except Exception as exc:  # pragma: no cover - defensive redaction boundary
                logger.error(
                    "API operation %s failed with %s; sensitive details were suppressed.",
                    view.__name__,
                    type(exc).__name__,
                )
                return _error(
                    "internal_error",
                    "The operation failed. Sensitive details were suppressed.",
                    status=500,
                )

        return wrapped  # type: ignore[return-value]

    return decorator


def csrf_failure(request: HttpRequest, reason: str = ""):
    if request.path.startswith("/api/v1/"):
        return _error(
            "csrf_failed",
            "The security token is missing or expired. Refresh and try again.",
            status=403,
        )
    return django_csrf_failure(request, reason=reason)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value else None


WATCHING_CHANNELS_FRESH_FOR = timedelta(minutes=5)


def _fresh_watching_channels(
    state: MinerInstanceState | None,
    *,
    now: datetime | None = None,
) -> list[str]:
    if (
        state is None
        or state.current_run is None
        or state.observed_state
        not in (
            MinerInstanceState.ObservedState.STARTING,
            MinerInstanceState.ObservedState.RUNNING,
        )
        or state.watching_updated_at is None
        or state.watching_updated_at < (now or timezone.now()) - WATCHING_CHANNELS_FRESH_FOR
    ):
        return []
    requested = {
        channel.casefold()
        for channel in state.watching_channels
        if isinstance(channel, str)
    }
    return [
        channel
        for channel in state.current_run.channels
        if isinstance(channel, str) and channel.casefold() in requested
    ][:2]


@dataclass(frozen=True, slots=True)
class AccountTelemetry:
    account: MinerAccount
    state: MinerInstanceState | None
    desired: str
    observed: str
    current_run: MinerRun | None
    channels: tuple[str, ...]
    source_mode: str
    source_name: str
    source_label: str
    pid: int | None
    last_heartbeat: datetime | None
    open_incident: MinerIncident | None


def _account_queryset():
    open_incidents = MinerIncident.objects.filter(
        status=MinerIncident.Status.OPEN
    ).order_by("-opened_at")
    return (
        MinerAccount.objects.select_related(
            "runtime_state",
            "runtime_state__current_run",
            "selection",
            "selection__preset",
            "credential",
        )
        .prefetch_related(
            "custom_channels",
            "selection__preset__channels",
            Prefetch("incidents", queryset=open_incidents, to_attr="open_incident_rows"),
        )
        .order_by("config_key")
    )


def _as_telemetry(
    account: MinerAccount,
    *,
    farm_default_channels: tuple[str, ...] | None = None,
) -> AccountTelemetry:
    try:
        state = account.runtime_state
    except MinerInstanceState.DoesNotExist:
        state = None
    run = state.current_run if state else None
    incidents = getattr(account, "open_incident_rows", ())
    try:
        selection = account.selection
    except AccountChannelSelection.DoesNotExist:
        selection = None

    if run:
        channels = tuple(run.channels)
        source_mode = run.source_mode
        source_name = run.source_name
    elif selection and selection.mode == AccountChannelSelection.Mode.PRESET:
        channels = (
            tuple(channel.name for channel in selection.preset.channels.all())
            if selection.preset
            else ()
        )
        source_mode = selection.mode
        source_name = selection.preset.name if selection.preset else ""
    elif selection and selection.mode == AccountChannelSelection.Mode.CUSTOM:
        channels = tuple(channel.name for channel in account.custom_channels.all())
        source_mode = selection.mode
        source_name = account.config_key
    else:
        channels = farm_default_channels or tuple(FarmConfiguration.load().default_channels)
        source_mode = AccountChannelSelection.Mode.DEFAULT
        source_name = "farm defaults"

    if source_mode == AccountChannelSelection.Mode.PRESET:
        source_label = source_name or "Preset"
    elif source_mode == AccountChannelSelection.Mode.CUSTOM:
        source_label = "Custom"
    else:
        source_label = "Default"
    return AccountTelemetry(
        account=account,
        state=state,
        desired=state.desired_state if state else MinerInstanceState.DesiredState.STOPPED,
        observed=state.observed_state if state else MinerInstanceState.ObservedState.UNKNOWN,
        current_run=run,
        channels=channels,
        source_mode=source_mode,
        source_name=source_name,
        source_label=source_label,
        pid=(state.advisory_pid if state else None) or (run.pid if run else None),
        last_heartbeat=state.last_heartbeat if state else None,
        open_incident=incidents[0] if incidents else None,
    )


def _account_rows() -> list[AccountTelemetry]:
    defaults = tuple(FarmConfiguration.load().default_channels)
    return [_as_telemetry(account, farm_default_channels=defaults) for account in _account_queryset()]


def _supervisor() -> dict[str, Any]:
    lease = WorkerLease.objects.order_by("-heartbeat_at").first()
    if lease is None:
        return {
            "status": "offline",
            "label": "No supervisor heartbeat",
            "owner_id": "unclaimed",
            "pid": None,
            "heartbeat_at": None,
            "expires_at": None,
        }
    live = lease.expires_at > timezone.now()
    return {
        "status": "healthy" if live else "stale",
        "label": "Supervisor online" if live else "Supervisor heartbeat stale",
        "owner_id": lease.owner_id,
        "pid": lease.pid,
        "heartbeat_at": _iso(lease.heartbeat_at),
        "expires_at": _iso(lease.expires_at),
    }


def _serialize_incident(incident: MinerIncident) -> dict[str, Any]:
    return {
        "id": incident.pk,
        "account_id": incident.account_id,
        "account_key": incident.account.config_key if incident.account else None,
        "kind": incident.kind,
        "status": incident.status,
        "summary": incident.summary,
        "details": incident.details,
        "opened_at": _iso(incident.opened_at),
        "recovered_at": _iso(incident.recovered_at),
        "restart_attempts": [
            {
                "attempt_number": item.attempt_number,
                "outcome": item.outcome,
                "scheduled_at": _iso(item.scheduled_at),
                "error": item.error,
            }
            for item in incident.restart_attempts.all()
        ],
    }


def _serialize_command(command: MinerCommand) -> dict[str, Any]:
    return {
        "id": command.pk,
        "account_id": command.account_id,
        "account_key": command.account.config_key,
        "action": command.action,
        "status": command.status,
        "reason": command.reason,
        "attempts": command.attempts,
        "error": command.error,
        "actor": command.actor.get_username() if command.actor else "system",
        "created_at": _iso(command.created_at),
        "completed_at": _iso(command.completed_at),
    }


def _serialize_action(item: ActionLog) -> dict[str, Any]:
    return {
        "id": item.pk,
        "action": item.action,
        "message": item.message,
        "account_id": item.account_id,
        "account_key": item.account.config_key if item.account else None,
        "actor": item.actor.get_username() if item.actor else "system",
        "created_at": _iso(item.created_at),
    }


def _serialize_account(row: AccountTelemetry) -> dict[str, Any]:
    account = row.account
    state = row.state
    try:
        credential = account.credential
        auth_method = credential.auth_method
    except ObjectDoesNotExist:
        auth_method = AccountCredential.AuthMethod.TWITCH_TV
    auth_status = (
        state.authentication_status
        if state is not None
        else MinerInstanceState.AuthenticationStatus.UNLINKED
    )
    expired = bool(
        state
        and state.authentication_expires_at
        and state.authentication_expires_at <= timezone.now()
    )
    if expired and auth_status == MinerInstanceState.AuthenticationStatus.PENDING:
        auth_status = MinerInstanceState.AuthenticationStatus.REAUTH_REQUIRED
    return {
        "id": account.pk,
        "config_key": account.config_key,
        "username": account.display_username,
        "is_active": account.is_active,
        "has_credentials": account.has_credentials,
        "authentication": {
            "method": auth_method,
            "status": auth_status,
            "activation_url": (
                state.authentication_uri
                if state and auth_status == MinerInstanceState.AuthenticationStatus.PENDING
                else ""
            ),
            "user_code": (
                state.authentication_code
                if state and auth_status == MinerInstanceState.AuthenticationStatus.PENDING
                else ""
            ),
            "expires_at": (
                _iso(state.authentication_expires_at)
                if state and auth_status == MinerInstanceState.AuthenticationStatus.PENDING
                else None
            ),
            "error": state.authentication_error if state else "",
            "updated_at": _iso(state.authentication_updated_at) if state else None,
            "can_reconnect": bool(account.is_active),
        },
        "desired": row.desired,
        "observed": row.observed,
        "source": {
            "mode": row.source_mode,
            "name": row.source_name,
            "label": row.source_label,
            "channels": list(row.channels),
        },
        "watching_channels": _fresh_watching_channels(state),
        "watching_updated_at": _iso(state.watching_updated_at) if state else None,
        "pid": row.pid,
        "last_heartbeat": _iso(row.last_heartbeat),
        "open_incident": (
            {
                "id": row.open_incident.pk,
                "summary": row.open_incident.summary,
                "opened_at": _iso(row.open_incident.opened_at),
            }
            if row.open_incident
            else None
        ),
        "updated_at": _iso(account.updated_at),
    }


def _planned_source(account: MinerAccount) -> dict[str, Any]:
    try:
        selection = account.selection
    except AccountChannelSelection.DoesNotExist:
        selection = None
    if selection is None or selection.mode == AccountChannelSelection.Mode.DEFAULT:
        configuration = FarmConfiguration.load()
        return {
            "mode": "default",
            "name": "Farm defaults",
            "preset_id": None,
            "channels": list(configuration.default_channels),
        }
    if selection.mode == AccountChannelSelection.Mode.PRESET:
        return {
            "mode": "preset",
            "name": selection.preset.name if selection.preset else "Missing preset",
            "preset_id": selection.preset_id,
            "channels": selection.preset.channel_names if selection.preset else [],
        }
    return {
        "mode": "custom",
        "name": account.config_key,
        "preset_id": None,
        "channels": list(
            account.custom_channels.order_by("position", "id").values_list("name", flat=True)
        ),
    }


def _serialize_run(run: MinerRun) -> dict[str, Any]:
    return {
        "id": run.pk,
        "source_mode": run.source_mode,
        "source_name": run.source_name,
        "channels": list(run.channels),
        "channel_revision": run.channel_revision,
        "auth_method": run.auth_method,
        "reset_session": run.reset_session,
        "pid": run.pid,
        "started_at": _iso(run.started_at),
        "ended_at": _iso(run.ended_at),
        "exit_code": run.exit_code,
        "exit_signal": run.exit_signal,
        "stop_reason": run.stop_reason,
        "error": run.error,
    }


def _preset_summary(preset: ChannelPreset) -> dict[str, Any]:
    channels = [channel.name for channel in preset.channels.all()]
    watched: set[str] = set()
    assignments = list(preset.account_selections.all())
    for assignment in assignments:
        try:
            state = assignment.account.runtime_state
        except MinerInstanceState.DoesNotExist:
            continue
        if (
            state.current_run is None
            or state.current_run.source_mode != AccountChannelSelection.Mode.PRESET
            or state.current_run.source_name != preset.name
        ):
            continue
        watched.update(channel.casefold() for channel in _fresh_watching_channels(state))
    return {
        "id": preset.pk,
        "name": preset.name,
        "channels": channels,
        "watching_channels": [
            channel for channel in channels if channel.casefold() in watched
        ],
        "assignment_count": len(assignments),
        "updated_at": _iso(preset.updated_at),
    }


def _preset_queryset():
    return ChannelPreset.objects.prefetch_related(
        "channels",
        "account_selections",
        "account_selections__account",
        "account_selections__account__runtime_state__current_run",
    ).order_by("name")


@never_cache
@ensure_csrf_cookie
@api_endpoint("GET", staff=False)
def session(request: HttpRequest):
    get_token(request)
    user = request.user
    return _success(
        {
            "authenticated": bool(user.is_authenticated and user.is_staff),
            "user": (
                {"id": user.pk, "username": user.get_username()}
                if user.is_authenticated and user.is_staff
                else None
            ),
        }
    )


@sensitive_post_parameters("password")
@api_endpoint("POST", staff=False)
def session_login(request: HttpRequest):
    payload = _parse_json(request)
    form = StaffAuthenticationForm(
        request,
        data={"username": payload.get("username", ""), "password": payload.get("password", "")},
    )
    if not form.is_valid():
        return _form_error(form)
    login(request, form.get_user())
    return _success(
        {
            "authenticated": True,
            "user": {"id": request.user.pk, "username": request.user.get_username()},
        }
    )


@api_endpoint("POST", staff=False)
def session_logout(request: HttpRequest):
    logout(request)
    return _success({"authenticated": False, "user": None})


@never_cache
@api_endpoint("GET")
def runtime(request: HttpRequest):
    rows = _account_rows()
    incidents = (
        MinerIncident.objects.select_related("account", "run")
        .prefetch_related("restart_attempts")
        .order_by("-opened_at")[:8]
    )
    failed = (
        MinerCommand.objects.select_related("account", "actor")
        .filter(status=MinerCommand.Status.FAILED)
        .order_by("-completed_at", "-created_at")[:8]
    )
    activity = ActionLog.objects.select_related("actor", "account").order_by("-created_at")[:12]
    return _success(
        {
            "supervisor": _supervisor(),
            "summary": {
                "total": len(rows),
                "desired_running": sum(row.desired == "running" for row in rows),
                "observed_running": sum(row.observed == "running" for row in rows),
                "degraded": sum(row.observed == "degraded" for row in rows),
                "open_incidents": MinerIncident.objects.filter(status="open").count(),
            },
            "accounts": [_serialize_account(row) for row in rows],
            "incidents": [_serialize_incident(item) for item in incidents],
            "command_faults": [_serialize_command(item) for item in failed],
            "activity": [_serialize_action(item) for item in activity],
            "generated_at": _iso(timezone.now()),
        }
    )


@api_endpoint("POST")
def runtime_actions(request: HttpRequest):
    action = str(_parse_json(request).get("action", ""))
    if action not in {"start", "stop", "restart"}:
        raise ValidationError({"action": ["Choose start, stop, or restart."]})
    commands = enqueue_all(
        action,
        actor=request.user,
        reason="Requested for all configured accounts from the web control room.",
    )
    return _success({"action": action, "queued": len(commands)}, status=202)


@api_endpoint("GET", "POST")
def accounts(request: HttpRequest):
    if request.method == "GET":
        rows = _account_rows()
        return _success(
            {
                "accounts": [_serialize_account(row) for row in rows],
                "active_count": sum(row.account.is_active for row in rows),
                "presets": [_preset_summary(item) for item in _preset_queryset()],
                "farm_default_channels": list(FarmConfiguration.load().default_channels),
                "autostart_new_accounts": FarmConfiguration.load().autostart_new_accounts,
            }
        )

    payload = _channel_form_data(_parse_json(request))
    if "preset_id" in payload:
        payload["preset"] = payload.pop("preset_id")
    form = AccountCreateForm(_form_data(payload))
    if not form.is_valid():
        return _form_error(
            form, aliases={"custom_channels": "channels", "preset": "preset_id"}
        )
    mode = form.cleaned_data["mode"]
    account = create_account(
        config_key=form.cleaned_data["config_key"],
        username=form.cleaned_data["username"],
        password=form.cleaned_data["password"],
        auth_method=(
            AccountCredential.AuthMethod.LEGACY_PASSWORD
            if form.cleaned_data["password"]
            else AccountCredential.AuthMethod.TWITCH_TV
        ),
        mode=mode,
        channels=(form.cleaned_data["custom_channel_names"] if mode == "custom" else None),
        preset=form.cleaned_data.get("preset"),
        start_after_save=form.cleaned_data["start_after_save"],
        actor=request.user,
    )
    row = _as_telemetry(get_object_or_404(_account_queryset(), pk=account.pk))
    return _success(_serialize_account(row), notices=_notices(form), status=201)


def _account_detail(account: MinerAccount) -> dict[str, Any]:
    row = _as_telemetry(account)
    planned = _planned_source(account)
    incidents = account.incidents.prefetch_related("restart_attempts").select_related("run").order_by(
        "-opened_at"
    )[:20]
    runs = account.runs.order_by("-started_at")[:12]
    commands = account.commands.select_related("actor", "account").order_by("-created_at")[:12]
    return {
        **_serialize_account(row),
        "planned_source": planned,
        "configuration": {
            "username": account.display_username,
            "mode": planned["mode"],
            "preset_id": planned["preset_id"],
            "channels": planned["channels"] if planned["mode"] == "custom" else [],
        },
        "presets": [_preset_summary(item) for item in _preset_queryset()],
        "farm_default_channels": list(FarmConfiguration.load().default_channels),
        "incidents": [_serialize_incident(item) for item in incidents],
        "runs": [_serialize_run(item) for item in runs],
        "commands": [_serialize_command(item) for item in commands],
    }


@api_endpoint("GET", "PATCH")
def account_detail(request: HttpRequest, pk: int):
    account = get_object_or_404(_account_queryset(), pk=pk)
    if request.method == "GET":
        return _success(_account_detail(account))
    if not account.is_active:
        return _error("inactive_account", "Inactive legacy accounts are read-only.", status=409)
    payload = _parse_json(request)
    form = AccountEditForm(
        _form_data({"username": payload.get("username", ""), "password": payload.get("password", "")}),
        account=account,
    )
    if not form.is_valid():
        return _form_error(form)
    update_account(
        account,
        username=form.cleaned_data["username"],
        password=form.cleaned_data["password"] or None,
        actor=request.user,
    )
    account = get_object_or_404(_account_queryset(), pk=pk)
    return _success(_account_detail(account))


@never_cache
@api_endpoint("GET")
def account_telemetry(request: HttpRequest, pk: int):
    account = get_object_or_404(_account_queryset(), pk=pk)
    row = _as_telemetry(account)
    return _success(
        {
            "account": _serialize_account(row),
            "planned_source": _planned_source(account),
            "generated_at": _iso(timezone.now()),
        }
    )


@api_endpoint("PUT")
def account_channel_source(request: HttpRequest, pk: int):
    account = get_object_or_404(MinerAccount, pk=pk, is_active=True)
    payload = _channel_form_data(_parse_json(request))
    if "preset_id" in payload:
        payload["preset"] = payload.pop("preset_id")
    form = AccountChannelSelectionForm(_form_data(payload), account=account)
    if not form.is_valid():
        return _form_error(
            form, aliases={"custom_channels": "channels", "preset": "preset_id"}
        )
    mode = form.cleaned_data["mode"]
    set_account_channel_selection(
        account,
        mode,
        channels=(form.cleaned_data["custom_channel_names"] if mode == "custom" else None),
        preset=form.cleaned_data.get("preset") if mode == "preset" else None,
        actor=request.user,
    )
    account = get_object_or_404(_account_queryset(), pk=pk)
    return _success(_account_detail(account), notices=_notices(form))


@api_endpoint("POST")
def account_actions(request: HttpRequest, pk: int):
    account = get_object_or_404(MinerAccount, pk=pk)
    action = str(_parse_json(request).get("action", ""))
    if action not in {"start", "stop", "restart"}:
        raise ValidationError({"action": ["Choose start, stop, or restart."]})
    command = enqueue_command(
        account,
        action,
        actor=request.user,
        reason="Requested from the web control room.",
    )
    return _success({"command": _serialize_command(command)}, status=202)


@never_cache
@api_endpoint("POST")
def account_tv_authentication(request: HttpRequest, pk: int):
    account = get_object_or_404(MinerAccount, pk=pk)
    command = request_tv_authentication(account, actor=request.user)
    account = get_object_or_404(_account_queryset(), pk=pk)
    return _success(
        {
            "command": _serialize_command(command),
            "account": _serialize_account(_as_telemetry(account)),
        },
        status=202,
    )


@api_endpoint("GET", "POST")
def presets(request: HttpRequest):
    if request.method == "GET":
        return _success({"presets": [_preset_summary(item) for item in _preset_queryset()]})
    payload = _parse_json(request)
    channels = payload.get("channels", [])
    if not isinstance(channels, list):
        raise ValidationError({"channels": ["Channels must be an ordered array."]})
    form = PresetForm(
        _form_data({"name": payload.get("name", ""), "channels": "\n".join(map(str, channels))})
    )
    if not form.is_valid():
        return _form_error(form)
    preset = save_preset(
        name=form.cleaned_data["name"],
        channels=form.cleaned_channel_names,
        actor=request.user,
    )
    preset = get_object_or_404(_preset_queryset(), pk=preset.pk)
    return _success(_preset_summary(preset), notices=_notices(form), status=201)


def _preset_detail(preset: ChannelPreset) -> dict[str, Any]:
    eligible = MinerAccount.objects.filter(
        is_active=True, credential__isnull=False
    ).order_by("display_username", "config_key")
    selected = set(preset.account_selections.values_list("account_id", flat=True))
    return {
        **_preset_summary(preset),
        "assigned_account_ids": sorted(selected),
        "eligible_accounts": [
            {
                "id": account.pk,
                "config_key": account.config_key,
                "username": account.display_username,
                "assigned": account.pk in selected,
            }
            for account in eligible
        ],
    }


@api_endpoint("GET", "PUT", "DELETE")
def preset_detail(request: HttpRequest, pk: int):
    preset = get_object_or_404(_preset_queryset(), pk=pk)
    if request.method == "GET":
        return _success(_preset_detail(preset))
    if request.method == "DELETE":
        name = preset.name
        delete_preset(preset, actor=request.user)
        return _success({"id": pk, "name": name, "deleted": True})

    payload = _parse_json(request)
    channels = payload.get("channels", [])
    if not isinstance(channels, list):
        raise ValidationError({"channels": ["Channels must be an ordered array."]})
    form = PresetForm(
        _form_data({"name": payload.get("name", ""), "channels": "\n".join(map(str, channels))}),
        instance=preset,
    )
    if not form.is_valid():
        return _form_error(form)
    preset = save_preset(
        preset=preset,
        name=form.cleaned_data["name"],
        channels=form.cleaned_channel_names,
        actor=request.user,
    )
    preset = get_object_or_404(_preset_queryset(), pk=preset.pk)
    return _success(_preset_detail(preset), notices=_notices(form))


@api_endpoint("PUT")
@transaction.atomic
def preset_assignments(request: HttpRequest, pk: int):
    preset = get_object_or_404(ChannelPreset, pk=pk)
    payload = _parse_json(request)
    account_ids = payload.get("account_ids", [])
    if not isinstance(account_ids, list):
        raise ValidationError({"account_ids": ["Assignments must be an array of account IDs."]})
    form = PresetAssignmentForm(
        _form_data({"accounts": account_ids}, list_fields=("accounts",)), preset=preset
    )
    if not form.is_valid():
        return _form_error(form, aliases={"accounts": "account_ids"})
    selected = set(form.cleaned_data["accounts"].values_list("pk", flat=True))
    current = {
        item.account_id: item.account
        for item in AccountChannelSelection.objects.select_related("account").filter(
            preset=preset, account__is_active=True
        )
    }
    for account_id, account in current.items():
        if account_id not in selected:
            set_account_channel_selection(account, "default", actor=request.user)
    for account in MinerAccount.objects.filter(pk__in=selected):
        if account.pk not in current:
            set_account_channel_selection(account, "preset", preset=preset, actor=request.user)
    preset = get_object_or_404(_preset_queryset(), pk=pk)
    return _success(_preset_detail(preset))


@api_endpoint("GET", "PUT")
def settings_general(request: HttpRequest):
    configuration = FarmConfiguration.load()
    if request.method == "GET":
        return _success(
            {
                "default_channels": list(configuration.default_channels),
                "autostart_new_accounts": configuration.autostart_new_accounts,
                "updated_at": _iso(configuration.updated_at),
            }
        )
    payload = _parse_json(request)
    channels = payload.get("default_channels", [])
    if not isinstance(channels, list):
        raise ValidationError({"default_channels": ["Channels must be an ordered array."]})
    form = FarmConfigurationForm(
        _form_data(
            {
                "default_channels": "\n".join(map(str, channels)),
                "autostart_new_accounts": bool(payload.get("autostart_new_accounts", False)),
            }
        ),
        configuration=configuration,
    )
    if not form.is_valid():
        return _form_error(form)
    configuration = update_farm_configuration(
        default_channels=form.cleaned_channel_names,
        autostart_new_accounts=form.cleaned_data["autostart_new_accounts"],
        actor=request.user,
    )
    return _success(
        {
            "default_channels": list(configuration.default_channels),
            "autostart_new_accounts": configuration.autostart_new_accounts,
            "updated_at": _iso(configuration.updated_at),
        },
        notices=_notices(form),
    )


@api_endpoint("POST")
def settings_imports(request: HttpRequest):
    form = LegacyImportUploadForm(request.POST, request.FILES)
    if not form.is_valid():
        return _form_error(form)
    from .legacy_import import prepare_legacy_import

    draft = prepare_legacy_import(form.cleaned_data["archive"], request.user)
    return _success(
        {
            "id": str(draft.pk),
            "preview": draft.preview,
            "expires_at": _iso(draft.expires_at),
            "created_at": _iso(draft.created_at),
        },
        status=201,
    )


@api_endpoint("POST")
def settings_import_confirm(request: HttpRequest, draft_id: uuid.UUID):
    payload = _parse_json(request)
    form = LegacyImportConfirmForm(
        _form_data(
            {
                "draft_id": str(draft_id),
                "replace": bool(payload.get("replace", False)),
                "acknowledged": bool(payload.get("acknowledged", False)),
                "confirmation": payload.get("confirmation", ""),
            }
        )
    )
    if not form.is_valid():
        return _form_error(form)
    from .legacy_import import confirm_legacy_import

    result = confirm_legacy_import(
        form.cleaned_data["draft_id"],
        request.user,
        replace=form.cleaned_data["replace"],
        acknowledged=form.cleaned_data["acknowledged"],
        confirmation=form.cleaned_data["confirmation"],
    )
    return _success(result.as_dict())


@api_endpoint("DELETE")
def settings_import_delete(request: HttpRequest, draft_id: uuid.UUID):
    deleted, _ = LegacyImportDraft.objects.filter(pk=draft_id, actor=request.user).delete()
    if not deleted:
        return _error("not_found", "The import draft was not found.", status=404)
    return _success({"id": str(draft_id), "deleted": True})


@never_cache
@api_endpoint("GET")
def logs(request: HttpRequest):
    account_value = request.GET.get("account_id", "").strip()
    cursor = request.GET.get("cursor", "").strip() or None
    try:
        if account_value:
            try:
                account_id = int(account_value)
            except ValueError:
                raise ValidationError({"account_id": ["Choose a valid account."]}) from None
            account = get_object_or_404(MinerAccount, pk=account_id)
            try:
                state = account.runtime_state
            except MinerInstanceState.DoesNotExist:
                state = None
            run = state.current_run if state else None
            if run is None:
                live = {"lines": [], "cursor": None, "reset": bool(cursor), "run_id": None}
            else:
                live = read_account_live(
                    account_id=account.pk,
                    run_id=run.pk,
                    cursor=cursor,
                )
            source = {
                "kind": "account",
                "account_id": account.pk,
                "account_key": account.config_key,
                "username": account.display_username,
            }
        else:
            live = read_combined_live(cursor)
            source = {
                "kind": "combined",
                "account_id": None,
                "account_key": None,
                "username": None,
            }
    except LogStorageError as exc:
        return _error("log_unavailable", str(exc), status=409)
    lines = live["lines"]
    return _success(
        {
            "lines": lines,
            "line_count": len(lines),
            "max_lines": MAX_LOG_LINES,
            "max_bytes": MAX_LOG_BYTES,
            "cursor": live["cursor"],
            "reset": live["reset"],
            "run_id": live["run_id"],
            "source": source,
            "supervisor": _supervisor(),
            "generated_at": _iso(timezone.now()),
        }
    )


def _log_limit(request: HttpRequest, *, default: int, maximum: int) -> int:
    raw = request.GET.get("limit", str(default))
    try:
        value = int(raw)
    except ValueError:
        raise ValidationError({"limit": ["Enter a valid result limit."]}) from None
    if value < 1 or value > maximum:
        raise ValidationError({"limit": [f"Choose a limit from 1 to {maximum}."]})
    return value


def _encode_history_cursor(run: MinerRun) -> str:
    return signing.dumps(
        {"started_at": run.started_at.isoformat(), "id": run.pk},
        salt=LOG_HISTORY_CURSOR_SALT,
        compress=True,
    )


def _decode_history_cursor(value: str) -> tuple[datetime, int]:
    try:
        payload = signing.loads(
            value,
            salt=LOG_HISTORY_CURSOR_SALT,
            max_age=30 * 24 * 60 * 60,
        )
        started_at = datetime.fromisoformat(payload["started_at"])
        run_id = int(payload["id"])
    except (signing.BadSignature, KeyError, TypeError, ValueError) as exc:
        raise ValidationError({"before": ["The history cursor is invalid or expired."]}) from exc
    return started_at, run_id


def _serialize_log_run(run: MinerRun) -> dict[str, Any] | None:
    try:
        summary = summarize_run_log(run.account_id, run.pk)
    except LogStorageError:
        return None
    if not summary.available:
        return None
    if run.ended_at is None:
        archive_state = "active"
    elif summary.compression_pending:
        archive_state = "compression_pending"
    else:
        archive_state = "ready"
    return {
        "run_id": run.pk,
        "account": {
            "id": run.account_id,
            "config_key": run.account.config_key,
            "username": run.account.display_username,
            "is_active": run.account.is_active,
        },
        "started_at": _iso(run.started_at),
        "ended_at": _iso(run.ended_at),
        "stop_reason": run.stop_reason,
        "exit_code": run.exit_code,
        "exit_signal": run.exit_signal,
        "archive_state": archive_state,
        "compressed_bytes": summary.compressed_bytes,
        "compressed_parts": summary.compressed_parts,
        "plaintext_parts": summary.plaintext_parts,
        "truncated": summary.truncated,
        "downloadable": run.ended_at is not None and not summary.compression_pending,
    }


@never_cache
@api_endpoint("GET")
def log_runs(request: HttpRequest):
    limit = _log_limit(request, default=25, maximum=100)
    queryset = MinerRun.objects.select_related("account").filter(ended_at__isnull=False)
    account_value = request.GET.get("account_id", "").strip()
    if account_value:
        try:
            account_id = int(account_value)
        except ValueError:
            raise ValidationError({"account_id": ["Choose a valid account."]}) from None
        if not MinerAccount.objects.filter(pk=account_id).exists():
            raise Http404
        queryset = queryset.filter(account_id=account_id)
    before = request.GET.get("before", "").strip()
    if before:
        started_at, run_id = _decode_history_cursor(before)
        queryset = queryset.filter(
            Q(started_at__lt=started_at) | Q(started_at=started_at, pk__lt=run_id)
        )

    rows: list[dict[str, Any]] = []
    last_returned: MinerRun | None = None
    has_more = False
    for run in queryset.order_by("-started_at", "-id").iterator():
        serialized = _serialize_log_run(run)
        if serialized is None:
            continue
        if len(rows) == limit:
            has_more = True
            break
        rows.append(serialized)
        last_returned = run
    next_before = (
        _encode_history_cursor(last_returned) if has_more and last_returned else None
    )
    return _success(
        {
            "runs": rows,
            "next_before": next_before,
            "retention_bytes": ACCOUNT_LOG_ARCHIVE_BYTES,
            "generated_at": _iso(timezone.now()),
        }
    )


@never_cache
@api_endpoint("GET")
def log_run_detail(request: HttpRequest, run_id: int):
    run = get_object_or_404(MinerRun.objects.select_related("account"), pk=run_id)
    serialized = _serialize_log_run(run)
    if serialized is None:
        return _error("log_not_found", "No retained log is available for this run.", status=404)
    limit = _log_limit(request, default=MAX_LOG_LINES, maximum=MAX_LOG_LINES)
    before = request.GET.get("before", "").strip() or None
    try:
        page = read_run_log_page(
            account_id=run.account_id,
            run_id=run.pk,
            before=before,
            limit=limit,
        )
    except LogStorageError as exc:
        return _error("log_unavailable", str(exc), status=409)
    return _success(
        {
            "run": serialized,
            "lines": page["lines"],
            "line_count": len(page["lines"]),
            "before": page["before"],
            "has_older": page["has_older"],
            "max_lines": MAX_LOG_LINES,
            "max_bytes": MAX_LOG_BYTES,
            "generated_at": _iso(timezone.now()),
        }
    )


@never_cache
@api_endpoint("GET")
def log_run_download(request: HttpRequest, run_id: int):
    run = get_object_or_404(MinerRun.objects.select_related("account"), pk=run_id)
    if run.ended_at is None:
        return _error(
            "log_still_active",
            "Active run logs can be downloaded after the run finishes.",
            status=409,
        )
    serialized = _serialize_log_run(run)
    if serialized is None:
        return _error("log_not_found", "No retained log is available for this run.", status=404)
    try:
        chunks, content_length = iter_run_gzip(run.account_id, run.pk)
    except LogStorageError as exc:
        return _error("log_unavailable", str(exc), status=409)
    response = StreamingHttpResponse(chunks, content_type="application/gzip")
    response["Content-Length"] = str(content_length)
    retention_label = "-truncated" if serialized["truncated"] else ""
    response["Content-Disposition"] = (
        f'attachment; filename="account-{run.account_id}-run-{run.pk}{retention_label}.log.gz"'
    )
    return response


@never_cache
@api_endpoint("GET")
def channel_validate(request: HttpRequest):
    channels = normalize_channels(request.GET.get("name", ""))
    if len(channels) != 1:
        raise ValidationError({"name": ["Enter exactly one Twitch channel."]})
    name = channels[0]
    status = lookup_twitch_names((name,)).get(name, TwitchLookupStatus.UNVERIFIED)
    return _success({"name": name, "status": status.value})
