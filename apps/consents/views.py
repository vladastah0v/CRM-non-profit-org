import logging
from urllib.parse import quote

from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from apps.accounts.otp import is_verified
from apps.accounts.roles import is_staff_member
from apps.audit.models import AuditAction
from apps.audit.services import log_event
from apps.core.crypto import DecryptionError

from .files import PHOTO_TYPES, parse_download_token, read_attachment
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
