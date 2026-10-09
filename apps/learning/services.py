"""Learning: typed workbook responses, completion and progress (FR15–FR21, FR30, FR34)."""
from django.db import transaction
from django.utils import timezone

from apps.cohorts.models import Cohort, Enrollment
from apps.core.errors import Conflict, Forbidden, NotFound, ValidationFailed
from apps.curriculum.models import Lesson, LessonComponent
from apps.curriculum.schema import clean_payload

from .models import ComponentResponse, LessonProgress, ProgrammeCompletionOverride

# ------------------------------------------------------------------ views of content


def participant_component_view(component: LessonComponent) -> dict:
    """FR16: participant payloads never contain facilitator-only guidance."""
    return {
        "id": str(component.pk),
        "key": component.component_key,
        "type": component.component_type,
        "title": component.title,
        "content": component.participant_content,
        "response_schema": component.response_schema,
        "required": component.required,
        "completion_method": component.completion_method,
        "visibility": component.visibility,
        "visibility_statement": component.visibility_statement,
    }


def facilitator_component_view(component: LessonComponent) -> dict:
    data = participant_component_view(component)
    data["facilitator_content"] = component.facilitator_content
    return data


# ------------------------------------------------------------------ guards


def _check_component(enrollment: Enrollment, component: LessonComponent):
    if component.lesson.curriculum_version_id != enrollment.cohort.curriculum_version_id:
        # Never accept a component ID solely because it exists.
        raise NotFound("Activity not found.")


def _check_can_work(enrollment: Enrollment):
    if enrollment.status == Enrollment.WITHDRAWN:
        raise Forbidden("You have withdrawn from this cohort. Rejoin with a join code to continue.")
    if enrollment.cohort.status in {Cohort.CANCELLED, Cohort.ARCHIVED}:
        raise Forbidden("This cohort is closed.")


def _locked_response(enrollment: Enrollment, component: LessonComponent) -> ComponentResponse | None:
    return ComponentResponse.objects.select_for_update().filter(enrollment=enrollment, component=component).first()


def _touch_started(enrollment: Enrollment, lesson: Lesson) -> LessonProgress:
    progress, _ = LessonProgress.objects.get_or_create(enrollment=enrollment, lesson=lesson)
    if progress.started_at is None:
        progress.started_at = timezone.now()
        progress.save(update_fields=["started_at", "updated_at"])
    return progress


# ------------------------------------------------------------------ participant work


@transaction.atomic
def save_draft(enrollment: Enrollment, component: LessonComponent, payload: dict, expected_revision: int) -> ComponentResponse:
    """FR17: save a draft. Drafts are not submitted work and never complete a component."""
    _check_component(enrollment, component)
    _check_can_work(enrollment)
    if component.completion_method != "submission":
        raise ValidationFailed("This part does not take written answers.")
    cleaned = clean_payload(component.response_schema, payload, for_submit=False)
    response = _locked_response(enrollment, component)
    if response and response.status in {ComponentResponse.SUBMITTED, ComponentResponse.COMPLETED}:
        raise Conflict("This activity has already been submitted.", code="already_submitted")
    current_revision = response.revision if response else 0
    if int(expected_revision) != current_revision:
        if response is not None and response.payload == cleaned and int(expected_revision) == current_revision - 1:
            # A retry of the save that already succeeded (e.g. the reply was lost): no duplicate effect.
            return response
        raise Conflict("These answers were saved from another window or device. Reload to see the latest version.")
    if response is None:
        response = ComponentResponse.objects.create(enrollment=enrollment, component=component, payload=cleaned, status=ComponentResponse.DRAFT)
    else:
        response.payload = cleaned
        response.status = ComponentResponse.DRAFT
        response.revision += 1
        response.save(update_fields=["payload", "status", "revision", "updated_at"])
    _touch_started(enrollment, component.lesson)
    return response


