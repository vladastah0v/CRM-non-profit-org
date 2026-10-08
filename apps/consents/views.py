import logging
from typing import Any
from urllib.parse import quote

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods

from apps.accounts.otp import is_verified
from apps.accounts.roles import RoleCode, is_staff_member, user_has_role
from apps.audit.models import AuditAction
from apps.audit.services import log_event
from apps.beneficiaries.models import Beneficiary
from apps.core.crypto import DecryptionError
from apps.core.pages import action_page, apply_service_errors
from apps.core.xlsx import build_workbook, xlsx_response
from apps.volunteers.models import Volunteer

from . import anonymize
from . import subject_export as subject_export_module
from .files import PHOTO_TYPES, parse_download_token, read_attachment
from .forms import AnonymizeForm, DismissErasureForm
from .models import Attachment

logger = logging.getLogger(__name__)

try:  # ошибки S3 (boto3) не наследуют OSError
    from botocore.exceptions import BotoCoreError, ClientError

    STORAGE_ERRORS: tuple[type[Exception], ...] = (BotoCoreError, ClientError)
except ImportError:  # pragma: no cover
    STORAGE_ERRORS = ()

READ_ERRORS: tuple[type[Exception], ...] = (DecryptionError, OSError, *STORAGE_ERRORS)


