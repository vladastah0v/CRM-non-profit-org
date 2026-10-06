from django.urls import path

from . import views

app_name = "notifications"

urlpatterns = [
    path("", views.notification_list, name="list"),
    path("<uuid:pk>/open/", views.notification_open, name="open"),
    path("read-all/", views.notification_read_all, name="read_all"),
]
