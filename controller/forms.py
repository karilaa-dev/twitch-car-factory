"""Forms for the staff-only Twitch farm control room."""

from __future__ import annotations

from django import forms
from django.contrib.auth.forms import AuthenticationForm
from django.core.exceptions import ValidationError

from .models import (
    AccountChannelSelection,
    ChannelPreset,
    FarmConfiguration,
    MinerAccount,
)


class StaffAuthenticationForm(AuthenticationForm):
    """Reject valid non-staff credentials at the login boundary."""

    error_messages = {
        **AuthenticationForm.error_messages,
        "not_staff": "This control room is restricted to staff operators.",
    }

    def clean(self):
        cleaned_data = super().clean()
        user = self.get_user()
        if user is not None and not user.is_staff:
            raise ValidationError(
                self.error_messages["not_staff"],
                code="not_staff",
            )
        return cleaned_data


class AccountMultipleChoiceField(forms.ModelMultipleChoiceField):
    def label_from_instance(self, account: MinerAccount) -> str:
        return account.display_username


def _normalize_channels(value: str, *, require_nonempty: bool = True) -> list[str]:
    """Use the controller's canonical channel normalizer."""

    from .services import normalize_channels

    return normalize_channels(value, require_nonempty=require_nonempty)


class PresetForm(forms.Form):
    name = forms.CharField(
        max_length=150,
        widget=forms.TextInput(
            attrs={
                "autocomplete": "off",
                "placeholder": "Night rotation",
            }
        ),
    )
    channels = forms.CharField(
        help_text="One channel per line, or separate channels with commas.",
        widget=forms.Textarea(
            attrs={
                "rows": 8,
                "spellcheck": "false",
                "placeholder": "warframe\ntwitch",
            }
        ),
    )

    def __init__(self, *args, instance: ChannelPreset | None = None, **kwargs):
        self.instance = instance
        if (
            instance is not None
            and (not args or args[0] is None)
            and "initial" not in kwargs
        ):
            kwargs["initial"] = {
                "name": instance.name,
                "channels": "\n".join(instance.channel_names),
            }
        super().__init__(*args, **kwargs)

    def clean_name(self) -> str:
        name = self.cleaned_data["name"].strip()
        queryset = ChannelPreset.objects.filter(name__iexact=name)
        if self.instance is not None:
            queryset = queryset.exclude(pk=self.instance.pk)
        if queryset.exists():
            raise ValidationError("A preset with this name already exists.")
        return name

    def clean_channels(self) -> str:
        raw_channels = self.cleaned_data["channels"]
        channels = _normalize_channels(raw_channels)
        if not channels:
            raise ValidationError("Add at least one channel.")
        self.cleaned_channel_names = channels
        return raw_channels

class AccountChannelSelectionForm(forms.Form):
    mode = forms.ChoiceField(
        choices=AccountChannelSelection.Mode.choices,
        widget=forms.RadioSelect,
    )
    preset = forms.ModelChoiceField(
        queryset=ChannelPreset.objects.none(),
        required=False,
        empty_label="Select a preset",
    )
    custom_channels = forms.CharField(
        required=False,
        help_text="Used only in custom mode. One channel per line or comma-separated.",
        widget=forms.Textarea(
            attrs={
                "rows": 6,
                "spellcheck": "false",
                "placeholder": "channel_one\nchannel_two",
            }
        ),
    )

    def __init__(self, *args, account: MinerAccount, **kwargs):
        self.account = account
        if not args and "initial" not in kwargs:
            try:
                selection = account.selection
            except AccountChannelSelection.DoesNotExist:
                selection = None
            kwargs["initial"] = {
                "mode": selection.mode if selection else AccountChannelSelection.Mode.DEFAULT,
                "preset": selection.preset_id if selection else None,
                "custom_channels": "\n".join(
                    account.custom_channels.order_by("position").values_list("name", flat=True)
                ),
            }
        super().__init__(*args, **kwargs)
        self.fields["preset"].queryset = ChannelPreset.objects.order_by("name")

    def clean(self):
        cleaned_data = super().clean()
        mode = cleaned_data.get("mode")
        preset = cleaned_data.get("preset")
        raw_custom = cleaned_data.get("custom_channels", "")
        custom_channels = []
        if mode == AccountChannelSelection.Mode.CUSTOM:
            custom_channels = _normalize_channels(raw_custom, require_nonempty=False)

        if mode == AccountChannelSelection.Mode.PRESET and preset is None:
            self.add_error("preset", "Choose a preset for preset mode.")
        if mode == AccountChannelSelection.Mode.CUSTOM and not custom_channels:
            self.add_error("custom_channels", "Add at least one channel for custom mode.")

        cleaned_data["custom_channel_names"] = custom_channels
        if mode != AccountChannelSelection.Mode.PRESET:
            cleaned_data["preset"] = None
        return cleaned_data


