"""Staff-only, server-rendered views for the shared Twitch farm."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import logout
from django.contrib.auth.views import LoginView
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Prefetch
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_POST, require_safe

from .forms import (
    AccountCreateForm,
    AccountEditForm,
    AccountChannelSelectionForm,
    FarmConfigurationForm,
    LegacyImportConfirmForm,
    LegacyImportUploadForm,
    PresetAssignmentForm,
    PresetForm,
    StaffAuthenticationForm,
)
from .models import (
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
from .services import (
    create_account,
    delete_preset,
    enqueue_all,
    enqueue_command,
    save_preset,
    set_account_channel_selection,
    update_account,
    update_farm_configuration,
)


CONTROL_LOGIN_URL = "controller:login"
logger = logging.getLogger(__name__)


def _log_sensitive_failure(operation: str, exc: Exception) -> None:
    """Record only the exception class; messages may contain credentials."""

    logger.error(
        "%s failed with %s; sensitive details were suppressed.",
        operation,
        type(exc).__name__,
    )


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


@dataclass(frozen=True, slots=True)
class SupervisorTelemetry:
    status: str
    label: str
    owner_id: str
    pid: int | None
    heartbeat_at: datetime | None
    expires_at: datetime | None


class StaffLoginView(LoginView):
    template_name = "registration/login.html"
    authentication_form = StaffAuthenticationForm
    redirect_authenticated_user = True
    next_page = reverse_lazy("controller:dashboard")

    def dispatch(self, request: HttpRequest, *args, **kwargs):
        # Avoid redirect loops if a non-staff session reaches the operator login.
        if request.user.is_authenticated and not request.user.is_staff:
            logout(request)
        return super().dispatch(request, *args, **kwargs)


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


def _as_telemetry(account: MinerAccount) -> AccountTelemetry:
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
        channels = tuple(FarmConfiguration.load().default_channels)
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
        desired=(
            state.desired_state
            if state
            else MinerInstanceState.DesiredState.STOPPED
        ),
        observed=(
            state.observed_state
            if state
            else MinerInstanceState.ObservedState.UNKNOWN
        ),
        current_run=run,
        channels=channels,
        source_mode=source_mode,
        source_name=source_name,
        source_label=source_label,
        pid=(state.advisory_pid if state else None) or (run.pid if run else None),
        last_heartbeat=state.last_heartbeat if state else None,
        open_incident=incidents[0] if incidents else None,
    )


def _supervisor_telemetry() -> SupervisorTelemetry:
    lease = WorkerLease.objects.order_by("-heartbeat_at").first()
    if lease is None:
        return SupervisorTelemetry(
            status="offline",
            label="No supervisor heartbeat",
            owner_id="unclaimed",
            pid=None,
            heartbeat_at=None,
            expires_at=None,
        )

    is_live = lease.expires_at > timezone.now()
    return SupervisorTelemetry(
        status="healthy" if is_live else "stale",
        label="Supervisor online" if is_live else "Supervisor heartbeat stale",
        owner_id=lease.owner_id,
        pid=lease.pid,
        heartbeat_at=lease.heartbeat_at,
        expires_at=lease.expires_at,
    )


def _status_context() -> dict:
    rows = [_as_telemetry(account) for account in _account_queryset()]
    return {
        "account_rows": rows,
        "supervisor": _supervisor_telemetry(),
        "summary": {
            "total": len(rows),
            "desired_running": sum(
                row.desired == MinerInstanceState.DesiredState.RUNNING for row in rows
            ),
            "observed_running": sum(
                row.observed == MinerInstanceState.ObservedState.RUNNING for row in rows
            ),
            "degraded": sum(
                row.observed == MinerInstanceState.ObservedState.DEGRADED for row in rows
            ),
            "open_incidents": MinerIncident.objects.filter(
                status=MinerIncident.Status.OPEN
            ).count(),
        },
    }


@require_safe
def healthz(request: HttpRequest) -> HttpResponse:
    """Liveness only: deliberately exposes no controller or account state."""

    return HttpResponse("ok\n", content_type="text/plain; charset=utf-8")


@staff_member_required(login_url=CONTROL_LOGIN_URL)
def dashboard(request: HttpRequest) -> HttpResponse:
    context = _status_context()
    context.update(
        {
            "recent_incidents": MinerIncident.objects.select_related("account", "run")
            .prefetch_related("restart_attempts")
            .order_by("-opened_at")[:8],
            "recent_actions": ActionLog.objects.select_related("actor", "account")
            .order_by("-created_at")[:10],
            "failed_commands": MinerCommand.objects.select_related("account", "actor")
            .filter(status=MinerCommand.Status.FAILED)
            .order_by("-completed_at", "-created_at")[:6],
        }
    )
    return render(request, "controller/dashboard.html", context)


@staff_member_required(login_url=CONTROL_LOGIN_URL)
def status_fragment(request: HttpRequest) -> HttpResponse:
    return render(request, "controller/_status.html", _status_context())


@staff_member_required(login_url=CONTROL_LOGIN_URL)
def account_list(request: HttpRequest) -> HttpResponse:
    return render(
        request,
        "controller/accounts.html",
        {
            "account_rows": [_as_telemetry(account) for account in _account_queryset()],
            "active_count": MinerAccount.objects.filter(is_active=True).count(),
        },
    )


@staff_member_required(login_url=CONTROL_LOGIN_URL)
@sensitive_post_parameters("password")
def account_create(request: HttpRequest) -> HttpResponse:
    form = AccountCreateForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        mode = form.cleaned_data["mode"]
        try:
            account = create_account(
                config_key=form.cleaned_data["config_key"],
                username=form.cleaned_data["username"],
                password=form.cleaned_data["password"],
                mode=mode,
                channels=(
                    form.cleaned_data["custom_channel_names"]
                    if mode == AccountChannelSelection.Mode.CUSTOM
                    else None
                ),
                preset=form.cleaned_data.get("preset"),
                start_after_save=form.cleaned_data["start_after_save"],
                actor=request.user,
            )
        except ValidationError as exc:
            form.add_error(None, "; ".join(exc.messages))
        except Exception as exc:
            _log_sensitive_failure("Account creation", exc)
            form.add_error(
                None,
                "The account could not be saved. Sensitive details were suppressed.",
            )
            return render(
                request,
                "controller/account_form.html",
                {"form": form, "account": None},
                status=500,
            )
        else:
            messages.success(request, f"Account {account.config_key} created.")
            return redirect("controller:account_detail", pk=account.pk)
    return render(
        request,
        "controller/account_form.html",
        {"form": form, "account": None},
        status=400 if request.method == "POST" else 200,
    )


@staff_member_required(login_url=CONTROL_LOGIN_URL)
@sensitive_post_parameters("password")
def account_edit(request: HttpRequest, pk: int) -> HttpResponse:
    account = get_object_or_404(MinerAccount, pk=pk, is_active=True)
    form = AccountEditForm(request.POST or None, account=account)
    if request.method == "POST" and form.is_valid():
        try:
            account = update_account(
                account,
                username=form.cleaned_data["username"],
                password=form.cleaned_data["password"] or None,
                actor=request.user,
            )
        except ValidationError as exc:
            form.add_error(None, "; ".join(exc.messages))
        except Exception as exc:
            _log_sensitive_failure("Account update", exc)
            form.add_error(
                None,
                "The account could not be updated. Sensitive details were suppressed.",
            )
            return render(
                request,
                "controller/account_form.html",
                {"form": form, "account": account},
                status=500,
            )
        else:
            messages.success(request, f"Account {account.config_key} updated.")
            return redirect("controller:account_detail", pk=account.pk)
    return render(
        request,
        "controller/account_form.html",
        {"form": form, "account": account},
        status=400 if request.method == "POST" else 200,
    )


def _planned_channel_source(account: MinerAccount) -> dict:
    try:
        selection = account.selection
    except AccountChannelSelection.DoesNotExist:
        selection = None

    if selection is None or selection.mode == AccountChannelSelection.Mode.DEFAULT:
        configuration = FarmConfiguration.load()
        return {
            "mode": AccountChannelSelection.Mode.DEFAULT,
            "name": "Farm defaults",
            "channels": tuple(configuration.default_channels),
            "managed_externally": False,
        }
    if selection.mode == AccountChannelSelection.Mode.PRESET:
        return {
            "mode": selection.mode,
            "name": selection.preset.name if selection.preset else "Missing preset",
            "channels": (
                tuple(channel.name for channel in selection.preset.channels.all())
                if selection.preset
                else ()
            ),
            "managed_externally": False,
        }
    return {
        "mode": selection.mode,
        "name": account.config_key,
        "channels": tuple(channel.name for channel in account.custom_channels.all()),
        "managed_externally": False,
    }


def _account_detail_context(
    account: MinerAccount,
    *,
    selection_form: AccountChannelSelectionForm | None = None,
) -> dict:
    return {
        "account": account,
        "telemetry": _as_telemetry(account),
        "planned_source": _planned_channel_source(account),
        "selection_form": selection_form or AccountChannelSelectionForm(account=account),
        "incidents": account.incidents.prefetch_related("restart_attempts")
        .select_related("run")
        .order_by("-opened_at")[:20],
        "runs": account.runs.order_by("-started_at")[:12],
        "commands": account.commands.select_related("actor").order_by("-created_at")[:10],
    }


@staff_member_required(login_url=CONTROL_LOGIN_URL)
def account_detail(request: HttpRequest, pk: int) -> HttpResponse:
    account = get_object_or_404(_account_queryset(), pk=pk)
    return render(
        request,
        "controller/account_detail.html",
        _account_detail_context(account),
    )


@staff_member_required(login_url=CONTROL_LOGIN_URL)
@never_cache
@require_safe
def account_info_fragment(request: HttpRequest, pk: int) -> HttpResponse:
    account = get_object_or_404(_account_queryset(), pk=pk)
    response = render(
        request,
        "controller/account_info_fragment.html",
        {
            "account": account,
            "telemetry": _as_telemetry(account),
            "planned_source": _planned_channel_source(account),
        },
    )
    response["Cache-Control"] = "private, no-store, max-age=0"
    return response


@staff_member_required(login_url=CONTROL_LOGIN_URL)
@require_POST
def account_action(request: HttpRequest, pk: int, action: str) -> HttpResponse:
    account = get_object_or_404(MinerAccount, pk=pk)
    valid_actions = set(MinerCommand.Action.values)
    if action not in valid_actions:
        messages.error(request, "Unknown lifecycle command.")
        return redirect("controller:account_detail", pk=account.pk)
    if action != MinerCommand.Action.STOP and (
        not account.is_active or not account.has_credentials
    ):
        messages.error(request, "This account is archived or has no stored credentials.")
        return redirect("controller:account_detail", pk=account.pk)

    enqueue_command(
        account,
        action,
        actor=request.user,
        reason="Requested from the web control room.",
    )
    messages.success(request, f"{action.title()} command queued for {account.config_key}.")
    return redirect("controller:account_detail", pk=account.pk)


@staff_member_required(login_url=CONTROL_LOGIN_URL)
@require_POST
def global_action(request: HttpRequest, action: str) -> HttpResponse:
    if action not in {
        MinerCommand.Action.START,
        MinerCommand.Action.STOP,
        MinerCommand.Action.RESTART,
    }:
        messages.error(request, "Unknown global lifecycle command.")
        return redirect("controller:dashboard")
    commands = enqueue_all(
        action,
        actor=request.user,
        reason="Requested for all configured accounts from the web control room.",
    )
    messages.success(request, f"Queued {action} for {len(commands)} configured account(s).")
    return redirect("controller:dashboard")


@staff_member_required(login_url=CONTROL_LOGIN_URL)
@require_POST
def account_channel_selection(request: HttpRequest, pk: int) -> HttpResponse:
    account = get_object_or_404(MinerAccount, pk=pk)
    form = AccountChannelSelectionForm(request.POST, account=account)
    if not form.is_valid():
        telemetry_account = get_object_or_404(_account_queryset(), pk=pk)
        return render(
            request,
            "controller/account_detail.html",
            _account_detail_context(telemetry_account, selection_form=form),
            status=400,
        )

    mode = form.cleaned_data["mode"]
    kwargs = {
        "preset": form.cleaned_data.get("preset"),
        "actor": request.user,
    }
    if mode == AccountChannelSelection.Mode.CUSTOM:
        kwargs["channels"] = form.cleaned_data["custom_channel_names"]
        kwargs["preset"] = None
    try:
        set_account_channel_selection(account, mode, **kwargs)
    except ValidationError as exc:
        form.add_error(None, "; ".join(exc.messages))
        telemetry_account = get_object_or_404(_account_queryset(), pk=pk)
        return render(
            request,
            "controller/account_detail.html",
            _account_detail_context(telemetry_account, selection_form=form),
            status=400,
        )
    messages.success(
        request,
        "Channel source saved. A restart was queued if the account is meant to be running.",
    )
    return redirect("controller:account_detail", pk=account.pk)


@staff_member_required(login_url=CONTROL_LOGIN_URL)
def preset_list(request: HttpRequest) -> HttpResponse:
    presets = ChannelPreset.objects.prefetch_related("channels", "account_selections").order_by(
        "name"
    )
    return render(request, "controller/presets.html", {"presets": presets})


@staff_member_required(login_url=CONTROL_LOGIN_URL)
def preset_create(request: HttpRequest) -> HttpResponse:
    form = PresetForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            preset = save_preset(
                name=form.cleaned_data["name"],
                channels=form.cleaned_channel_names,
                actor=request.user,
            )
        except ValidationError as exc:
            form.add_error(None, "; ".join(exc.messages))
        else:
            messages.success(request, f"Preset {preset.name} created.")
            return redirect("controller:preset_detail", pk=preset.pk)
    return render(
        request,
        "controller/preset_form.html",
        {"form": form, "preset": None},
        status=400 if request.method == "POST" else 200,
    )


def _assignment_rows(preset: ChannelPreset, form: PresetAssignmentForm) -> list[dict]:
    if form.is_bound:
        selected_ids = {
            int(value)
            for value in form.data.getlist("accounts")
            if str(value).isdigit()
        }
    else:
        selected_ids = set(
            preset.account_selections.values_list("account_id", flat=True)
        )
    accounts = form.fields["accounts"].queryset
    return [
        {
            "account": account,
            "checked": account.pk in selected_ids,
            "input_id": f"id_accounts_{index}",
        }
        for index, account in enumerate(accounts)
    ]


def _preset_detail_context(
    preset: ChannelPreset,
    *,
    assignment_form: PresetAssignmentForm | None = None,
    editor_form: PresetForm | None = None,
) -> dict:
    form = assignment_form or PresetAssignmentForm(preset=preset)
    return {
        "preset": preset,
        "assignment_form": form,
        "assignment_rows": _assignment_rows(preset, form),
        "editor_form": editor_form or PresetForm(instance=preset),
    }


@staff_member_required(login_url=CONTROL_LOGIN_URL)
def preset_detail(request: HttpRequest, pk: int) -> HttpResponse:
    preset = get_object_or_404(
        ChannelPreset.objects.prefetch_related("channels", "account_selections__account"),
        pk=pk,
    )
    return render(
        request,
        "controller/preset_detail.html",
        _preset_detail_context(preset),
    )


@staff_member_required(login_url=CONTROL_LOGIN_URL)
@require_POST
def preset_edit(request: HttpRequest, pk: int) -> HttpResponse:
    preset = get_object_or_404(ChannelPreset, pk=pk)
    form = PresetForm(request.POST, instance=preset)
    if form.is_valid():
        try:
            preset = save_preset(
                preset=preset,
                name=form.cleaned_data["name"],
                channels=form.cleaned_channel_names,
                actor=request.user,
            )
        except ValidationError as exc:
            form.add_error(None, "; ".join(exc.messages))
        else:
            messages.success(request, f"Preset {preset.name} updated.")
            return redirect("controller:preset_detail", pk=preset.pk)
    preset = get_object_or_404(
        ChannelPreset.objects.prefetch_related("channels", "account_selections__account"),
        pk=pk,
    )
    return render(
        request,
        "controller/preset_detail.html",
        _preset_detail_context(preset, editor_form=form),
        status=400,
    )


@staff_member_required(login_url=CONTROL_LOGIN_URL)
@never_cache
@require_safe
def bot_logs(request: HttpRequest) -> HttpResponse:
    from .runtime_logs import read_runtime_log_tail

    return render(
        request,
        "controller/bot_logs.html",
        {
            "log_lines": read_runtime_log_tail(),
            "supervisor": _supervisor_telemetry(),
        },
    )


@staff_member_required(login_url=CONTROL_LOGIN_URL)
@never_cache
@require_safe
def bot_log_tail(request: HttpRequest) -> HttpResponse:
    from .runtime_logs import read_runtime_log_tail

    return render(
        request,
        "controller/_bot_log_tail.html",
        {
            "log_lines": read_runtime_log_tail(),
            "supervisor": _supervisor_telemetry(),
        },
    )


@staff_member_required(login_url=CONTROL_LOGIN_URL)
@require_POST
def preset_delete(request: HttpRequest, pk: int) -> HttpResponse:
    preset = get_object_or_404(ChannelPreset, pk=pk)
    try:
        delete_preset(preset, actor=request.user)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect("controller:preset_detail", pk=preset.pk)
    messages.success(request, f"Preset {preset.name} deleted.")
    return redirect("controller:preset_list")


@staff_member_required(login_url=CONTROL_LOGIN_URL)
@require_POST
@transaction.atomic
def preset_assign(request: HttpRequest, pk: int) -> HttpResponse:
    preset = get_object_or_404(ChannelPreset, pk=pk)
    form = PresetAssignmentForm(request.POST, preset=preset)
    if not form.is_valid():
        messages.error(request, "The preset assignment could not be saved.")
        return render(
            request,
            "controller/preset_detail.html",
            _preset_detail_context(preset, assignment_form=form),
            status=400,
        )

    selected_accounts = set(form.cleaned_data["accounts"].values_list("pk", flat=True))
    currently_selected = {
        selection.account_id: selection.account
        for selection in AccountChannelSelection.objects.select_related("account").filter(
            preset=preset,
            account__is_active=True,
        )
    }

    for account_id, account in currently_selected.items():
        if account_id not in selected_accounts:
            set_account_channel_selection(
                account,
                AccountChannelSelection.Mode.DEFAULT,
                actor=request.user,
            )
    for account in MinerAccount.objects.filter(pk__in=selected_accounts):
        if account.pk not in currently_selected:
            set_account_channel_selection(
                account,
                AccountChannelSelection.Mode.PRESET,
                preset=preset,
                actor=request.user,
            )

    messages.success(request, f"Assignments for {preset.name} updated.")
    return redirect("controller:preset_detail", pk=preset.pk)


@staff_member_required(login_url=CONTROL_LOGIN_URL)
def settings_general(request: HttpRequest) -> HttpResponse:
    configuration = FarmConfiguration.load()
    form = FarmConfigurationForm(request.POST or None, configuration=configuration)
    if request.method == "POST" and form.is_valid():
        try:
            update_farm_configuration(
                default_channels=form.cleaned_channel_names,
                autostart_new_accounts=form.cleaned_data["autostart_new_accounts"],
                actor=request.user,
            )
        except ValidationError as exc:
            form.add_error(None, "; ".join(exc.messages))
        else:
            messages.success(request, "Farm settings updated.")
            return redirect("controller:settings_general")
    return render(
        request,
        "controller/settings_general.html",
        {
            "form": form,
            "configuration": configuration,
            "settings_section": "general",
        },
        status=400 if request.method == "POST" else 200,
    )


@staff_member_required(login_url=CONTROL_LOGIN_URL)
def settings_import(request: HttpRequest) -> HttpResponse:
    form = LegacyImportUploadForm(request.POST or None, request.FILES or None)
    context = {
        "form": form,
        "settings_section": "import",
        "draft": None,
        "preview": None,
        "result": None,
    }
    if request.method == "POST" and form.is_valid():
        from .legacy_import import LegacyImportError, prepare_legacy_import

        try:
            draft = prepare_legacy_import(form.cleaned_data["archive"], request.user)
        except LegacyImportError as exc:
            form.add_error("archive", str(exc))
        except Exception as exc:
            _log_sensitive_failure("Legacy import preview", exc)
            form.add_error(
                "archive",
                "The archive could not be inspected. Sensitive details were suppressed.",
            )
            return render(
                request,
                "controller/settings_import.html",
                context,
                status=500,
            )
        else:
            context.update({"draft": draft, "preview": draft.preview})
            return render(request, "controller/settings_import.html", context)
    return render(
        request,
        "controller/settings_import.html",
        context,
        status=400 if request.method == "POST" else 200,
    )


@staff_member_required(login_url=CONTROL_LOGIN_URL)
@require_POST
def settings_import_confirm(request: HttpRequest) -> HttpResponse:
    form = LegacyImportConfirmForm(request.POST)
    context = {
        "form": LegacyImportUploadForm(),
        "settings_section": "import",
        "draft": None,
        "preview": None,
        "result": None,
    }
    if not form.is_valid():
        draft_id = form.cleaned_data.get("draft_id")
        draft = (
            LegacyImportDraft.objects.filter(pk=draft_id, actor=request.user).first()
            if draft_id is not None
            else None
        )
        context.update(
            {
                "confirm_form": form,
                "draft": draft,
                "preview": draft.preview if draft else None,
            }
        )
        return render(request, "controller/settings_import.html", context, status=400)

    from .legacy_import import LegacyImportError, confirm_legacy_import

    try:
        result = confirm_legacy_import(
            form.cleaned_data["draft_id"],
            request.user,
            replace=form.cleaned_data["replace"],
            acknowledged=form.cleaned_data["acknowledged"],
            confirmation=form.cleaned_data["confirmation"],
        )
    except LegacyImportError as exc:
        messages.error(request, str(exc))
        return redirect("controller:settings_import")
    except Exception as exc:
        _log_sensitive_failure("Legacy import confirmation", exc)
        messages.error(
            request,
            "The import could not be completed. Sensitive details were suppressed.",
        )
        return redirect("controller:settings_import")
    context["result"] = result
    messages.success(request, "Legacy import completed.")
    return render(request, "controller/settings_import.html", context)


@staff_member_required(login_url=CONTROL_LOGIN_URL)
@require_POST
def settings_import_cancel(request: HttpRequest) -> HttpResponse:
    draft_id = request.POST.get("draft_id")
    try:
        LegacyImportDraft.objects.filter(pk=draft_id, actor=request.user).delete()
    except (ValidationError, ValueError):
        pass
    messages.success(request, "Legacy import draft discarded.")
    return redirect("controller:settings_import")


@require_POST
def logout_view(request: HttpRequest) -> HttpResponse:
    logout(request)
    return redirect("controller:login")
