"""Community projects (FR25–FR30)."""
from datetime import date

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from apps.cohorts.models import Cohort, Enrollment
from apps.cohorts.policies import is_assigned, is_lead
from apps.core.errors import Conflict, Forbidden, NotFound, ValidationFailed
from apps.learning.services import sync_milestones

from .models import STAGES, Project, ProjectAction, ProjectEvaluation, ProjectGoal, ProjectMember, ProjectTask

EDITABLE_STAGES = {"problem", "idea", "team", "planning", "implementation", "evaluation"}


# ------------------------------------------------------------------ access


def member_for(user, project: Project) -> ProjectMember | None:
    return (
        ProjectMember.objects.filter(project=project, enrollment__participant_user=user, ended_at__isnull=True)
        .exclude(enrollment__status=Enrollment.WITHDRAWN)
        .first()
    )


def project_for_viewer(user, project_id) -> tuple[Project, ProjectMember | None, bool]:
    """Returns (project, own membership, is_facilitator). Other teams' projects are concealed."""
    try:
        project = Project.objects.select_related("cohort", "cohort__partner").get(pk=project_id)
    except (Project.DoesNotExist, ValueError):
        raise NotFound("Project not found.")
    member = member_for(user, project)
    facilitator = is_assigned(user, project.cohort)
    if member is None and not facilitator:
        raise NotFound("Project not found.")
    return project, member, facilitator


def _require_editor(user, project: Project):
    if member_for(user, project) is None and not is_assigned(user, project.cohort):
        raise NotFound("Project not found.")
    if project.stage not in EDITABLE_STAGES:
        raise ValidationFailed("This project is closed for editing.")


def _require_facilitator(user, project: Project):
    if not is_assigned(user, project.cohort):
        raise Forbidden("Only the cohort's facilitators can do this.")


def _check_dates(start: date | None, end: date | None, field: str):
    if start and end and end < start:
        raise ValidationFailed("Dates are contradictory.", fields={field: "End date cannot be before the start date."})


def _bump(project: Project, expected_revision: int | None):
    """Optimistic revision check on the project (FR28)."""
    locked = Project.objects.select_for_update().get(pk=project.pk)
    if expected_revision is not None and locked.revision != int(expected_revision):
        raise Conflict("Someone else updated this project. Reload to see their changes.")
    locked.revision += 1
    locked.save(update_fields=["revision", "updated_at"])
    project.revision = locked.revision
    return locked


def _sync_team(project: Project):
    for m in project.members.filter(ended_at__isnull=True).select_related("enrollment__cohort__curriculum_version"):
        sync_milestones(m.enrollment)


# ------------------------------------------------------------------ project


@transaction.atomic
def create_project(actor, cohort: Cohort, *, name: str, problem_statement: str, idea_summary: str, community_benefit: str = "",
                   target_start_date=None, target_end_date=None) -> Project:
    if not is_assigned(actor, cohort):
        raise Forbidden("Only the cohort's facilitators can create projects.")
    if cohort.status not in {Cohort.PLANNED, Cohort.ACTIVE}:
        raise ValidationFailed("Projects can only be added to planned or active cohorts.")
    errors = {}
    for field, value in (("name", name), ("problem_statement", problem_statement), ("idea_summary", idea_summary)):
        if not (value or "").strip():
            errors[field] = "This is required."
    if errors:
        raise ValidationFailed("Please correct the highlighted fields.", fields=errors)
    _check_dates(target_start_date, target_end_date, "target_end_date")
    return Project.objects.create(
        cohort=cohort,
        name=name.strip(),
        problem_statement=problem_statement.strip(),
        idea_summary=idea_summary.strip(),
        community_benefit=(community_benefit or "").strip() or None,
        target_start_date=target_start_date,
        target_end_date=target_end_date,
        stage="idea",
    )


