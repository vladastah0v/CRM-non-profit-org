from typing import Any

from django import forms

from apps.core.forms import DateInput, StyledFormMixin

from .models import ConsentKind, ConsentSigner


class ConsentForm(StyledFormMixin, forms.Form):
    """ФТ-Д-01: тип, дата подписания, кто подписал, скан, срок действия."""

    kind = forms.ChoiceField(label="Вид согласия", choices=ConsentKind.choices)
    signed_on = forms.DateField(label="Дата подписания", widget=DateInput())
    signer = forms.ChoiceField(label="Кто подписал", choices=ConsentSigner.choices)
    signer_name = forms.CharField(
        label="ФИО законного представителя",
        max_length=300,
        required=False,
        help_text="Если согласие подписал представитель.",
    )
    valid_until = forms.DateField(
        label="Действует до",
        required=False,
        widget=DateInput(),
        help_text="Пусто — бессрочно до отзыва.",
    )
    scan = forms.FileField(
        label="Скан",
        required=False,
        help_text="PDF или фото, до 20 МБ.",
        widget=forms.ClearableFileInput(attrs={"accept": "application/pdf,image/*"}),
    )

    def __init__(
        self,
        *args: Any,
        minor: bool = False,
        representative_name: str = "",
        kind_help: str = "",
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        if minor:
            # За несовершеннолетнего согласие подписывает законный представитель
            self.initial.setdefault("signer", ConsentSigner.REPRESENTATIVE)
            self.initial.setdefault("signer_name", representative_name)
        if kind_help:
            self.fields["kind"].help_text = kind_help

    def clean(self) -> dict[str, Any]:
        super().clean()
        data = self.cleaned_data
        if data.get("signer") == ConsentSigner.REPRESENTATIVE and not data.get("signer_name"):
            self.add_error("signer_name", "Укажите ФИО законного представителя.")
        return data


class RevokeConsentForm(StyledFormMixin, forms.Form):
    revoked_on = forms.DateField(label="Дата отзыва", widget=DateInput())
    note = forms.CharField(label="Комментарий", max_length=500, required=False)
