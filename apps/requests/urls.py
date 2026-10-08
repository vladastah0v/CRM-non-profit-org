from django.urls import path

from . import views

app_name = "requests"

urlpatterns = [
    path("", views.request_list, name="list"),
    path("new/<uuid:beneficiary_pk>/", views.request_create, name="create"),
    path("<uuid:pk>/", views.request_detail, name="detail"),
    path("<uuid:pk>/edit/", views.request_edit, name="edit"),
    path("<uuid:pk>/status/", views.request_status, name="status"),
    path("<uuid:pk>/archive/", views.request_archive, name="archive"),
    path("assistance/new/<uuid:beneficiary_pk>/", views.assistance_add, name="assistance_add"),
    path("assistance/<uuid:pk>/edit/", views.assistance_edit, name="assistance_edit"),
    path("assistance/<uuid:pk>/archive/", views.assistance_archive, name="assistance_archive"),
]