@transaction.atomic
def update_project(actor, project: Project, data: dict, expected_revision: int) -> Project:
    _require_editor(actor, project)
    locked = _bump(project, expected_revision)
    for field in ("name", "problem_statement", "idea_summary", "community_benefit"):
        if field in data:
            value = (data[field] or "").strip()
            if field != "community_benefit" and not value:
                raise ValidationFailed("This is required.", fields={field: "This is required."})
            setattr(locked, field, value or None)
    for field in ("target_start_date", "target_end_date"):
        if field in data:
            setattr(locked, field, data[field] or None)
    _check_dates(locked.target_start_date, locked.target_end_date, "target_end_date")
    if "stage" in data and data["stage"] != locked.stage:
        stage = data["stage"]
        if stage not in EDITABLE_STAGES:
            raise ValidationFailed("Use completion confirmation to complete a project.", fields={"stage": "Choose an earlier stage."})
        locked.stage = stage
        if stage == "implementation" and not locked.implemented_at:
            locked.implemented_at = timezone.now()
    locked.save()
    return locked


@transaction.atomic
def cancel_project(actor, project: Project, expected_revision: int) -> Project:
    _require_facilitator(actor, project)
    locked = _bump(project, expected_revision)
    locked.stage = "cancelled"
    locked.save(update_fields=["stage", "updated_at"])
    return locked


# ------------------------------------------------------------------ team


@transaction.atomic
def add_member(actor, project: Project, enrollment: Enrollment, team_role: str) -> ProjectMember:
    """FR26: members come from the same cohort (also enforced by a trigger)."""
    _require_facilitator(actor, project)
    if team_role not in {"leader", "secretary", "spokesperson", "resourcer", "member"}:
        raise ValidationFailed("Choose a team role.", fields={"team_role": "Choose a team role."})
    if enrollment.cohort_id != project.cohort_id:
        raise ValidationFailed("That participant is enrolled in another cohort.", fields={"enrollment": "Choose a participant from this cohort."})
    if enrollment.status == Enrollment.WITHDRAWN:
        raise ValidationFailed("That participant has withdrawn.", fields={"enrollment": "Choose an active participant."})
    member, created = ProjectMember.objects.get_or_create(project=project, enrollment=enrollment, defaults={"team_role": team_role})
    if not created:
        member.team_role = team_role
        member.ended_at = None
        member.save(update_fields=["team_role", "ended_at", "updated_at"])
    if project.stage in {"problem", "idea"}:
        Project.objects.filter(pk=project.pk).update(stage="team")
    sync_milestones(enrollment)
    return member


@transaction.atomic
def remove_member(actor, project: Project, member: ProjectMember) -> None:
    _require_facilitator(actor, project)
    if member.project_id != project.pk:
        raise NotFound("Member not found.")
    member.ended_at = timezone.now()
    member.save(update_fields=["ended_at", "updated_at"])


# ------------------------------------------------------------------ planning sheet


@transaction.atomic
def add_goal(actor, project: Project, description: str, expected_revision: int) -> ProjectGoal:
    _require_editor(actor, project)
    if not (description or "").strip():
        raise ValidationFailed("Describe the goal.", fields={"description": "Describe the goal."})
    _bump(project, expected_revision)
    order = (project.goals.aggregate(n=Max("display_order"))["n"] or 0) + 1
    goal = ProjectGoal.objects.create(project=project, display_order=order, description=description.strip())
    if project.stage in {"problem", "idea", "team"}:
        Project.objects.filter(pk=project.pk).update(stage="planning")
    _sync_team(project)
    return goal


