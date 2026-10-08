"""Экраны модуля «Подопечные». Здесь только разбор запроса: права — в выборках,
изменения и правила — в сервисах."""

from __future__ import annotations

from typing import cast

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from django.views.decorators.vary import vary_on_headers

from apps.accounts.models import User
from apps.audit.models import AuditAction
from apps.audit.services import log_event
from apps.consents.files import make_download_url
from apps.consents.forms import ConsentForm, RevokeConsentForm
from apps.consents.models import Consent
from apps.consents.services import create_beneficiary_consent
from apps.core.list_filters import RESET_PARAM, restore_or_save
from apps.core.pages import action_page, apply_service_errors
from apps.interactions.selectors import card_activity
from apps.requests.selectors import beneficiary_help

from . import permissions, selectors, services
from .filters import SAVED_PARAMS
from .forms import (
    BeneficiaryForm,
    CuratorForm,
    DocumentForm,
    GrantAccessForm,
    RelatedPersonForm,
    SensitiveForm,
    StatusChangeForm,
)
from .models import Beneficiary, CaseStatus, CuratorAccess, RelatedPerson

LIST_KEY = "beneficiaries"
PAGE_SIZE = 25


def _detail(beneficiary: Beneficiary) -> HttpResponse:
    return redirect("beneficiaries:detail", pk=beneficiary.pk)


def _editable(request: HttpRequest, pk: str) -> tuple[Beneficiary, HttpResponse | None]:
    """Карточка для изменения; архивная — только для чтения (кроме отзыва согласия)."""
    beneficiary = selectors.get_managed_beneficiary(request.user, pk)
    if beneficiary.is_archived:
        messages.error(request, "Карточка в архиве — сначала верните её из архива.")
        return beneficiary, _detail(beneficiary)
    return beneficiary, None


# ---------------------------------------------------------------------------
# Список
# ---------------------------------------------------------------------------
@login_required
@require_GET
@vary_on_headers("HX-Request")
def beneficiary_list(request: HttpRequest) -> HttpResponse:
    if not permissions.can_view_list(request.user):
        raise PermissionDenied
    htmx = getattr(request, "htmx", None)
    is_htmx = htmx is not None and bool(htmx) and not htmx.history_restore_request
    if not is_htmx:
        saved = restore_or_save(request, LIST_KEY, SAVED_PARAMS)
        if saved:
            return redirect(f"{reverse('beneficiaries:list')}?{saved}")
    elif request.GET:
        restore_or_save(request, LIST_KEY, SAVED_PARAMS)

    beneficiary_filter = selectors.beneficiary_list(request.user, request.GET)
    page = Paginator(beneficiary_filter.qs, PAGE_SIZE).get_page(request.GET.get("page"))
    context = {
        "filter": beneficiary_filter,
        "page": page,
        "full": beneficiary_filter.full_access,
        "can_create": permissions.can_create(request.user),
        "reset_param": RESET_PARAM,
    }
    template = "beneficiaries/_results.html" if is_htmx else "beneficiaries/list.html"
    return render(request, template, context)


# ---------------------------------------------------------------------------
# Карточка
# ---------------------------------------------------------------------------
@login_required
@require_GET
def beneficiary_detail(request: HttpRequest, pk: str) -> HttpResponse:
    if not permissions.can_view_list(request.user):
        raise PermissionDenied
    beneficiary = selectors.get_beneficiary(request.user, pk)
    card = selectors.beneficiary_card(request.user, beneficiary)
    scan_urls = {
        consent.pk: make_download_url(consent.scan, request.user)
        for consent in card.consents
        if consent.scan is not None
    }
    return render(
        request,
        "beneficiaries/detail.html",
        {
            "b": beneficiary,
            "card": card,
            "scan_urls": scan_urls,
            "help": beneficiary_help(request.user, beneficiary),
            "activity": card_activity(request.user, beneficiary),
        },
    )


def _form_page(
    request: HttpRequest,
    form: BeneficiaryForm,
    *,
    beneficiary: Beneficiary | None = None,
    duplicates: list | None = None,
) -> HttpResponse:
    return render(
        request,
        "beneficiaries/form.html",
        {"form": form, "b": beneficiary, "duplicates": duplicates or []},
    )


