from django.urls import path

from . import views

app_name = "shifts"

urlpatterns = [
    path("", views.shift_list, name="list"),
    path("new/", views.shift_create, name="create"),
    path("<uuid:pk>/", views.shift_detail, name="detail"),
    path("<uuid:pk>/edit/", views.shift_edit, name="edit"),
    path("<uuid:pk>/cancel/", views.shift_cancel, name="cancel"),
    path("<uuid:pk>/archive/", views.shift_archive, name="archive"),
    path("<uuid:pk>/pick/", views.shift_pick, name="pick"),
    path("<uuid:pk>/attendance/", views.shift_attendance, name="attendance"),
    path("assignments/<uuid:pk>/status/", views.assignment_status, name="assignment_status"),
    path("assignments/<uuid:pk>/hours/", views.assignment_hours, name="assignment_hours"),
    path("visits/new/<uuid:beneficiary_pk>/", views.visit_create, name="visit_create"),
    path("visits/<uuid:pk>/", views.visit_detail, name="visit_detail"),
    path("visits/<uuid:pk>/edit/", views.visit_edit, name="visit_edit"),
]
