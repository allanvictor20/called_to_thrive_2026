from django.urls import include, path

from apps.core import api
from apps.learning.urls import join_urlpatterns

urlpatterns = [
    path("", include("apps.core.urls")),
    path("accounts/", include("apps.accounts.urls")),
    path("join/", include((join_urlpatterns, "cohorts"))),
    path("my/", include("apps.learning.urls")),
    path("facilitate/", include("apps.cohorts.urls")),
    path("projects/", include("apps.projects.urls")),
    path("admin-console/", include("apps.programme.urls")),
    path("reports/", include("apps.reporting.urls")),
    path("api/", include((api.urlpatterns, "api"))),
]