@transaction.atomic
def submit_response(enrollment: Enrollment, component: LessonComponent, payload: dict, expected_revision: int) -> ComponentResponse:
    """FR17/FR19: validate, save and complete; progress updates in the same transaction.

    A retry of an identical submission returns the stored response (NFR06).
    """
    _check_component(enrollment, component)
    _check_can_work(enrollment)
    if component.completion_method != "submission":
        raise ValidationFailed("This part does not take written answers.")
    cleaned = clean_payload(component.response_schema, payload, for_submit=True)
    response = _locked_response(enrollment, component)
    if response and response.status == ComponentResponse.COMPLETED:
        if response.payload == cleaned:
            return response
        raise Conflict("This activity has already been submitted.", code="already_submitted")
    current_revision = response.revision if response else 0
    if int(expected_revision) != current_revision:
        raise Conflict("These answers were changed from another window or device. Reload to see the latest version.")
    now = timezone.now()
    if response is None:
        response = ComponentResponse(enrollment=enrollment, component=component)
    else:
        response.revision += 1
    response.payload = cleaned
    response.status = ComponentResponse.COMPLETED
    response.submitted_at = now
    response.completed_at = now
    response.save()
    recompute_lesson(enrollment, component.lesson)
    return response


@transaction.atomic
def acknowledge(enrollment: Enrollment, component: LessonComponent) -> ComponentResponse:
    """Required readings use an explicit acknowledgement. Idempotent."""
    _check_component(enrollment, component)
    _check_can_work(enrollment)
    if component.completion_method != "acknowledgment":
        raise ValidationFailed("This part is not completed by acknowledgement.")
    response = _locked_response(enrollment, component)
    if response and response.status == ComponentResponse.COMPLETED:
        return response
    now = timezone.now()
    if response is None:
        response = ComponentResponse(enrollment=enrollment, component=component)
    response.status = ComponentResponse.COMPLETED
    response.completed_at = now
    response.save()
    recompute_lesson(enrollment, component.lesson)
    return response


def complete_component(enrollment: Enrollment, component: LessonComponent, *, confirmed_by=None) -> ComponentResponse:
    """Mark a non-written component complete (assessment, milestone, discussion). Caller holds the transaction."""
    response = _locked_response(enrollment, component)
    if response and response.status == ComponentResponse.COMPLETED:
        return response
    now = timezone.now()
    if response is None:
        response = ComponentResponse(enrollment=enrollment, component=component)
    response.status = ComponentResponse.COMPLETED
    response.completed_at = now
    response.confirmed_by = confirmed_by
    response.save()
    recompute_lesson(enrollment, component.lesson)
    return response


@transaction.atomic
def confirm_discussion(actor, cohort: Cohort, component: LessonComponent, enrollment_ids: list) -> int:
    """FR18: facilitator confirmation completes a discussion for selected participants — no invented text."""
    if component.completion_method != "facilitator_confirmation":
        raise ValidationFailed("This part is not confirmed by the facilitator.")
    if component.lesson.curriculum_version_id != cohort.curriculum_version_id:
        raise NotFound("Activity not found.")
    enrollments = list(Enrollment.objects.filter(cohort=cohort, pk__in=enrollment_ids).exclude(status=Enrollment.WITHDRAWN))
    if len(enrollments) != len(set(map(str, enrollment_ids))):
        raise ValidationFailed("Some selected participants are not active in this cohort.")
    for enrollment in enrollments:
        complete_component(enrollment, component, confirmed_by=actor)
    return len(enrollments)


# ------------------------------------------------------------------ progress


def recompute_lesson(enrollment: Enrollment, lesson: Lesson) -> LessonProgress:
    """Lesson completes only when every required component is complete (FR19)."""
    progress, _ = LessonProgress.objects.select_for_update().get_or_create(enrollment=enrollment, lesson=lesson)
    now = timezone.now()
    if progress.started_at is None:
        progress.started_at = now
    required = set(lesson.components.filter(required=True).values_list("pk", flat=True))
    done = set(
        ComponentResponse.objects.filter(
            enrollment=enrollment, component_id__in=required, status=ComponentResponse.COMPLETED
        ).values_list("component_id", flat=True)
    )
    if required and required <= done:
        if progress.completed_at is None:
            progress.completed_at = now
            progress.completion_method = "requirements"
    elif progress.completion_method == "requirements":
        # Explicit reversal path only (e.g. an authorized return-to-draft); overrides stay intact.
        progress.completed_at = None
        progress.completion_method = None
    progress.save()
    return progress


