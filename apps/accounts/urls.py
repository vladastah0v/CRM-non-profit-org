from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("login/", views.LoginView.as_view(), name="login"),
    path("login/code/", views.otp_verify, name="otp_verify"),
    path("login/setup-2fa/", views.otp_setup, name="otp_setup"),
    path("logout/", views.logout_view, name="logout"),
    path("password/", views.PasswordChangeView.as_view(), name="password_change"),
]