@transaction.atomic
def add_action(actor, project: Project, *, problem_to_solve: str, action_required: str, goal: ProjectGoal | None,
               expected_revision: int) -> ProjectAction:
    _require_editor(actor, project)
    errors = {}
    if not (problem_to_solve or "").strip():
        errors["problem_to_solve"] = "Describe the problem to solve."
    if not (action_required or "").strip():
        errors["action_required"] = "Describe the action required."
    if goal is not None and goal.project_id != project.pk:
        errors["goal"] = "That goal belongs to another project."
    if errors:
        raise ValidationFailed("Please correct the highlighted fields.", fields=errors)
    _bump(project, expected_revision)
    order = (project.actions.aggregate(n=Max("display_order"))["n"] or 0) + 1
    action = ProjectAction.objects.create(
        project=project, goal=goal, problem_to_solve=problem_to_solve.strip(), action_required=action_required.strip(), display_order=order
    )
    _sync_team(project)
    return action


def _task_fields(project: Project, data: dict) -> dict:
    errors = {}
    out = {}
    if "description" in data:
        if not (data["description"] or "").strip():
            errors["description"] = "Describe the action item."
        out["description"] = (data["description"] or "").strip()
    if "responsible_member" in data:
        member = data["responsible_member"]
        if member is not None and (member.project_id != project.pk or member.ended_at is not None):
            errors["responsible_member"] = "Choose a current member of this project."
        out["responsible_member"] = member
    for f in ("resources_needed", "others_involved"):
        if f in data:
            out[f] = (data[f] or "").strip() or None
    for f in ("start_date", "due_date"):
        if f in data:
            out[f] = data[f] or None
    if "status" in data:
        if data["status"] not in {"todo", "doing", "done"}:
            errors["status"] = "Choose to do, doing or done."
        out["status"] = data["status"]
    if errors:
        raise ValidationFailed("Please correct the highlighted fields.", fields=errors)
    return out


@transaction.atomic
def add_task(actor, project: Project, action: ProjectAction, data: dict) -> ProjectTask:
    _require_editor(actor, project)
    if action.project_id != project.pk:
        raise NotFound("Action not found.")
    fields = _task_fields(project, data)
    if not fields.get("description"):
        raise ValidationFailed("Describe the action item.", fields={"description": "Describe the action item."})
    _check_dates(fields.get("start_date"), fields.get("due_date"), "due_date")
    task = ProjectTask.objects.create(action=action, **fields)
    _sync_team(project)
    return task


@transaction.atomic
def update_task(actor, project: Project, task: ProjectTask, data: dict, expected_revision: int) -> ProjectTask:
    """PATCH /projects/{id}/tasks/{task}: a stale revision gets 409 and never overwrites a newer edit (FR28/TC07)."""
    _require_editor(actor, project)
    locked = ProjectTask.objects.select_for_update().select_related("action").get(pk=task.pk)
    if locked.action.project_id != project.pk:
        raise NotFound("Task not found.")
    if locked.revision != int(expected_revision):
        raise Conflict("Someone else updated this action item. Reload to see the latest version.")
    fields = _task_fields(project, data)
    for k, v in fields.items():
        setattr(locked, k, v)
    _check_dates(locked.start_date, locked.due_date, "due_date")
    if locked.status == "done" and not locked.completed_at:
        locked.completed_at = timezone.now()
    elif locked.status != "done":
        locked.completed_at = None
    locked.revision += 1
    locked.save()
    return locked


# ------------------------------------------------------------------ evaluation (FR29)


def evaluation_definition(project: Project) -> dict:
    return project.cohort.curriculum_version.completion_policy["project_evaluation"]


def clean_evaluation(definition: dict, payload: dict, *, for_submit: bool) -> dict:
    lo, hi = definition["scale"]["min"], definition["scale"]["max"]
    cleaned, errors = {}, {}
    for section in definition["sections"]:
        for item in section["items"]:
            raw = (payload or {}).get(item["key"])
            if raw in (None, ""):
                if for_submit:
                    errors[item["key"]] = f"Choose a rating from {lo} to {hi}."
                continue
            try:
                value = int(raw)
            except (TypeError, ValueError):
                errors[item["key"]] = f"Choose a rating from {lo} to {hi}."
                continue
            if not lo <= value <= hi:
                errors[item["key"]] = f"Choose a rating from {lo} to {hi}."
                continue
            cleaned[item["key"]] = value
    for q in definition.get("text_questions", []):
        text = str((payload or {}).get(q["key"]) or "")
        if len(text) > 5000:
            errors[q["key"]] = "Please keep this under 5000 characters."
        elif for_submit and q.get("required") and not text.strip():
            errors[q["key"]] = "This answer is required before you submit."
        else:
            cleaned[q["key"]] = text
    if errors:
        raise ValidationFailed("Some answers need attention.", fields=errors)
    return cleaned


