from django.urls import path

from apps.cohorts.views_join import join

from . import views

app_name = "participant"
urlpatterns = [
    path("", views.home, name="home"),
    path("<uuid:enrollment_id>/", views.journey, name="journey"),
    path("<uuid:enrollment_id>/lessons/<int:number>/", views.lesson, name="lesson"),
    path("<uuid:enrollment_id>/components/<uuid:component_id>/", views.component_action, name="component_action"),
    path("<uuid:enrollment_id>/quiz/", views.quiz_view, name="quiz"),
    path("<uuid:enrollment_id>/quiz/results/<uuid:attempt_id>/", views.quiz_results, name="quiz_results"),
    path("<uuid:enrollment_id>/completion/", views.completion, name="completion"),
    path("<uuid:enrollment_id>/withdraw/", views.withdraw, name="withdraw"),
]

join_urlpatterns = [
    path("", join, name="join"),
    path("<str:code>/", join, name="join_code"),
]
