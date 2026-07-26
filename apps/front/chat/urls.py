from django.urls import path

from . import views

app_name = "chat"


# Associe les routes de la page,
# de création et de suivi des tickets.
urlpatterns = [
    path(
        "",
        views.index,
        name="index",
    ),
    path(
        "jobs/submit/",
        views.submit_job,
        name="submit-job",
    ),
    path(
        "jobs/<uuid:job_id>/",
        views.job_status,
        name="job-status",
    ),
    path(
        "clear/",
        views.clear_chat,
        name="clear",
    ),
]