@never_cache
@require_GET
@login_required
def download(request: HttpRequest, token: str) -> HttpResponse:
    # Права проверяются заново при каждом скачивании (НФТ-ИБ-08)
    if not (is_verified(request.user) and is_staff_member(request.user)):
        raise PermissionDenied
    try:
        payload = parse_download_token(token)
    except signing.SignatureExpired:
        return render(request, "errors/link_expired.html", status=410)
    except signing.BadSignature as exc:
        raise Http404 from exc
    if payload.get("u") != str(request.user.pk):
        raise Http404

    try:
        # Имя файла расшифровывается уже при загрузке записи — тоже внутри try
        attachment = get_object_or_404(Attachment.objects.visible_to(request.user), pk=payload["a"])
        data = read_attachment(attachment)
    except Http404:
        raise
    except READ_ERRORS:
        # Нарушение целостности или потеря файла — сигнал для разработчика, без ПДн в логе
        logger.exception("Файл %s не читается", payload["a"])
        log_event(
            AuditAction.FILE_DOWNLOAD,
            request=request,
            object_type="consents.attachment",
            object_id=str(payload["a"]),
            extra={"failed": True},
        )
        return render(request, "errors/file_unavailable.html", status=500)
    inline = payload.get("i") == "1" and attachment.content_type in PHOTO_TYPES
    log_event(AuditAction.FILE_DOWNLOAD, request=request, obj=attachment, extra={"inline": inline})

    response = HttpResponse(data, content_type=attachment.content_type)
    filename = quote(attachment.original_name)
    disposition = "inline" if inline else "attachment"
    response["Content-Disposition"] = f"{disposition}; filename*=UTF-8''{filename}"
    response["Cache-Control"] = "no-store, private"
    response["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return response


# ---------------------------------------------------------------------------
# Обезличивание (ФТ-А-04, ФТ-Д-03) — только администратор
# ---------------------------------------------------------------------------
PERSON_MODELS: dict[str, Any] = {"volunteer": Volunteer, "beneficiary": Beneficiary}


def _require_admin(request: HttpRequest) -> None:
    if not user_has_role(request.user, RoleCode.ADMIN):
        raise PermissionDenied


def _person(request: HttpRequest, kind: str, pk: str) -> Any:
    _require_admin(request)
    model = PERSON_MODELS.get(kind)
    if model is None:
        raise Http404
    return get_object_or_404(model.objects.visible_to(request.user), pk=pk)


@login_required
@require_GET
def erasure_list(request: HttpRequest) -> HttpResponse:
    _require_admin(request)
    today = timezone.localdate()
    pending = [
        {"person": p, "kind": "volunteer" if isinstance(p, Volunteer) else "beneficiary",
         "deadline": anonymize.deadline(p)}
        for p in anonymize.pending_requests()
    ]  # fmt: skip
    anonymized: list[Volunteer | Beneficiary] = [
        *Volunteer.objects.filter(anonymized_at__isnull=False).order_by("-anonymized_at")[:20],
        *Beneficiary.objects.filter(anonymized_at__isnull=False).order_by("-anonymized_at")[:20],
    ]
    done = sorted(anonymized, key=lambda p: p.anonymized_at or today, reverse=True)[:20]
    return render(
        request, "consents/erasure_list.html", {"pending": pending, "done": done, "today": today}
    )


@login_required
@require_http_methods(["GET", "POST"])
def anonymize_person(request: HttpRequest, kind: str, pk: str) -> HttpResponse:
    person = _person(request, kind, pk)
    if person.anonymized_at is not None:
        messages.error(request, "Данные уже обезличены.")
        return redirect(person)
    initial = {"basis": "revocation"} if person.erasure_requested_on else {}
    form = AnonymizeForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        service = (
            anonymize.anonymize_volunteer
            if kind == "volunteer"
            else anonymize.anonymize_beneficiary
        )
        try:
            service(
                person,
                basis=form.cleaned_data["basis"],
                document=form.cleaned_data["document"],
                request=request,
            )
        except ValidationError as exc:
            apply_service_errors(form, exc)
        else:
            messages.success(
                request, "Данные обезличены. Карточка осталась в архиве для статистики."
            )
            return redirect(person)
    what = "волонтёра" if kind == "volunteer" else "подопечного"
    notes = [
        "ФИО заменятся на «Обезличено», от даты рождения останется только год.",
        "Будут стёрты контакты, адрес, Telegram, данные представителей и связанных лиц.",
        "Будут удалены файлы: фото, сканы согласий, документы"
        + (", защищённый блок (здоровье, документы)." if kind == "beneficiary" else "."),
        "Тексты заявок, взаимодействий, задач и комментарии будут заменены.",
        "Сохранятся для статистики: факты помощи, часы, отметки явки, статусы.",
    ]
    return action_page(
        request,
        form,
        subject=person,
        title=f"Обезличивание данных {what}",
        submit="Обезличить безвозвратно",
        notes=notes,
        notes_title="Что произойдёт:",
    )


@login_required
@require_http_methods(["GET", "POST"])
def dismiss_erasure(request: HttpRequest, kind: str, pk: str) -> HttpResponse:
    person = _person(request, kind, pk)
    if person.erasure_requested_on is None:
        messages.error(request, "Запроса на обезличивание нет.")
        return redirect(person)
    form = DismissErasureForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            anonymize.dismiss_erasure(person, reason=form.cleaned_data["reason"], request=request)
        except ValidationError as exc:
            apply_service_errors(form, exc)
        else:
            messages.success(request, "Запрос на обезличивание снят.")
            return redirect(person)
    return action_page(
        request,
        form,
        subject=person,
        title="Снять запрос на обезличивание",
        submit="Снять запрос",
        intro="Снимайте запрос, только если у фонда есть иное законное основание хранить данные "
        "(ч. 5 ст. 21 152-ФЗ). Решение записывается в журнал аудита.",
    )


@login_required
@require_GET
@never_cache
def subject_export(request: HttpRequest, kind: str, pk: str) -> HttpResponse:
    """НФТ-ПД-05: все данные человека по его запросу — одной книгой Excel."""
    person = _person(request, kind, pk)
    if kind == "volunteer":
        sheets = subject_export_module.volunteer_sheets(person)
    else:
        sheets = subject_export_module.beneficiary_sheets(person)
        log_event(AuditAction.VIEW_SENSITIVE, request=request, obj=person, extra={"via": "export"})
    log_event(
        AuditAction.EXPORT,
        request=request,
        obj=person,
        extra={"export": "subject_request", "sheets": len(sheets)},
    )
    return xlsx_response(build_workbook(sheets), "dannye-cheloveka")
