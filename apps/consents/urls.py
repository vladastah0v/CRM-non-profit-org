from django.urls import path

from . import views

app_name = "consents"

urlpatterns = [
    path("files/<str:token>/", views.download, name="download"),
    path("privacy/", views.erasure_list, name="erasure_list"),
    path(
        "privacy/<str:kind>/<uuid:pk>/export/",
        views.subject_export,
        name="subject_export",
    ),
    path(
        "privacy/<str:kind>/<uuid:pk>/anonymize/",
        views.anonymize_person,
        name="anonymize",
    ),
    path(
        "privacy/<str:kind>/<uuid:pk>/dismiss/",
        views.dismiss_erasure,
        name="dismiss_erasure",
    ),
]
