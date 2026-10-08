from django.urls import path

from . import search, views

app_name = "core"

urlpatterns = [
    path("", views.home, name="home"),
    path("healthz/", views.healthz, name="healthz"),
    path("search/", search.search, name="search"),
]
