from django.urls import path

from . import views

app_name = "volunteers"

urlpatterns = [
    path("", views.volunteer_list, name="list"),
    path("new/", views.volunteer_create, name="create"),
    path("<uuid:pk>/", views.volunteer_detail, name="detail"),
    path("<uuid:pk>/edit/", views.volunteer_edit, name="edit"),
    path("<uuid:pk>/status/", views.volunteer_status, name="status"),
    path("<uuid:pk>/trainings/add/", views.training_add, name="training_add"),
    path("<uuid:pk>/documents/add/", views.document_add, name="document_add"),
    path(
        "<uuid:pk>/documents/<uuid:document_pk>/archive/",
        views.document_archive,
        name="document_archive",
    ),
    path("<uuid:pk>/hours/add/", views.hours_add, name="hours_add"),
    path("<uuid:pk>/consents/add/", views.consent_add, name="consent_add"),
    path(
        "<uuid:pk>/consents/<uuid:consent_pk>/revoke/",
        views.consent_revoke,
        name="consent_revoke",
    ),
    path("<uuid:pk>/duplicate/", views.duplicate_resolve, name="duplicate_resolve"),
    path("<uuid:pk>/bot/code/", views.bot_code, name="bot_code"),
    path("<uuid:pk>/bot/unlink/", views.bot_unlink, name="bot_unlink"),
    path("<uuid:pk>/archive/", views.volunteer_archive, name="archive"),
    path("<uuid:pk>/restore/", views.volunteer_restore, name="restore"),
]