@login_required
@require_http_methods(["GET", "POST"])
def beneficiary_create(request: HttpRequest) -> HttpResponse:
    if not permissions.can_create(request.user):
        raise PermissionDenied
    choose_curator = permissions.is_admin(request.user)
    form = BeneficiaryForm(request.POST or None, choose_curator=choose_curator)
    duplicates: list = []
    if request.method == "POST" and form.is_valid():
        curator = form.cleaned_data.get("curator") if choose_curator else cast(User, request.user)
        try:
            beneficiary = services.create_beneficiary(
                form.service_data(),
                curator=curator,
                programs=form.cleaned_data["programs"],
                confirm_duplicates=form.cleaned_data["confirm_duplicates"],
                request=request,
            )
        except services.DuplicateBeneficiaryError as exc:
            # Чужие карточки не раскрываются: только факт совпадения и к кому обратиться
            visible = set(
                Beneficiary.objects.visible_to(request.user)
                .filter(pk__in=[m.person.pk for m in exc.matches])
                .values_list("pk", flat=True)
            )
            duplicates = [(m, m.person.pk in visible) for m in exc.matches]
            form.add_error(
                None,
                "Найдены похожие карточки. Проверьте их; если это другой человек, "
                "отметьте «всё равно сохранить» внизу формы.",
            )
        except ValidationError as exc:
            apply_service_errors(form, exc)
        else:
            messages.success(request, "Карточка подопечного создана.")
            return _detail(beneficiary)
    return _form_page(request, form, duplicates=duplicates)


@login_required
@require_http_methods(["GET", "POST"])
def beneficiary_edit(request: HttpRequest, pk: str) -> HttpResponse:
    beneficiary, blocked = _editable(request, pk)
    if blocked:
        return blocked
    # Форма работает со своей копией: сервис сравнивает новые значения со старыми
    form_instance = Beneficiary.objects.get(pk=beneficiary.pk)
    form = BeneficiaryForm(request.POST or None, instance=form_instance)
    if request.method == "POST" and form.is_valid():
        try:
            services.update_beneficiary(
                beneficiary,
                form.service_data(),
                programs=form.cleaned_data["programs"],
                request=request,
            )
        except ValidationError as exc:
            apply_service_errors(form, exc)
        else:
            messages.success(request, "Изменения сохранены.")
            return _detail(beneficiary)
    return _form_page(request, form, beneficiary=beneficiary)


@login_required
@require_http_methods(["GET", "POST"])
def beneficiary_status(request: HttpRequest, pk: str) -> HttpResponse:
    """ФТ-П-04: смена статуса случая с проверкой перехода в сервисе."""
    beneficiary, blocked = _editable(request, pk)
    if blocked:
        return blocked
    transitions = services.allowed_transitions(beneficiary)
    if not transitions:
        messages.error(request, "Случай закрыт. Повторное обращение — новая карточка.")
        return _detail(beneficiary)
    form = StatusChangeForm(request.POST or None, transitions=transitions)
    if request.method == "POST" and form.is_valid():
        try:
            services.change_status(beneficiary, **form.cleaned_data, request=request)
        except ValidationError as exc:
            apply_service_errors(form, exc)
        else:
            messages.success(request, f"Статус изменён: «{beneficiary.get_status_display()}».")
            return _detail(beneficiary)
    problems = services.in_work_problems(beneficiary) if CaseStatus.IN_WORK in transitions else []
    return action_page(
        request,
        form,
        subject=beneficiary,
        title="Смена статуса случая",
        submit="Сменить статус",
        notes=problems,
        notes_title="Перевод в «В работе» сейчас невозможен:",
    )


# ---------------------------------------------------------------------------
# Защищённый блок (ФТ-П-03): каждое открытие — в журнал аудита
# ---------------------------------------------------------------------------
@never_cache
@login_required
@require_GET
def sensitive_detail(request: HttpRequest, pk: str) -> HttpResponse:
    beneficiary = selectors.get_managed_beneficiary(request.user, pk)
    sensitive = services.get_sensitive(beneficiary)
    documents = list(beneficiary.documents.filter(is_archived=False).select_related("file"))
    log_event(AuditAction.VIEW_SENSITIVE, request=request, obj=beneficiary)
    return render(
        request,
        "beneficiaries/sensitive.html",
        {
            "b": beneficiary,
            "sensitive": sensitive,
            "documents": [(d, make_download_url(d.file, request.user)) for d in documents],
        },
    )