@transaction.atomic
def save_evaluation(actor, project: Project, payload: dict, expected_revision: int, *, submit: bool) -> ProjectEvaluation:
    member = member_for(actor, project)
    if member is None:
        raise Forbidden("Only project members complete the evaluation.")
    if project.stage in {"cancelled"}:
        raise ValidationFailed("This project was cancelled.")
    cleaned = clean_evaluation(evaluation_definition(project), payload, for_submit=submit)
    evaluation = ProjectEvaluation.objects.select_for_update().filter(project=project, evaluator_member=member).first()
    if evaluation and evaluation.status == "submitted":
        if submit and evaluation.payload == cleaned:
            return evaluation
        raise Conflict("Your evaluation has already been submitted.", code="already_submitted")
    current = evaluation.revision if evaluation else 0
    if int(expected_revision) != current:
        raise Conflict("Your evaluation was changed from another window. Reload to continue.")
    if evaluation is None:
        evaluation = ProjectEvaluation(project=project, evaluator_member=member)
    else:
        evaluation.revision += 1
    evaluation.payload = cleaned
    if submit:
        evaluation.status = "submitted"
        evaluation.submitted_at = timezone.now()
    evaluation.save()
    if submit:
        if project.stage in {"implementation", "planning", "team", "idea", "problem"}:
            Project.objects.filter(pk=project.pk).update(stage="evaluation")
        sync_milestones(member.enrollment)
    return evaluation


# ------------------------------------------------------------------ completion (FR30)


def completion_conditions(project: Project) -> dict:
    members = list(project.members.filter(ended_at__isnull=True).select_related("enrollment__participant_user"))
    submitted = set(project.evaluations.filter(status="submitted").values_list("evaluator_member_id", flat=True))
    unmet = []
    if not members:
        unmet.append("The project has no team members.")
    if not project.goals.exists():
        unmet.append("No goals are recorded on the planning sheet.")
    if not ProjectTask.objects.filter(action__project=project).exists():
        unmet.append("No action items are recorded on the planning sheet.")
    if not submitted:
        unmet.append("No team member has submitted an evaluation yet.")
    if project.stage not in {"implementation", "evaluation"}:
        unmet.append("The project has not reached implementation or evaluation.")
    return {
        "unmet": unmet,
        "ready": not unmet,
        "members_without_evaluation": [m for m in members if m.pk not in submitted],
    }


@transaction.atomic
def confirm_completion(actor, project: Project, expected_revision: int) -> Project:
    """Assigned lead confirms after reviewing conditions. Does not assume every member finished their personal work."""
    if not is_lead(actor, project.cohort):
        raise Forbidden("Only the lead facilitator can confirm project completion.")
    locked = Project.objects.select_for_update().get(pk=project.pk)
    if locked.stage == "completed":
        return locked
    if locked.revision != int(expected_revision):
        raise Conflict("Someone else updated this project. Reload before confirming.")
    conditions = completion_conditions(locked)
    if not conditions["ready"]:
        raise ValidationFailed("The project is not ready: " + " ".join(conditions["unmet"]), code="requirements_unmet")
    locked.stage = "completed"
    locked.completed_at = timezone.now()
    locked.completion_confirmed_by = actor
    locked.revision += 1
    locked.save()
    return locked


def stage_choices():
    return [s for s in STAGES if s in EDITABLE_STAGES]
