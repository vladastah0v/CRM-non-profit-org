"""Команда разработчика dev_otp_code: 2FA без телефона, только при DEBUG."""

from io import StringIO

import pytest
from django.core.management import CommandError, call_command
from django.urls import reverse

from apps.accounts.models import SecureTOTPDevice
from conftest import PASSWORD

pytestmark = pytest.mark.django_db


def run(username: str) -> str:
    out = StringIO()
    call_command("dev_otp_code", username, stdout=out)
    return out.getvalue()


def test_refused_without_debug(make_user, settings):
    settings.DEBUG = False
    with pytest.raises(CommandError, match="только для разработки"):
        run(make_user("admin").username)


def test_code_logs_in_through_normal_flow(make_user, settings, client):
    settings.DEBUG = True
    user = make_user("coordinator")
    output = run(user.username)
    assert "подключена без телефона" in output
    code = output.split("Код входа: ")[1][:6]

    client.post(reverse("accounts:login"), {"username": user.username, "password": PASSWORD})
    response = client.post(reverse("accounts:otp_verify"), {"token": code})
    assert response.status_code == 302 and response["Location"] == reverse("core:home")
    assert client.get(reverse("core:home")).status_code == 200


def test_reuses_existing_device(make_user, settings):
    settings.DEBUG = True
    user = make_user("admin")
    run(user.username)
    device = SecureTOTPDevice.objects.get(user=user)
    assert "подключена" not in run(user.username)  # второй раз — только код
    assert SecureTOTPDevice.objects.get(user=user) == device


def test_unknown_user(settings):
    settings.DEBUG = True
    with pytest.raises(CommandError, match="не найден"):
        run("nobody")
