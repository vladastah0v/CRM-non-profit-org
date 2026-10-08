"""Маршруты API бота: /api/v1/bot/ (подключаются в config/urls.py)."""

from django.urls import path

from . import api

urlpatterns = [
    path("link/", api.LinkView.as_view(), name="bot-link"),
    path("unlink/", api.UnlinkView.as_view(), name="bot-unlink"),
    path("me/", api.MeView.as_view(), name="bot-me"),
    path("assignments/", api.MyAssignmentsView.as_view(), name="bot-assignments"),
    path("assignments/<uuid:pk>/", api.AssignmentDetailView.as_view(), name="bot-assignment"),
    path("assignments/<uuid:pk>/confirm/", api.ConfirmView.as_view(), name="bot-confirm"),
    path("assignments/<uuid:pk>/decline/", api.DeclineView.as_view(), name="bot-decline"),
    path("assignments/<uuid:pk>/attendance/", api.AttendanceView.as_view(), name="bot-attendance"),
    path("shifts/", api.OpenShiftsView.as_view(), name="bot-shifts"),
    path("shifts/<uuid:pk>/signup/", api.SignupView.as_view(), name="bot-signup"),
]
