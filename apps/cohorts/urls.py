from django.urls import path

from . import views_facilitator as v

app_name = "facilitator"
urlpatterns = [
    path("", v.cohort_list, name="cohorts"),
    path("new/", v.cohort_create, name="cohort_create"),
    path("<uuid:cohort_id>/", v.cohort_detail, name="cohort"),
    path("<uuid:cohort_id>/invites/", v.invites, name="invites"),
    path("<uuid:cohort_id>/sessions/new/", v.session_form, name="session_create"),
    path("<uuid:cohort_id>/sessions/<uuid:session_id>/edit/", v.session_form, name="session_edit"),
    path("sessions/<uuid:session_id>/attendance/", v.attendance, name="attendance"),
    path("<uuid:cohort_id>/lessons/<int:number>/", v.lesson_guide, name="lesson_guide"),
    path("<uuid:cohort_id>/participants/<uuid:enrollment_id>/", v.participant_detail, name="participant"),
    path("<uuid:cohort_id>/completion/", v.completion_review, name="completion"),
    path("<uuid:cohort_id>/facilitators/", v.facilitators, name="facilitators"),
    path("<uuid:cohort_id>/projects/", v.projects, name="projects"),
]
