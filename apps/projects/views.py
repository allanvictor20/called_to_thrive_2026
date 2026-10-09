from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

from apps.cohorts.models import Enrollment
from apps.cohorts.policies import is_lead
from apps.core.errors import DomainError, NotFound, ValidationFailed
from apps.core.utils import opt_date
from apps.learning.models import ComponentResponse
from apps.learning.services import can_read_payload

from . import services
from .models import TEAM_ROLES, ProjectAction, ProjectGoal, ProjectMember, ProjectTask


@login_required
def detail(request, project_id):
    project, member, is_facilitator = services.project_for_viewer(request.user, project_id)
    user = request.user
    eval_errors, eval_payload = {}, None
    if request.method == "POST":
        action = request.POST.get("action")
        rev = request.POST.get("revision")
        try:
            if action == "update_project":
                services.update_project(user, project, {
                    "name": request.POST.get("name"), "problem_statement": request.POST.get("problem_statement"),
                    "idea_summary": request.POST.get("idea_summary"), "community_benefit": request.POST.get("community_benefit"),
                    "target_start_date": opt_date(request.POST.get("target_start_date")), "target_end_date": opt_date(request.POST.get("target_end_date")),
                    "stage": request.POST.get("stage") or project.stage,
                }, int(rev or 0))
                messages.success(request, "Project updated.")
            elif action == "add_member":
                enrollment = Enrollment.objects.filter(pk=request.POST.get("enrollment")).first()
                if enrollment is None:
                    raise ValidationFailed("Choose a participant.")
                services.add_member(user, project, enrollment, request.POST.get("team_role", "member"))
                messages.success(request, "Team member added.")
            elif action == "remove_member":
                m = ProjectMember.objects.filter(pk=request.POST.get("member"), project=project).first()
                if m is None:
                    raise NotFound("Member not found.")
                services.remove_member(user, project, m)
                messages.success(request, "Team member removed. Their earlier contributions are kept.")
            elif action == "add_goal":
                services.add_goal(user, project, request.POST.get("description", ""), int(rev or 0))
                messages.success(request, "Goal added.")
            elif action == "add_action":
                goal = ProjectGoal.objects.filter(pk=request.POST.get("goal"), project=project).first() if request.POST.get("goal") else None
                services.add_action(user, project, problem_to_solve=request.POST.get("problem_to_solve", ""),
                                    action_required=request.POST.get("action_required", ""), goal=goal, expected_revision=int(rev or 0))
                messages.success(request, "Action added.")
            elif action in {"add_task", "update_task"}:
                responsible = ProjectMember.objects.filter(pk=request.POST.get("responsible_member"), project=project).first() if request.POST.get("responsible_member") else None
                data = {
                    "description": request.POST.get("description", ""), "responsible_member": responsible,
                    "resources_needed": request.POST.get("resources_needed"), "others_involved": request.POST.get("others_involved"),
                    "start_date": opt_date(request.POST.get("start_date")), "due_date": opt_date(request.POST.get("due_date")),
                    "status": request.POST.get("status", "todo"),
                }
                if action == "add_task":
                    act = ProjectAction.objects.filter(pk=request.POST.get("action_id"), project=project).first()
                    if act is None:
                        raise NotFound("Action not found.")
                    services.add_task(user, project, act, data)
                    messages.success(request, "Action item added.")
                else:
                    task = ProjectTask.objects.filter(pk=request.POST.get("task"), action__project=project).first()
                    if task is None:
                        raise NotFound("Action item not found.")
                    services.update_task(user, project, task, data, int(rev or 0))
                    messages.success(request, "Action item updated.")
            elif action in {"save_evaluation", "submit_evaluation"}:
                eval_payload = {k: v for k, v in request.POST.items() if k not in {"csrfmiddlewaretoken", "action", "revision"}}
                services.save_evaluation(user, project, eval_payload, int(rev or 0), submit=(action == "submit_evaluation"))
                messages.success(request, "Evaluation submitted." if action == "submit_evaluation" else "Evaluation draft saved.")
            elif action == "confirm_completion":
                services.confirm_completion(user, project, int(rev or 0))
                messages.success(request, "Project completion confirmed.")
            elif action == "cancel_project":
                services.cancel_project(user, project, int(rev or 0))
                messages.info(request, "Project cancelled.")
            return redirect("projects:detail", project_id=project.pk)
        except ValidationFailed as exc:
            if action in {"save_evaluation", "submit_evaluation"}:
                eval_errors = exc.fields
            messages.error(request, exc.message)
        except DomainError as exc:
            messages.error(request, exc.message)
            if exc.status != 422:
                return redirect("projects:detail", project_id=project.pk)
        project.refresh_from_db()

    definition = services.evaluation_definition(project)
    my_eval = project.evaluations.filter(evaluator_member=member).first() if member else None
    members = project.members.filter(ended_at__isnull=True).select_related("enrollment__participant_user")
    # Team-shared submissions (project_team visibility), shown only when the viewer may read them.
    team_items = []
    shared = ComponentResponse.objects.filter(
        enrollment__in=[m.enrollment for m in members], component__visibility="project_team", status=ComponentResponse.COMPLETED
    ).select_related("component", "enrollment__participant_user", "enrollment__cohort")
    for r in shared:
        if can_read_payload(user, r):
            team_items.append(r)
    return render(request, "projects/detail.html", {
        "project": project,
        "member": member,
        "is_facilitator": is_facilitator,
        "is_lead": is_lead(user, project.cohort),
        "members": members,
        "goals": project.goals.all(),
        "actions": project.actions.select_related("goal").prefetch_related("tasks__responsible_member__enrollment__participant_user"),
        "available": project.cohort.enrollments.exclude(status=Enrollment.WITHDRAWN).exclude(pk__in=[m.enrollment_id for m in members]).select_related("participant_user") if is_facilitator else [],
        "team_roles": TEAM_ROLES,
        "stages": services.stage_choices(),
        "definition": definition,
        "my_eval": my_eval,
        "eval_payload": eval_payload if eval_payload is not None else (my_eval.payload if my_eval else {}),
        "eval_errors": eval_errors,
        "conditions": services.completion_conditions(project) if is_facilitator else None,
        "team_items": team_items,
        "evaluations_submitted": project.evaluations.filter(status="submitted").count(),
    })
