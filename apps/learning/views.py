"""Participant pages: home, journey, lesson reader, career quiz and completion summary."""
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.assessment import services as quiz
from apps.assessment.models import Assessment, AssessmentAttempt
from apps.cohorts.models import Enrollment, Session
from apps.cohorts.policies import own_enrollment
from apps.core.errors import DomainError, NotFound, ValidationFailed
from apps.curriculum.models import Lesson, LessonComponent
from apps.projects.models import ProjectMember

from . import services
from .models import ComponentResponse, LessonProgress


@login_required
def home(request):
    enrollments = (
        Enrollment.objects.filter(participant_user=request.user)
        .select_related("cohort", "cohort__partner", "cohort__curriculum_version")
        .order_by("-joined_at")
    )
    cards = []
    for e in enrollments:
        total = Lesson.objects.filter(curriculum_version_id=e.cohort.curriculum_version_id).count()
        done = LessonProgress.objects.filter(enrollment=e, completed_at__isnull=False).count()
        next_session = (
            Session.objects.filter(cohort=e.cohort, status=Session.SCHEDULED, starts_at__gte=timezone.now() - timedelta(hours=3))
            .select_related("lesson")
            .order_by("starts_at")
            .first()
        )
        cards.append({"enrollment": e, "total": total, "done": done, "pct": int(100 * done / total) if total else 0, "next_session": next_session})
    return render(request, "participant/home.html", {"cards": cards})


@login_required
def journey(request, enrollment_id):
    enrollment = own_enrollment(request.user, enrollment_id)
    lessons = Lesson.objects.filter(curriculum_version_id=enrollment.cohort.curriculum_version_id).order_by("lesson_number")
    states = services.lesson_states(enrollment)
    rows = [{"lesson": lesson, "state": states.get(lesson.pk, "not_started")} for lesson in lessons]
    sessions = Session.objects.filter(cohort=enrollment.cohort).exclude(status=Session.CANCELLED).select_related("lesson").order_by("starts_at")
    upcoming = [s for s in sessions if s.status == Session.SCHEDULED and s.starts_at >= timezone.now() - timedelta(hours=3)][:3]
    memberships = ProjectMember.objects.filter(enrollment=enrollment, ended_at__isnull=True).select_related("project")
    done = sum(1 for r in rows if r["state"] == "complete")
    return render(request, "participant/journey.html", {
        "enrollment": enrollment,
        "rows": rows,
        "done": done,
        "pct": int(100 * done / len(rows)) if rows else 0,
        "upcoming": upcoming,
        "memberships": memberships,
        "attendance": {a.session_id: a.status for a in enrollment.attendance.all()},
        "past_sessions": [s for s in sessions if s.status == Session.COMPLETED],
    })


def _lesson_context(enrollment, lesson, errors_for=None, posted=None):
    components = list(lesson.components.order_by("display_order"))
    responses = {r.component_id: r for r in ComponentResponse.objects.filter(enrollment=enrollment, component__in=components)}
    progress = LessonProgress.objects.filter(enrollment=enrollment, lesson=lesson).first()
    items = []
    for c in components:
        r = responses.get(c.pk)
        payload = (r.payload if r else None) or {}
        if errors_for and errors_for[0] == c.pk:
            payload = posted or payload
        items.append({
            "c": services.participant_component_view(c),
            "obj": c,
            "response": r,
            "payload": payload,
            "errors": errors_for[1] if errors_for and errors_for[0] == c.pk else {},
            "complete": bool(r and r.status == ComponentResponse.COMPLETED),
            "revision": r.revision if r else 0,
        })
    required = [i for i in items if i["obj"].required]
    lessons = list(Lesson.objects.filter(curriculum_version_id=lesson.curriculum_version_id).order_by("lesson_number").values_list("lesson_number", flat=True))
    return {
        "enrollment": enrollment,
        "lesson": lesson,
        "items": items,
        "progress": progress,
        "required_total": len(required),
        "required_done": sum(1 for i in required if i["complete"]),
        "prev_n": lesson.lesson_number - 1 if lesson.lesson_number > 1 else None,
        "next_n": lesson.lesson_number + 1 if lesson.lesson_number < max(lessons) else None,
        "assessment": Assessment.objects.filter(component__lesson=lesson).first(),
    }


@login_required
def lesson(request, enrollment_id, number):
    enrollment = own_enrollment(request.user, enrollment_id)
    lesson = get_object_or_404(Lesson, curriculum_version_id=enrollment.cohort.curriculum_version_id, lesson_number=number)
    return render(request, "participant/lesson.html", _lesson_context(enrollment, lesson))


def payload_from_post(schema: dict, post) -> dict:
    payload = {}
    for f in schema.get("fields", []):
        name = f"f__{f['key']}"
        if f["type"] == "list":
            payload[f["key"]] = [post.get(f"{name}__{i}", "") for i in range(int(f["count"]))]
        elif f["type"] == "multi_choice":
            payload[f["key"]] = post.getlist(name)
        else:
            payload[f["key"]] = post.get(name)
    return payload