@never_cache
@login_required
@require_http_methods(["GET", "POST"])
def sensitive_edit(request: HttpRequest, pk: str) -> HttpResponse:
    beneficiary, blocked = _editable(request, pk)
    if blocked:
        return blocked
    sensitive = services.get_sensitive(beneficiary)
    initial = {key: getattr(sensitive, key) for key in services.SENSITIVE_FIELDS}
    form = SensitiveForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        try:
            services.save_sensitive(beneficiary, form.cleaned_data, request=request)
        except ValidationError as exc:
            apply_service_errors(form, exc)
        else:
            messages.success(request, "Защищённые данные сохранены.")
            return redirect("beneficiaries:sensitive", pk=beneficiary.pk)
    else:
        log_event(AuditAction.VIEW_SENSITIVE, request=request, obj=beneficiary)
    return action_page(
        request, form, subject=beneficiary, title="Защищённые данные", submit="Сохранить"
    )


@login_required
@require_http_methods(["GET", "POST"])
def document_add(request: HttpRequest, pk: str) -> HttpResponse:
    beneficiary, blocked = _editable(request, pk)
    if blocked:
        return blocked
    form = DocumentForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            services.add_document(beneficiary, **form.cleaned_data, request=request)
        except ValidationError as exc:
            apply_service_errors(form, exc)
        else:
            messages.success(request, "Документ загружен.")
            return redirect("beneficiaries:sensitive", pk=beneficiary.pk)
    return action_page(
        request, form, subject=beneficiary, title="Документ", submit="Загрузить", multipart=True
    )


# ---------------------------------------------------------------------------
# Связанные лица (ФТ-П-02)
# ---------------------------------------------------------------------------
@login_required
@require_http_methods(["GET", "POST"])
def related_add(request: HttpRequest, pk: str) -> HttpResponse:
    beneficiary, blocked = _editable(request, pk)
    if blocked:
        return blocked
    form = RelatedPersonForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            services.add_related_person(beneficiary, form.cleaned_data, request=request)
        except ValidationError as exc:
            apply_service_errors(form, exc)
        else:
            messages.success(request, "Связанное лицо добавлено.")
            return _detail(beneficiary)
    return action_page(
        request, form, subject=beneficiary, title="Связанное лицо", submit="Добавить"
    )


def _related_person(beneficiary: Beneficiary, person_pk: str) -> RelatedPerson:
    return get_object_or_404(
        RelatedPerson.objects.filter(beneficiary=beneficiary, is_archived=False), pk=person_pk
    )


@login_required
@require_http_methods(["GET", "POST"])
def related_edit(request: HttpRequest, pk: str, person_pk: str) -> HttpResponse:
    beneficiary, blocked = _editable(request, pk)
    if blocked:
        return blocked
    person = _related_person(beneficiary, person_pk)
    form = RelatedPersonForm(request.POST or None, instance=RelatedPerson.objects.get(pk=person.pk))
    if request.method == "POST" and form.is_valid():
        try:
            services.update_related_person(person, form.cleaned_data, request=request)
        except ValidationError as exc:
            apply_service_errors(form, exc)
        else:
            messages.success(request, "Изменения сохранены.")
            return _detail(beneficiary)
    return action_page(
        request, form, subject=beneficiary, title="Связанное лицо", submit="Сохранить"
    )


@login_required
@require_POST
def related_archive(request: HttpRequest, pk: str, person_pk: str) -> HttpResponse:
    beneficiary, blocked = _editable(request, pk)
    if blocked:
        return blocked
    services.archive_related_person(_related_person(beneficiary, person_pk), request=request)
    messages.success(request, "Связанное лицо убрано из карточки.")
    return _detail(beneficiary)


# ---------------------------------------------------------------------------
# Согласия (ФТ-Д-01…03)
# ---------------------------------------------------------------------------
def _representative_name(beneficiary: Beneficiary) -> str:
    person = beneficiary.related_persons.filter(
        is_legal_representative=True, is_archived=False
    ).first()
    return person.full_name if person else ""


@login_required
@require_http_methods(["GET", "POST"])
def consent_add(request: HttpRequest, pk: str) -> HttpResponse:
    beneficiary, blocked = _editable(request, pk)
    if blocked:
        return blocked
    form = ConsentForm(
        request.POST or None,
        request.FILES or None,
        minor=beneficiary.is_minor,
        representative_name=_representative_name(beneficiary),
        kind_help="Для данных о здоровье и инвалидности нужно отдельное согласие "
        "на особые категории данных.",
    )
    if request.method == "POST" and form.is_valid():
        try:
            create_beneficiary_consent(beneficiary, **form.cleaned_data, request=request)
        except ValidationError as exc:
            apply_service_errors(form, exc)
        else:
            messages.success(request, "Согласие добавлено.")
            return _detail(beneficiary)
    return action_page(
        request,
        form,
        subject=beneficiary,
        title="Согласие на обработку данных",
        submit="Добавить",
        multipart=True,
    )


