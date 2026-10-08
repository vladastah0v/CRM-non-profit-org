from django.urls import path

from . import views

app_name = "interactions"

urlpatterns = [
    path("add/<str:kind>/<uuid:pk>/", views.interaction_add, name="add"),
    path("<uuid:pk>/edit/", views.interaction_edit, name="edit"),
    path("<uuid:pk>/archive/", views.interaction_archive, name="archive"),
    path("tasks/", views.task_list, name="tasks"),
    path("tasks/add/<str:kind>/<uuid:pk>/", views.task_add, name="task_add"),
    path("tasks/<uuid:pk>/edit/", views.task_edit, name="task_edit"),
    path("tasks/<uuid:pk>/close/", views.task_close, name="task_close"),
]