@login_required
@require_POST
def component_action(request, enrollment_id, component_id):
    """No-JavaScript fallback for save/submit/acknowledge. Errors re-render with the typed input kept."""
    enrollment = own_enrollment(request.user, enrollment_id)
    component = LessonComponent.objects.select_related("lesson").filter(pk=component_id).first()
    if component is None or component.lesson.curriculum_version_id != enrollment.cohort.curriculum_version_id:
        raise NotFound("Activity not found.")
    action = request.POST.get("action")
    anchor = f"#c-{component.component_key}"
    try:
        if action == "acknowledge":
            services.acknowledge(enrollment, component)
            messages.success(request, f"“{component.title}” marked as read.")
        else:
            payload = payload_from_post(component.response_schema or {}, request.POST)
            revision = int(request.POST.get("expected_revision") or 0)
            if action == "submit":
                services.submit_response(enrollment, component, payload, revision)
                messages.success(request, f"“{component.title}” submitted.")
            else:
                services.save_draft(enrollment, component, payload, revision)
                messages.success(request, f"Draft of “{component.title}” saved.")
    except ValidationFailed as exc:
        ctx = _lesson_context(enrollment, component.lesson, errors_for=(component.pk, exc.fields), posted=payload_from_post(component.response_schema or {}, request.POST))
        ctx["error_message"] = exc.message
        ctx["error_component"] = component.component_key
        return render(request, "participant/lesson.html", ctx, status=422)
    except DomainError as exc:
        messages.error(request, exc.message)
    return redirect(f"/my/{enrollment.pk}/lessons/{component.lesson.lesson_number}/{anchor}")


# ------------------------------------------------------------------ quiz


def _assessment_for(enrollment):
    a = Assessment.objects.filter(component__lesson__curriculum_version_id=enrollment.cohort.curriculum_version_id).select_related("component__lesson").first()
    if a is None:
        raise NotFound("This curriculum has no career quiz.")
    return a


@login_required
def quiz_view(request, enrollment_id):
    enrollment = own_enrollment(request.user, enrollment_id)
    assessment = _assessment_for(enrollment)
    attempts = list(AssessmentAttempt.objects.filter(enrollment=enrollment, assessment=assessment).order_by("-attempt_number"))
    draft = next((a for a in attempts if a.status == "draft"), None)
    latest = next((a for a in attempts if a.status == "submitted"), None)
    if request.method == "POST":
        action = request.POST.get("action")
        answers = {k[2:]: v for k, v in request.POST.items() if k.startswith("q_")}
        try:
            if action == "start" or action == "retake":
                quiz.open_attempt(enrollment, assessment, retake=(action == "retake"))
                return redirect("participant:quiz", enrollment_id=enrollment.pk)
            if draft is None:
                raise ValidationFailed("Start the quiz first.")
            revision = int(request.POST.get("expected_revision") or 0)
            if action == "submit":
                attempt = quiz.submit_attempt(draft, answers, revision)
                messages.success(request, "Quiz submitted. Here are your results.")
                return redirect("participant:quiz_results", enrollment_id=enrollment.pk, attempt_id=attempt.pk)
            quiz.save_answers(draft, answers, revision)
            messages.success(request, "Answers saved.")
            return redirect("participant:quiz", enrollment_id=enrollment.pk)
        except ValidationFailed as exc:
            draft = AssessmentAttempt.objects.filter(pk=draft.pk).first() if draft else None
            return render(request, "participant/quiz.html", {
                "enrollment": enrollment, "assessment": assessment, "draft": draft, "latest": latest, "attempts": attempts,
                "answers": {int(k): v for k, v in answers.items() if k.isdigit()}, "errors": exc.fields, "error_message": exc.message,
                "questions": assessment.questions.order_by("question_number"), "scale": range(assessment.min_answer, assessment.max_answer + 1),
            }, status=422)
        except DomainError as exc:
            messages.error(request, exc.message)
            return redirect("participant:quiz", enrollment_id=enrollment.pk)
    answers = {}
    if draft:
        answers = {a.question.question_number: str(a.value) for a in draft.answers.select_related("question")}
    return render(request, "participant/quiz.html", {
        "enrollment": enrollment, "assessment": assessment, "draft": draft, "latest": latest, "attempts": attempts,
        "answers": answers, "errors": {}, "questions": assessment.questions.order_by("question_number"),
        "scale": range(assessment.min_answer, assessment.max_answer + 1),
    })


@login_required
def quiz_results(request, enrollment_id, attempt_id):
    enrollment = own_enrollment(request.user, enrollment_id)
    attempt = AssessmentAttempt.objects.filter(pk=attempt_id, enrollment=enrollment, status="submitted").first()
    if attempt is None:
        raise NotFound("Results not found.")
    data = quiz.results(attempt)
    others = AssessmentAttempt.objects.filter(enrollment=enrollment, assessment=attempt.assessment, status="submitted").order_by("-attempt_number")
    return render(request, "participant/quiz_results.html", {"enrollment": enrollment, **data, "others": others})


@login_required
def completion(request, enrollment_id):
    enrollment = own_enrollment(request.user, enrollment_id)
    return render(request, "participant/completion.html", {"enrollment": enrollment, "checklist": services.programme_checklist(enrollment)})


@login_required
@require_POST
def withdraw(request, enrollment_id):
    from apps.cohorts.services import withdraw_enrollment

    enrollment = own_enrollment(request.user, enrollment_id)
    withdraw_enrollment(request.user, enrollment)
    messages.info(request, "You have left this cohort. Your work is kept if you rejoin.")
    return redirect("participant:home")
