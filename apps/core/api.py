"""JSON service contracts from the SDD.

Status codes: 201 create, 200 read/update, 422 malformed input, 401 no session,
403 forbidden scope or 404 concealed, 409 stale revision, 429 throttled.
Errors are {"error": {"code", "message", "fields"}} and never include SQL or secrets.
"""
import json
from functools import wraps

from django.http import JsonResponse
from django.urls import path

from apps.assessment import services as quiz
from apps.assessment.models import AssessmentAttempt
from apps.cohorts import services as cohort_services
from apps.cohorts.models import Session
from apps.cohorts.policies import cohort_for_facilitator, own_enrollment, require_admin
from apps.core.errors import DomainError, NotFound, ValidationFailed
from apps.core.ratelimit import check_rate
from apps.curriculum.models import LessonComponent
from apps.learning import services as learning
from apps.learning.models import ComponentResponse
from apps.projects import services as project_services
from apps.projects.models import ProjectMember, ProjectTask
from apps.reporting import services as reporting


def api_view(methods):
    def decorator(fn):
        @wraps(fn)
        def wrapper(request, *args, **kwargs):
            if request.method not in methods:
                return JsonResponse({"error": {"code": "method_not_allowed", "message": "Method not allowed.", "fields": {}}}, status=405)
            if not request.user.is_authenticated:
                return JsonResponse({"error": {"code": "unauthenticated", "message": "Sign in to continue.", "fields": {}}}, status=401)
            try:
                body = json.loads(request.body or b"{}") if request.method in {"POST", "PUT", "PATCH"} else {}
                if not isinstance(body, dict):
                    raise ValueError
            except ValueError:
                return JsonResponse({"error": {"code": "malformed", "message": "Request body must be a JSON object.", "fields": {}}}, status=422)
            try:
                result = fn(request, body, *args, **kwargs)
            except DomainError as exc:
                return JsonResponse({"error": exc.as_dict()}, status=exc.status)
            if isinstance(result, tuple):
                data, status = result
            else:
                data, status = result, 200
            return JsonResponse(data, status=status, json_dumps_params={"default": str})

        return wrapper

    return decorator


def _revision(body) -> int:
    try:
        return int(body.get("expected_revision"))
    except (TypeError, ValueError):
        raise ValidationFailed("expected_revision is required.", fields={"expected_revision": "Required."})


# ------------------------------------------------------------------ invites


@api_view({"POST"})
def invite_preview(request, body):
    check_rate("invite", f"user:{request.user.pk}")
    return cohort_services.preview_invite(request.user, str(body.get("code", "")))


@api_view({"POST"})
def invite_join(request, body):
    check_rate("invite", f"user:{request.user.pk}")
    enrollment, created = cohort_services.join_cohort(request.user, str(body.get("code", "")))
    data = {"enrollment_id": str(enrollment.pk), "cohort_id": str(enrollment.cohort_id), "status": enrollment.status, "created": created}
    return data, (201 if created else 200)


# ------------------------------------------------------------------ attendance


@api_view({"PUT"})
def session_attendance(request, body, session_id):
    session = Session.objects.filter(pk=session_id).first()
    if session is None:
        raise NotFound("Session not found.")
    cohort_for_facilitator(request.user, session.cohort_id)
    statuses = body.get("statuses")
    if not isinstance(statuses, dict):
        raise ValidationFailed("statuses must map enrollment ids to a status.", fields={"statuses": "Required."})
    session = cohort_services.record_attendance(request.user, session, statuses, _revision(body))
    return {"session_id": str(session.pk), "revision": session.revision, "summary": cohort_services.attendance_summary(session)}


# ------------------------------------------------------------------ responses


def _component(enrollment, component_id) -> LessonComponent:
    component = LessonComponent.objects.select_related("lesson").filter(pk=component_id).first()
    if component is None or component.lesson.curriculum_version_id != enrollment.cohort.curriculum_version_id:
        raise NotFound("Activity not found.")
    return component


@api_view({"GET", "PUT"})
def response_detail(request, body, enrollment_id, component_id):
    enrollment = own_enrollment(request.user, enrollment_id)
    component = _component(enrollment, component_id)
    if request.method == "GET":
        r = ComponentResponse.objects.filter(enrollment=enrollment, component=component).select_related("component", "enrollment__cohort").first()
        return {"response": learning.response_for_viewer(request.user, r) if r else None}
    r = learning.save_draft(enrollment, component, body.get("payload"), _revision(body))
    return {"status": r.status, "revision": r.revision, "saved_at": r.updated_at}