class PresetAssignmentForm(forms.Form):
    accounts = AccountMultipleChoiceField(
        queryset=MinerAccount.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )

    def __init__(self, *args, preset: ChannelPreset, **kwargs):
        self.preset = preset
        if not args and "initial" not in kwargs:
            kwargs["initial"] = {
                "accounts": preset.account_selections.values_list("account_id", flat=True),
            }
        super().__init__(*args, **kwargs)
        self.fields["accounts"].queryset = MinerAccount.objects.filter(
            is_active=True,
            credential__isnull=False,
        ).order_by("display_username", "config_key")


class AccountCreateForm(forms.Form):
    config_key = forms.CharField(
        max_length=150,
        label="Account key",
        help_text="Permanent internal identifier used by imported state and audit history.",
        widget=forms.TextInput(attrs={"autocomplete": "off", "placeholder": "primary"}),
    )
    username = forms.CharField(
        max_length=100,
        label="Twitch username",
        widget=forms.TextInput(attrs={"autocomplete": "username", "placeholder": "channel_user"}),
    )
    password = forms.CharField(
        max_length=4096,
        label="Twitch password",
        strip=False,
        widget=forms.PasswordInput(
            render_value=False,
            attrs={"autocomplete": "new-password"},
        ),
    )
    mode = forms.ChoiceField(
        label="Initial channel source",
        choices=AccountChannelSelection.Mode.choices,
        widget=forms.RadioSelect,
    )
    preset = forms.ModelChoiceField(
        queryset=ChannelPreset.objects.none(),
        required=False,
        empty_label="Select a preset",
    )
    custom_channels = forms.CharField(
        required=False,
        help_text="Required only for custom mode. One channel per line or comma-separated.",
        widget=forms.Textarea(
            attrs={"rows": 6, "spellcheck": "false", "placeholder": "channel_one\nchannel_two"}
        ),
    )
    start_after_save = forms.BooleanField(
        required=False,
        label="Start after saving",
        help_text="The launch source must validate before running intent is recorded.",
    )

    def __init__(self, *args, **kwargs):
        if (not args or args[0] is None) and "initial" not in kwargs:
            kwargs["initial"] = {
                "mode": AccountChannelSelection.Mode.DEFAULT,
                "start_after_save": FarmConfiguration.load().autostart_new_accounts,
            }
        super().__init__(*args, **kwargs)
        self.fields["preset"].queryset = ChannelPreset.objects.order_by("name")

    def clean_config_key(self) -> str:
        value = self.cleaned_data["config_key"].strip()
        if MinerAccount.objects.filter(config_key__iexact=value).exists():
            raise ValidationError("An account with this key already exists.")
        return value

    def clean_username(self) -> str:
        value = self.cleaned_data["username"].strip()
        if MinerAccount.objects.filter(display_username__iexact=value).exists():
            raise ValidationError("This Twitch username is already managed.")
        return value

    def clean(self):
        cleaned_data = super().clean()
        mode = cleaned_data.get("mode")
        preset = cleaned_data.get("preset")
        custom_channels: list[str] = []
        if mode == AccountChannelSelection.Mode.PRESET and preset is None:
            self.add_error("preset", "Choose a preset for preset mode.")
        if mode == AccountChannelSelection.Mode.CUSTOM:
            try:
                custom_channels = _normalize_channels(
                    cleaned_data.get("custom_channels", ""),
                    require_nonempty=False,
                )
            except ValidationError as exc:
                self.add_error("custom_channels", exc)
            if not custom_channels and "custom_channels" not in self.errors:
                self.add_error("custom_channels", "Add at least one channel for custom mode.")
        cleaned_data["custom_channel_names"] = custom_channels
        if mode != AccountChannelSelection.Mode.PRESET:
            cleaned_data["preset"] = None
        return cleaned_data


