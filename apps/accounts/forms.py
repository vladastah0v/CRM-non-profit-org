from django import forms
from django.contrib.auth.forms import PasswordChangeForm

from apps.core.forms import StyledFormMixin


class StyledPasswordChangeForm(StyledFormMixin, PasswordChangeForm):
    pass


class LoginForm(forms.Form):
    username = forms.CharField(label="Логин", max_length=150)
    password = forms.CharField(label="Пароль", strip=False, widget=forms.PasswordInput)


class EnrollmentCodeForm(forms.Form):
    enrollment_code = forms.CharField(
        label="Код подключения от администратора",
        max_length=20,
        widget=forms.TextInput(attrs={"autocomplete": "off", "autocapitalize": "characters"}),
    )


class TokenForm(forms.Form):
    token = forms.RegexField(
        label="Код из приложения",
        regex=r"^\d{6}$",
        error_messages={"invalid": "Введите 6 цифр из приложения-аутентификатора."},
        widget=forms.TextInput(
            attrs={"inputmode": "numeric", "autocomplete": "one-time-code", "maxlength": "6"}
        ),
    )