@transaction.atomic
def override_lesson(actor, enrollment: Enrollment, lesson: Lesson, reason: str) -> LessonProgress:
    """FR20: authorized exceptional completion stores approver, time and reason."""
    from apps.cohorts.policies import is_lead

    if not is_lead(actor, enrollment.cohort):
        raise Forbidden("Only the lead facilitator can record a lesson override.")
    if lesson.curriculum_version_id != enrollment.cohort.curriculum_version_id:
        raise NotFound("Lesson not found.")
    reason = (reason or "").strip()
    if not reason:
        raise ValidationFailed("A reason is required.", fields={"reason": "Explain why this lesson is being completed by exception."})
    progress, _ = LessonProgress.objects.select_for_update().get_or_create(enrollment=enrollment, lesson=lesson)
    if progress.completion_method == "requirements":
        raise ValidationFailed("This lesson is already complete.")
    now = timezone.now()
    progress.started_at = progress.started_at or now
    progress.completed_at = now
    progress.completion_method = "override"
    progress.override_reason = reason
    progress.confirmed_by = actor
    progress.save()
    return progress


def lesson_states(enrollment: Enrollment) -> dict:
    """lesson_id -> 'not_started' | 'in_progress' | 'complete'."""
    states = {}
    for p in LessonProgress.objects.filter(enrollment=enrollment):
        states[p.lesson_id] = "complete" if p.completed_at else ("in_progress" if p.started_at else "not_started")
    return states


# ------------------------------------------------------------------ project milestones


def milestone_satisfied(enrollment: Enrollment, milestone: str) -> bool:
    from apps.projects.models import ProjectEvaluation, ProjectMember, ProjectTask

    memberships = ProjectMember.objects.filter(enrollment=enrollment, ended_at__isnull=True).exclude(project__stage="cancelled")
    if milestone == "team_role_assigned":
        return memberships.exists()
    if milestone == "plan_recorded":
        project_ids = memberships.values_list("project_id", flat=True)
        return ProjectTask.objects.filter(action__project_id__in=project_ids, action__project__goals__isnull=False).exists()
    if milestone == "evaluation_submitted":
        return ProjectEvaluation.objects.filter(evaluator_member__in=memberships, status="submitted").exists()
    return False


def sync_milestones(enrollment: Enrollment) -> None:
    """Recompute project-milestone components after project changes. Completion moves forward only."""
    components = LessonComponent.objects.filter(
        lesson__curriculum_version_id=enrollment.cohort.curriculum_version_id, completion_method="project_milestone"
    ).select_related("lesson")
    for component in components:
        if milestone_satisfied(enrollment, component.participant_content.get("milestone")):
            complete_component(enrollment, component)


# ------------------------------------------------------------------ response visibility (FR34)


def can_read_payload(viewer, response: ComponentResponse) -> bool:
    """Private means participant only; facilitators see facilitator/team-visible text in assigned cohorts.

    Administrators never receive response text through the application.
    """
    from apps.cohorts.policies import is_assigned
    from apps.projects.models import ProjectMember

    if viewer.pk == response.enrollment.participant_user_id:
        return True
    visibility = response.component.visibility
    if visibility == "private":
        return False
    if is_assigned(viewer, response.enrollment.cohort):
        return True
    if visibility == "project_team":
        mine = ProjectMember.objects.filter(enrollment__participant_user=viewer, ended_at__isnull=True)
        theirs = ProjectMember.objects.filter(enrollment=response.enrollment, ended_at__isnull=True)
        return mine.filter(project_id__in=theirs.values("project_id")).exists()
    return False


