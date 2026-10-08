from django.urls import path

from . import views

app_name = "beneficiaries"

urlpatterns = [
    path("", views.beneficiary_list, name="list"),
    path("new/", views.beneficiary_create, name="create"),
    path("<uuid:pk>/", views.beneficiary_detail, name="detail"),
    path("<uuid:pk>/edit/", views.beneficiary_edit, name="edit"),
    path("<uuid:pk>/status/", views.beneficiary_status, name="status"),
    path("<uuid:pk>/sensitive/", views.sensitive_detail, name="sensitive"),
    path("<uuid:pk>/sensitive/edit/", views.sensitive_edit, name="sensitive_edit"),
    path("<uuid:pk>/documents/add/", views.document_add, name="document_add"),
    path("<uuid:pk>/related/add/", views.related_add, name="related_add"),
    path("<uuid:pk>/related/<uuid:person_pk>/edit/", views.related_edit, name="related_edit"),
    path(
        "<uuid:pk>/related/<uuid:person_pk>/archive/",
        views.related_archive,
        name="related_archive",
    ),
    path("<uuid:pk>/consents/add/", views.consent_add, name="consent_add"),
    path(
        "<uuid:pk>/consents/<uuid:consent_pk>/revoke/",
        views.consent_revoke,
        name="consent_revoke",
    ),
    path("<uuid:pk>/curators/", views.curators_manage, name="curators"),
    path(
        "<uuid:pk>/curators/<uuid:access_pk>/revoke/",
        views.access_revoke,
        name="access_revoke",
    ),
    path("<uuid:pk>/duplicate/", views.duplicate_resolve, name="duplicate_resolve"),
    path("<uuid:pk>/archive/", views.beneficiary_archive, name="archive"),
    path("<uuid:pk>/restore/", views.beneficiary_restore, name="restore"),
]
