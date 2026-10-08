"""Экраны модуля «Волонтёры». Здесь только разбор запроса: права — в выборках,
изменения и правила — в сервисах."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from django.views.decorators.vary import vary_on_headers

from apps.consents.files import make_download_url
from apps.consents.forms import ConsentForm, RevokeConsentForm
from apps.consents.models import Consent
from apps.consents.services import create_volunteer_consent, revoke_consent
from apps.core.list_filters import RESET_PARAM, restore_or_save
from apps.core.pages import action_page, apply_service_errors
from apps.interactions.selectors import card_activity
from apps.shifts.selectors import volunteer_shifts

from . import permissions, selectors, services
from .filters import SAVED_PARAMS
from .forms import (
    BaseAvailabilityFormSet,
    HoursForm,
    StatusChangeForm,
    TrainingForm,
    VolunteerForm,
    availability_formset,
    availability_initial,
)
from .models import Volunteer, VolunteerStatus

LIST_KEY = "volunteers"
PAGE_SIZE = 25


def _require_view(request: HttpRequest) -> None:
    if not permissions.can_view(request.user):
        raise PermissionDenied


def _require_manage(request: HttpRequest) -> None:
    if not permissions.can_manage(request.user):
        raise PermissionDenied


def _editable_volunteer(request: HttpRequest, pk: str) -> tuple[Volunteer, HttpResponse | None]:
    """Карточка для изменения. Архивная — только для чтения: возврат в карточку с пояснением.

    Отзыв согласия этой проверки не проходит: человек вправе отозвать согласие и после
    выбытия, а отзыв нужно зафиксировать.
    """
    volunteer = selectors.get_managed_volunteer(request.user, pk)
    if volunteer.is_archived:
        messages.error(request, "Карточка в архиве — сначала верните её из архива.")
        return volunteer, redirect("volunteers:detail", pk=volunteer.pk)
    return volunteer, None


# ---------------------------------------------------------------------------
# Список (ФТ-В-05)
# ---------------------------------------------------------------------------
@login_required
@require_GET
@vary_on_headers("HX-Request")  # фрагмент и полная страница по одному адресу — разный кэш
def volunteer_list(request: HttpRequest) -> HttpResponse:
    _require_view(request)
    htmx = getattr(request, "htmx", None)
    # При возврате «Назад» HTMX запрашивает страницу целиком
    is_htmx = htmx is not None and bool(htmx) and not htmx.history_restore_request
    if not is_htmx:
        saved = restore_or_save(request, LIST_KEY, SAVED_PARAMS)
        if saved:
            return redirect(f"{reverse('volunteers:list')}?{saved}")
    elif request.GET:
        restore_or_save(request, LIST_KEY, SAVED_PARAMS)

    volunteer_filter = selectors.volunteer_list(request.user, request.GET)
    page = Paginator(volunteer_filter.qs, PAGE_SIZE).get_page(request.GET.get("page"))
    context = {
        "filter": volunteer_filter,
        "page": page,
        "full": permissions.sees_full_card(request.user),
        "can_manage": permissions.can_manage(request.user),
        "reset_param": RESET_PARAM,
    }
    template = "volunteers/_results.html" if is_htmx else "volunteers/list.html"
    return render(request, template, context)


# ---------------------------------------------------------------------------
# Карточка
# ---------------------------------------------------------------------------
@login_required
@require_GET
def volunteer_detail(request: HttpRequest, pk: str) -> HttpResponse:
    _require_view(request)
    volunteer = selectors.get_volunteer(request.user, pk)
    card = selectors.volunteer_card(request.user, volunteer)
    photo_url = None
    if card.full and volunteer.photo is not None:
        photo_url = make_download_url(volunteer.photo, request.user, inline=True)
    scan_urls = {
        consent.pk: make_download_url(consent.scan, request.user)
        for consent in card.consents
        if consent.scan is not None
    }
    return render(
        request,
        "volunteers/detail.html",
        {
            "v": volunteer,
            "card": card,
            "photo_url": photo_url,
            "scan_urls": scan_urls,
            "activity": card_activity(request.user, volunteer),
            "shifts": volunteer_shifts(request.user, volunteer),
        },
    )


def _volunteer_form_page(
    request: HttpRequest,
    form: VolunteerForm,
    formset: BaseAvailabilityFormSet,
    *,
    volunteer: Volunteer | None = None,
    duplicates: list[services.DuplicateMatch] | None = None,
) -> HttpResponse:
    return render(
        request,
        "volunteers/form.html",
        {"form": form, "formset": formset, "v": volunteer, "duplicates": duplicates or []},
    )


@login_required
@require_http_methods(["GET", "POST"])
def volunteer_create(request: HttpRequest) -> HttpResponse:
    _require_manage(request)
    if request.method != "POST":
        return _volunteer_form_page(request, VolunteerForm(), availability_formset())

    form = VolunteerForm(request.POST, request.FILES)
    formset = availability_formset(request.POST)
    duplicates: list[services.DuplicateMatch] = []
    if form.is_valid() and formset.is_valid():
        try:
            volunteer = services.create_volunteer(
                form.service_data(),
                skills=form.cleaned_data["skills"],
                programs=form.cleaned_data["programs"],
                availability=formset.slots(),
                photo=form.cleaned_data.get("photo_upload"),
                confirm_duplicates=form.cleaned_data["confirm_duplicates"],
                request=request,
            )
        except services.DuplicateVolunteerError as exc:
            duplicates = exc.matches
            form.add_error(
                None,
                "Найдены похожие карточки. Проверьте их; если это другой человек, "
                "отметьте «всё равно сохранить» внизу формы.",
            )
        except ValidationError as exc:
            apply_service_errors(form, exc)
        else:
            messages.success(request, "Карточка волонтёра создана.")
            return redirect("volunteers:detail", pk=volunteer.pk)
    return _volunteer_form_page(request, form, formset, duplicates=duplicates)


@login_required
@require_http_methods(["GET", "POST"])
def volunteer_edit(request: HttpRequest, pk: str) -> HttpResponse:
    volunteer, blocked = _editable_volunteer(request, pk)
    if blocked:
        return blocked
    # Форма работает со своей копией: сервис сравнивает новые значения со старыми
    form_instance = Volunteer.objects.get(pk=volunteer.pk)
    if request.method != "POST":
        form = VolunteerForm(instance=form_instance)
        formset = availability_formset(initial=availability_initial(volunteer))
        return _volunteer_form_page(request, form, formset, volunteer=volunteer)

    form = VolunteerForm(request.POST, request.FILES, instance=form_instance)
    formset = availability_formset(request.POST)
    if form.is_valid() and formset.is_valid():
        try:
            services.update_volunteer(
                volunteer,
                form.service_data(),
                skills=form.cleaned_data["skills"],
                programs=form.cleaned_data["programs"],
                availability=formset.slots(),
                photo=form.cleaned_data.get("photo_upload"),
                remove_photo=form.cleaned_data["remove_photo"],
                request=request,
            )
        except ValidationError as exc:
            apply_service_errors(form, exc)
        else:
            messages.success(request, "Изменения сохранены.")
            return redirect("volunteers:detail", pk=volunteer.pk)
    return _volunteer_form_page(request, form, formset, volunteer=volunteer)


# ---------------------------------------------------------------------------
# Действия с карточкой: статус, обучение, часы, согласия, архив
# ---------------------------------------------------------------------------
@login_required
@require_http_methods(["GET", "POST"])
def volunteer_status(request: HttpRequest, pk: str) -> HttpResponse:
    """ФТ-В-02: смена статуса с проверкой перехода в сервисе."""
    volunteer, blocked = _editable_volunteer(request, pk)
    if blocked:
        return blocked
    transitions = services.allowed_transitions(volunteer)
    if not transitions:
        messages.error(request, "Из статуса «Выбыл» переходов нет: вернуть можно новой анкетой.")
        return redirect("volunteers:detail", pk=volunteer.pk)

    form = StatusChangeForm(request.POST or None, transitions=transitions)
    if request.method == "POST" and form.is_valid():
        try:
            services.change_status(
                volunteer,
                form.cleaned_data["to_status"],
                reason=form.cleaned_data["reason"],
                comment=form.cleaned_data["comment"],
                request=request,
            )
        except ValidationError as exc:
            apply_service_errors(form, exc)
        else:
            messages.success(request, f"Статус изменён: «{volunteer.get_status_display()}».")
            return redirect("volunteers:detail", pk=volunteer.pk)
    problems = []
    if VolunteerStatus.ACTIVE in transitions:
        problems = services.activation_problems(volunteer)
    return action_page(
        request,
        form,
        subject=volunteer,
        title="Смена статуса",
        submit="Сменить статус",
        notes=problems,
        notes_title="Перевод в «Активен» сейчас невозможен:",
    )


@login_required
@require_http_methods(["GET", "POST"])
def training_add(request: HttpRequest, pk: str) -> HttpResponse:
    """ФТ-В-06: пройденное обучение с датой."""
    volunteer, blocked = _editable_volunteer(request, pk)
    if blocked:
        return blocked
    form = TrainingForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            services.add_training(volunteer, **form.cleaned_data, request=request)
        except ValidationError as exc:
            apply_service_errors(form, exc)
        else:
            messages.success(request, "Обучение отмечено.")
            return redirect("volunteers:detail", pk=volunteer.pk)
    return action_page(
        request, form, subject=volunteer, title="Пройденное обучение", submit="Добавить"
    )


@login_required
@require_http_methods(["GET", "POST"])
def hours_add(request: HttpRequest, pk: str) -> HttpResponse:
    """ФТ-В-07: часы вручную — с комментарием."""
    volunteer, blocked = _editable_volunteer(request, pk)
    if blocked:
        return blocked
    form = HoursForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            services.add_manual_hours(volunteer, **form.cleaned_data, request=request)
        except ValidationError as exc:
            apply_service_errors(form, exc)
        else:
            messages.success(request, "Часы добавлены.")
            return redirect("volunteers:detail", pk=volunteer.pk)
    return action_page(request, form, subject=volunteer, title="Часы вручную", submit="Добавить")


@login_required
@require_http_methods(["GET", "POST"])
def consent_add(request: HttpRequest, pk: str) -> HttpResponse:
    """ФТ-Д-01: согласие волонтёра (для несовершеннолетнего — от представителя, ФТ-В-08)."""
    volunteer, blocked = _editable_volunteer(request, pk)
    if blocked:
        return blocked
    form = ConsentForm(
        request.POST or None,
        request.FILES or None,
        minor=volunteer.is_minor,
        representative_name=volunteer.guardian_name,
    )
    if request.method == "POST" and form.is_valid():
        try:
            create_volunteer_consent(volunteer, **form.cleaned_data, request=request)
        except ValidationError as exc:
            apply_service_errors(form, exc)
        else:
            messages.success(request, "Согласие добавлено.")
            return redirect("volunteers:detail", pk=volunteer.pk)
    return action_page(
        request,
        form,
        subject=volunteer,
        title="Согласие на обработку данных",
        submit="Добавить",
        multipart=True,
    )


@login_required
@require_http_methods(["GET", "POST"])
def consent_revoke(request: HttpRequest, pk: str, consent_pk: str) -> HttpResponse:
    volunteer = selectors.get_managed_volunteer(request.user, pk)
    consent = get_object_or_404(
        Consent.objects.visible_to(request.user).filter(volunteer=volunteer), pk=consent_pk
    )
    if consent.revoked_on is not None:
        messages.info(request, "Согласие уже отозвано.")
        return redirect("volunteers:detail", pk=volunteer.pk)
    form = RevokeConsentForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            revoke_consent(consent, **form.cleaned_data, request=request)
        except ValidationError as exc:
            apply_service_errors(form, exc)
        else:
            messages.warning(
                request,
                "Отзыв согласия зафиксирован. Обработку данных нужно прекратить; "
                "обезличивание выполняет администратор.",
            )
            return redirect("volunteers:detail", pk=volunteer.pk)
    return action_page(
        request,
        form,
        subject=volunteer,
        title="Отзыв согласия",
        submit="Зафиксировать отзыв",
        intro=f"{consent} ({consent.get_signer_display().lower()}).",
    )


@login_required
@require_POST
def duplicate_resolve(request: HttpRequest, pk: str) -> HttpResponse:
    """ФТ-В-04: решение по пометке «возможный дубль» — другой человек или тот же."""
    volunteer, blocked = _editable_volunteer(request, pk)
    if blocked:
        return blocked
    decision = request.POST.get("decision")
    if decision not in {"different", "same"}:
        messages.error(request, "Не выбрано решение по возможному дублю.")
        return redirect("volunteers:detail", pk=volunteer.pk)
    try:
        services.resolve_duplicate(volunteer, same_person=decision == "same", request=request)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        if decision == "same":
            messages.success(
                request,
                "Карточка переведена в архив как дубль. Перенесите нужные сведения "
                "в основную карточку.",
            )
        else:
            messages.success(request, "Пометка «возможный дубль» снята.")
    return redirect("volunteers:detail", pk=volunteer.pk)


@login_required
@require_POST
def volunteer_archive(request: HttpRequest, pk: str) -> HttpResponse:
    volunteer = selectors.get_managed_volunteer(request.user, pk)
    services.archive_volunteer(volunteer, request=request)
    messages.success(request, "Карточка переведена в архив.")
    return redirect("volunteers:detail", pk=volunteer.pk)


@login_required
@require_POST
def volunteer_restore(request: HttpRequest, pk: str) -> HttpResponse:
    volunteer = selectors.get_managed_volunteer(request.user, pk)
    services.restore_volunteer(volunteer, request=request)
    messages.success(request, "Карточка возвращена из архива.")
    return redirect("volunteers:detail", pk=volunteer.pk)


# ---------------------------------------------------------------------------
# Telegram-бот (п. 7.2)
# ---------------------------------------------------------------------------
@login_required
@require_POST
def bot_code(request: HttpRequest, pk: str) -> HttpResponse:
    """Одноразовый код привязки бота. Показывается один раз — в сообщении на странице."""
    volunteer, response = _editable_volunteer(request, pk)
    if response is not None:
        return response
    try:
        code = services.issue_bot_link_code(volunteer, request=request)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(
            request,
            f"Код привязки бота: {code}. Передайте его волонтёру лично или по телефону: "
            "он откроет бота, нажмёт «Старт» и отправит код. Код действует 72 часа, "
            "повторно его не показать — при потере выдайте новый.",
        )
    return redirect("volunteers:detail", pk=volunteer.pk)


@login_required
@require_POST
def bot_unlink(request: HttpRequest, pk: str) -> HttpResponse:
    volunteer = selectors.get_managed_volunteer(request.user, pk)
    services.unlink_telegram(volunteer, request=request)
    messages.success(request, "Telegram отвязан: сообщения от бота приходить перестанут.")
    return redirect("volunteers:detail", pk=volunteer.pk)
