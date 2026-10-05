from django.urls import path

from . import views

app_name = "consents"

urlpatterns = [
    path("files/<str:token>/", views.download, name="download"),
]
