"""Forms for the staff-only Twitch farm control room."""

from __future__ import annotations

from django import forms
from django.contrib.auth.forms import AuthenticationForm
from django.core.exceptions import ValidationError

from .models import (
    AccountChannelSelection,
    ChannelPreset,
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
        status = "configured" if account.is_configured else "config missing"
        return f"{account.config_key} — {account.display_username} ({status})"


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
        self.fields["accounts"].queryset = MinerAccount.objects.order_by("config_key")