def response_for_viewer(viewer, response: ComponentResponse) -> dict:
    data = {
        "component_id": str(response.component_id),
        "status": response.status,
        "submitted_at": response.submitted_at,
        "completed_at": response.completed_at,
        "revision": response.revision,
    }
    if can_read_payload(viewer, response):
        data["payload"] = response.payload
    return data


# ------------------------------------------------------------------ programme completion (FR30)


def programme_checklist(enrollment: Enrollment) -> dict:
    from apps.projects.models import ProjectEvaluation, ProjectMember

    policy = enrollment.cohort.curriculum_version.completion_policy.get("programme", {})
    lessons = list(Lesson.objects.filter(curriculum_version_id=enrollment.cohort.curriculum_version_id).order_by("lesson_number"))
    completed = set(
        LessonProgress.objects.filter(enrollment=enrollment, completed_at__isnull=False).values_list("lesson_id", flat=True)
    )
    missing_lessons = [lesson for lesson in lessons if lesson.pk not in completed]
    memberships = ProjectMember.objects.filter(enrollment=enrollment, ended_at__isnull=True).select_related("project")
    confirmed_projects = [m.project for m in memberships if m.project.stage == "completed" and m.project.completion_confirmed_by_id]
    evaluation_submitted = ProjectEvaluation.objects.filter(
        evaluator_member__in=[m for m in memberships if m.project.stage == "completed"], status="submitted"
    ).exists()
    override = enrollment.programme_overrides.filter(revoked_at__isnull=True).first()
    unmet = []
    if missing_lessons:
        unmet.append(f"{len(missing_lessons)} lesson(s) not complete: " + ", ".join(str(lesson.lesson_number) for lesson in missing_lessons))
    if policy.get("require_confirmed_project", True) and not confirmed_projects:
        unmet.append("Not a member of a project the facilitator has confirmed as complete.")
    if policy.get("require_submitted_evaluation", True) and not evaluation_submitted:
        unmet.append("Individual project evaluation not submitted.")
    return {
        "lessons_total": len(lessons),
        "lessons_completed": len(lessons) - len(missing_lessons),
        "missing_lessons": missing_lessons,
        "confirmed_projects": confirmed_projects,
        "evaluation_submitted": evaluation_submitted,
        "override": override,
        "unmet": unmet,
        "eligible": not unmet,
        "completed": enrollment.status == Enrollment.COMPLETED,
    }


@transaction.atomic
def confirm_programme_completion(actor, enrollment: Enrollment, *, override_reason: str = "") -> Enrollment:
    """Facilitator verifies conditions and confirms. Attendance alone is never sufficient.

    Missing requirements are listed; exceptional completion records an explicit reason.
    """
    from apps.cohorts.policies import is_lead

    enrollment = Enrollment.objects.select_for_update().select_related("cohort", "cohort__curriculum_version").get(pk=enrollment.pk)
    if not (is_lead(actor, enrollment.cohort) or actor.is_admin):
        raise Forbidden("Only the lead facilitator or an administrator can confirm programme completion.")
    if enrollment.status == Enrollment.COMPLETED:
        return enrollment
    if enrollment.status == Enrollment.WITHDRAWN:
        raise ValidationFailed("This participant has withdrawn.")
    checklist = programme_checklist(enrollment)
    override_reason = (override_reason or "").strip()
    if not checklist["eligible"]:
        if not override_reason:
            raise ValidationFailed(
                "Requirements are not yet met: " + " ".join(checklist["unmet"]),
                fields={"override_reason": "To complete by exception, give a reason."},
                code="requirements_unmet",
            )
        ProgrammeCompletionOverride.objects.create(enrollment=enrollment, authorized_by=actor, reason=override_reason)
    enrollment.status = Enrollment.COMPLETED
    enrollment.completed_at = timezone.now()
    enrollment.completion_confirmed_by = actor
    enrollment.save(update_fields=["status", "completed_at", "completion_confirmed_by", "updated_at"])
    return enrollment