class AccountEditForm(forms.Form):
    username = forms.CharField(
        max_length=100,
        label="Twitch username",
        widget=forms.TextInput(attrs={"autocomplete": "username"}),
    )
    password = forms.CharField(
        max_length=4096,
        required=False,
        strip=False,
        label="Replace Twitch password",
        help_text="Leave blank to keep the existing encrypted password.",
        widget=forms.PasswordInput(
            render_value=False,
            attrs={"autocomplete": "new-password"},
        ),
    )

    def __init__(self, *args, account: MinerAccount, **kwargs):
        self.account = account
        if not args and "initial" not in kwargs:
            kwargs["initial"] = {"username": account.display_username}
        super().__init__(*args, **kwargs)

    def clean_username(self) -> str:
        value = self.cleaned_data["username"].strip()
        if (
            MinerAccount.objects.filter(display_username__iexact=value)
            .exclude(pk=self.account.pk)
            .exists()
        ):
            raise ValidationError("This Twitch username is already managed.")
        return value

    def clean_password(self) -> str:
        password = self.cleaned_data.get("password", "")
        if password and not password.strip():
            raise ValidationError("Twitch password cannot contain only whitespace.")
        return password


class FarmConfigurationForm(forms.Form):
    default_channels = forms.CharField(
        label="Default channels",
        help_text="One channel per line or comma-separated.",
        widget=forms.Textarea(attrs={"rows": 8, "spellcheck": "false"}),
    )
    autostart_new_accounts = forms.BooleanField(
        required=False,
        label="Suggest starting new accounts after save",
        help_text="Preselects the start option on the add-account form; imports always stay stopped.",
    )

    def __init__(self, *args, configuration: FarmConfiguration, **kwargs):
        self.configuration = configuration
        if not args and "initial" not in kwargs:
            kwargs["initial"] = {
                "default_channels": "\n".join(configuration.default_channels),
                "autostart_new_accounts": configuration.autostart_new_accounts,
            }
        super().__init__(*args, **kwargs)

    def clean_default_channels(self) -> str:
        raw = self.cleaned_data["default_channels"]
        self.cleaned_channel_names = _normalize_channels(raw)
        return raw


class LegacyImportUploadForm(forms.Form):
    archive = forms.FileField(
        label="Legacy backup ZIP",
        help_text="Upload config.yaml, data/state.json, optional presets, and cookies together.",
        widget=forms.ClearableFileInput(attrs={"accept": ".zip,application/zip"}),
    )

    def clean_archive(self):
        upload = self.cleaned_data["archive"]
        if upload.size > 10 * 1024 * 1024:
            raise ValidationError("The ZIP must be 10 MiB or smaller.")
        if not upload.name.lower().endswith(".zip"):
            raise ValidationError("Choose a .zip backup archive.")
        return upload


class LegacyImportConfirmForm(forms.Form):
    draft_id = forms.UUIDField(widget=forms.HiddenInput)
    replace = forms.BooleanField(required=False)
    acknowledged = forms.BooleanField(required=False)
    confirmation = forms.CharField(required=False, max_length=16)

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("replace"):
            if not cleaned_data.get("acknowledged"):
                self.add_error("acknowledged", "Acknowledge the replacement effects.")
            if cleaned_data.get("confirmation", "").strip() != "REPLACE":
                self.add_error("confirmation", "Type REPLACE to confirm.")
        return cleaned_data
