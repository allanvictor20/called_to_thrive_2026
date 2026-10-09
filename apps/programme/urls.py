from django.urls import path

from . import views

app_name = "admin_console"
urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("facilitators/", views.facilitators, name="facilitators"),
    path("partners/", views.partners, name="partners"),
    path("partners/<uuid:partner_id>/", views.partner_detail, name="partner"),
    path("geography/", views.geography, name="geography"),
    path("cohorts/<uuid:cohort_id>/", views.cohort_oversight, name="cohort"),
]
