from django.urls import path

from . import views

app_name = "reporting"
urlpatterns = [
    path("programme/", views.programme, name="programme"),
    path("export/", views.export, name="export"),
]
