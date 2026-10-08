from django.urls import path

from . import views

app_name = "imports"

urlpatterns = [
    path("", views.import_list, name="list"),
    path("template/<str:kind>/", views.template, name="template"),
    path("<uuid:pk>/", views.job_detail, name="detail"),
    path("<uuid:pk>/confirm/", views.job_confirm, name="confirm"),
    path("<uuid:pk>/cancel/", views.job_cancel, name="cancel"),
    path("<uuid:pk>/errors/", views.job_errors, name="errors"),
]
