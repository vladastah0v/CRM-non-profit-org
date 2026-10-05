from django.contrib.auth import views as auth_views
from django.urls import path

app_name = "accounts"

# Временный вход по паролю. На этапе «Ядро» заменяется входом с 2FA.
urlpatterns = [
    path(
        "login/",
        auth_views.LoginView.as_view(template_name="accounts/login.html"),
        name="login",
    ),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
]
