from django.conf import settings
from django.contrib import admin
from django.urls import URLPattern, URLResolver, include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerSplitView

from apps.volunteers.api import VolunteerIntakeView

api_v1: list[URLPattern | URLResolver] = [
    path("schema/", SpectacularAPIView.as_view(), name="schema"),
    # Split-вариант не содержит встроенных скриптов — работает при строгом CSP
    path("docs/", SpectacularSwaggerSplitView.as_view(url_name="api-v1:schema"), name="docs"),
    path("intake/volunteer/", VolunteerIntakeView.as_view(), name="intake-volunteer"),
    path("bot/", include("apps.shifts.api_urls")),
]

urlpatterns = [
    path("", include("apps.core.urls")),
    path("accounts/", include("apps.accounts.urls")),
    path("", include("apps.consents.urls")),
    path("volunteers/", include("apps.volunteers.urls")),
    path("beneficiaries/", include("apps.beneficiaries.urls")),
    path("requests/", include("apps.requests.urls")),
    path("interactions/", include("apps.interactions.urls")),
    path("shifts/", include("apps.shifts.urls")),
    path("notifications/", include("apps.notifications.urls")),
    path("api/v1/", include((api_v1, "api-v1"))),
    path(settings.ADMIN_URL, admin.site.urls),
]