@login_required
@require_http_methods(["GET", "POST"])
def consent_revoke(request: HttpRequest, pk: str, consent_pk: str) -> HttpResponse:
    beneficiary = selectors.get_managed_beneficiary(request.user, pk)
    consent = get_object_or_404(
        Consent.objects.visible_to(request.user).filter(beneficiary=beneficiary), pk=consent_pk
    )
    if consent.revoked_on is not None:
        messages.info(request, "Согласие уже отозвано.")
        return _detail(beneficiary)
    form = RevokeConsentForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            services.revoke_beneficiary_consent(consent, **form.cleaned_data, request=request)
        except ValidationError as exc:
            apply_service_errors(form, exc)
        else:
            messages.warning(
                request,
                "Отзыв согласия зафиксирован. Обработку данных нужно прекратить; "
                "обезличивание выполняет администратор.",
            )
            return _detail(beneficiary)
    return action_page(
        request,
        form,
        subject=beneficiary,
        title="Отзыв согласия",
        submit="Зафиксировать отзыв",
        intro=f"{consent} ({consent.get_signer_display().lower()}).",
    )


# ---------------------------------------------------------------------------
# Кураторы (п. 2.3): назначает и открывает доступ руководитель
# ---------------------------------------------------------------------------
def _admin_beneficiary(request: HttpRequest, pk: str) -> Beneficiary:
    if not permissions.is_admin(request.user):
        raise PermissionDenied
    return selectors.get_beneficiary(request.user, pk)


@login_required
@require_http_methods(["GET", "POST"])
def curators_manage(request: HttpRequest, pk: str) -> HttpResponse:
    beneficiary = _admin_beneficiary(request, pk)
    if beneficiary.is_archived:
        messages.error(request, "Карточка в архиве — сначала верните её из архива.")
        return _detail(beneficiary)
    action = request.POST.get("action") if request.method == "POST" else None
    curator_form = CuratorForm(
        request.POST if action == "assign" else None,
        initial={"curator": beneficiary.curator_id},
    )
    grant_form = GrantAccessForm(
        request.POST if action == "grant" else None, beneficiary=beneficiary
    )
    form = curator_form if action == "assign" else grant_form
    if action in {"assign", "grant"} and form.is_valid():
        try:
            if action == "assign":
                services.assign_curator(
                    beneficiary, curator_form.cleaned_data["curator"], request=request
                )
                messages.success(request, "Ответственный куратор сохранён.")
            else:
                services.grant_access(beneficiary, grant_form.cleaned_data["user"], request=request)
                messages.success(request, "Доступ к случаю открыт.")
        except ValidationError as exc:
            apply_service_errors(form, exc)
        else:
            return redirect("beneficiaries:curators", pk=beneficiary.pk)
    shared = CuratorAccess.objects.filter(beneficiary=beneficiary, is_archived=False)
    return render(
        request,
        "beneficiaries/curators.html",
        {
            "b": beneficiary,
            "curator_form": curator_form,
            "grant_form": grant_form,
            "shared": shared.select_related("user", "created_by"),
        },
    )


@login_required
@require_POST
def access_revoke(request: HttpRequest, pk: str, access_pk: str) -> HttpResponse:
    beneficiary = _admin_beneficiary(request, pk)
    access = get_object_or_404(
        CuratorAccess.objects.filter(beneficiary=beneficiary, is_archived=False), pk=access_pk
    )
    services.revoke_access(access, request=request)
    messages.success(request, "Доступ к случаю закрыт.")
    return redirect("beneficiaries:curators", pk=beneficiary.pk)


# ---------------------------------------------------------------------------
# Архив
# ---------------------------------------------------------------------------
@login_required
@require_POST
def beneficiary_archive(request: HttpRequest, pk: str) -> HttpResponse:
    beneficiary = selectors.get_managed_beneficiary(request.user, pk)
    services.archive_beneficiary(beneficiary, request=request)
    messages.success(request, "Карточка переведена в архив.")
    return _detail(beneficiary)


@login_required
@require_POST
def beneficiary_restore(request: HttpRequest, pk: str) -> HttpResponse:
    beneficiary = selectors.get_managed_beneficiary(request.user, pk)
    services.restore_beneficiary(beneficiary, request=request)
    messages.success(request, "Карточка возвращена из архива.")
    return _detail(beneficiary)