@api_view({"POST"})
def response_submit(request, body, enrollment_id, component_id):
    enrollment = own_enrollment(request.user, enrollment_id)
    component = _component(enrollment, component_id)
    r = learning.submit_response(enrollment, component, body.get("payload"), _revision(body))
    progress = r.enrollment.lesson_progress.filter(lesson=component.lesson).first()
    return {"status": r.status, "revision": r.revision, "lesson_completed": bool(progress and progress.completed_at)}


# ------------------------------------------------------------------ assessment


@api_view({"POST"})
def attempt_submit(request, body, attempt_id):
    attempt = AssessmentAttempt.objects.select_related("enrollment").filter(pk=attempt_id).first()
    if attempt is None or attempt.enrollment.participant_user_id != request.user.pk:
        raise NotFound("Attempt not found.")
    answers = body.get("answers")
    if answers is not None and not isinstance(answers, dict):
        raise ValidationFailed("answers must map question numbers to values.")
    attempt = quiz.submit_attempt(attempt, answers, _revision(body))
    data = quiz.results(attempt)
    return {
        "attempt_id": str(attempt.pk),
        "attempt_number": attempt.attempt_number,
        "status": attempt.status,
        "scoring_version": attempt.scoring_version,
        "scores": [{"category": r["code"], "label": r["label"], "score": r["score"], "is_top": r["is_top"]} for r in data["rows"]],
    }


# ------------------------------------------------------------------ projects


@api_view({"PATCH"})
def task_patch(request, body, project_id, task_id):
    project, _member, _fac = project_services.project_for_viewer(request.user, project_id)
    task = ProjectTask.objects.filter(pk=task_id, action__project=project).first()
    if task is None:
        raise NotFound("Task not found.")
    allowed = {"description", "resources_needed", "others_involved", "start_date", "due_date", "status", "responsible_member_id"}
    unknown = set(body) - allowed - {"expected_revision"}
    if unknown:
        raise ValidationFailed("Unknown fields.", fields={k: "Not editable." for k in unknown})
    data = {k: v for k, v in body.items() if k in allowed and k != "responsible_member_id"}
    for f in ("start_date", "due_date"):
        if f in data and data[f]:
            from django.utils.dateparse import parse_date

            parsed = parse_date(str(data[f]))
            if parsed is None:
                raise ValidationFailed("Use YYYY-MM-DD dates.", fields={f: "Use YYYY-MM-DD."})
            data[f] = parsed
    if "responsible_member_id" in body:
        mid = body["responsible_member_id"]
        member = ProjectMember.objects.filter(pk=mid).first() if mid else None
        if mid and member is None:
            raise ValidationFailed("Unknown member.", fields={"responsible_member_id": "Choose a current member of this project."})
        data["responsible_member"] = member
    task = project_services.update_task(request.user, project, task, data, _revision(body))
    return {"task_id": str(task.pk), "revision": task.revision, "status": task.status}


@api_view({"POST"})
def project_confirm(request, body, project_id):
    project, _member, _fac = project_services.project_for_viewer(request.user, project_id)
    if body.get("confirm") is not True:
        raise ValidationFailed("Set confirm to true to confirm completion.", fields={"confirm": "Required."})
    project = project_services.confirm_completion(request.user, project, _revision(body))
    return {"project_id": str(project.pk), "stage": project.stage, "revision": project.revision, "completed_at": project.completed_at}


# ------------------------------------------------------------------ reports


@api_view({"GET"})
def report_programme(request, body):
    require_admin(request.user)
    filters = reporting.Filters.from_query(request.GET)
    cohorts = list(reporting.filtered_cohorts(filters))
    summary = reporting.summarize(reporting.enrollment_rows(cohorts), cohorts, filters)
    return {"filters": {k: str(v) for k, v in filters.applied.items()}, "summary": summary}


app_name = "api"
urlpatterns = [
    path("cohort-invites/preview", invite_preview, name="invite_preview"),
    path("cohort-invites/join", invite_join, name="invite_join"),
    path("sessions/<uuid:session_id>/attendance", session_attendance, name="attendance"),
    path("enrollments/<uuid:enrollment_id>/responses/<uuid:component_id>", response_detail, name="response"),
    path("enrollments/<uuid:enrollment_id>/responses/<uuid:component_id>/submit", response_submit, name="response_submit"),
    path("assessment-attempts/<uuid:attempt_id>/submit", attempt_submit, name="attempt_submit"),
    path("projects/<uuid:project_id>/tasks/<uuid:task_id>", task_patch, name="task_patch"),
    path("projects/<uuid:project_id>/confirm-completion", project_confirm, name="project_confirm"),
    path("reports/programme", report_programme, name="report_programme"),
]
