from django.urls import path

from . import views

app_name = "reports"

urlpatterns = [
    path("", views.report_index, name="index"),
    path("program/", views.program_report, name="program"),
    path("volunteers/", views.volunteer_report, name="volunteers"),
    path("funnel/", views.funnel_report, name="funnel"),
    path("requests/", views.request_report, name="requests"),
    path("export/<str:kind>/", views.export_list, name="export"),
]
