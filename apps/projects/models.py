from django.db import models
from django.db.models import F, Q

from apps.core.models import TimeStampedModel

STAGES = ["problem", "idea", "team", "planning", "implementation", "evaluation", "completed", "cancelled"]
TEAM_ROLES = [
    ("leader", "Leader — ensures work is done and deadlines are met"),
    ("secretary", "Secretary — takes notes on decisions made"),
    ("spokesperson", "Spokesperson — presents to the class"),
    ("resourcer", "Resourcer — collects necessary resources"),
    ("member", "Member"),
]


class Project(TimeStampedModel):
    cohort = models.ForeignKey("cohorts.Cohort", on_delete=models.RESTRICT, related_name="projects")
    name = models.CharField(max_length=200)
    problem_statement = models.TextField()
    idea_summary = models.TextField()
    community_benefit = models.TextField(null=True, blank=True)
    stage = models.CharField(max_length=20, choices=[(s, s.title()) for s in STAGES], default="problem")
    target_start_date = models.DateField(null=True, blank=True)
    target_end_date = models.DateField(null=True, blank=True)
    implemented_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    completion_confirmed_by = models.ForeignKey("accounts.User", on_delete=models.RESTRICT, null=True, blank=True, related_name="+")
    revision = models.IntegerField(default=1)

    class Meta:
        db_table = "projects"
        ordering = ["name"]
        constraints = [
            models.CheckConstraint(condition=Q(stage__in=STAGES), name="projects_stage_valid"),
            models.CheckConstraint(
                condition=Q(target_end_date__isnull=True) | Q(target_start_date__isnull=True) | Q(target_end_date__gte=F("target_start_date")),
                name="projects_dates_valid",
            ),
            models.CheckConstraint(
                condition=~Q(stage="completed") | (Q(completed_at__isnull=False) & Q(completion_confirmed_by__isnull=False)),
                name="projects_completion_confirmed",
            ),
        ]
        indexes = [models.Index(fields=["cohort", "stage"])]

    def __str__(self):
        return self.name


class ProjectMember(TimeStampedModel):
    project = models.ForeignKey(Project, on_delete=models.RESTRICT, related_name="members")
    enrollment = models.ForeignKey("cohorts.Enrollment", on_delete=models.RESTRICT, related_name="project_memberships")
    team_role = models.CharField(max_length=20, choices=TEAM_ROLES, default="member")
    joined_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "project_members"
        constraints = [
            models.UniqueConstraint(fields=["project", "enrollment"], name="project_members_unique"),
            models.CheckConstraint(
                condition=Q(team_role__in=["leader", "secretary", "spokesperson", "resourcer", "member"]), name="project_members_role_valid"
            ),
        ]


class ProjectGoal(TimeStampedModel):
    project = models.ForeignKey(Project, on_delete=models.RESTRICT, related_name="goals")
    display_order = models.SmallIntegerField()
    description = models.TextField()
    achieved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "project_goals"
        ordering = ["display_order"]
        constraints = [models.UniqueConstraint(fields=["project", "display_order"], name="project_goals_order_unique")]


class ProjectAction(TimeStampedModel):
    project = models.ForeignKey(Project, on_delete=models.RESTRICT, related_name="actions")
    goal = models.ForeignKey(ProjectGoal, on_delete=models.RESTRICT, null=True, blank=True, related_name="actions")
    problem_to_solve = models.TextField()
    action_required = models.TextField()
    display_order = models.SmallIntegerField()

    class Meta:
        db_table = "project_actions"
        ordering = ["display_order"]


class ProjectTask(TimeStampedModel):
    STATUS_CHOICES = [("todo", "To do"), ("doing", "Doing"), ("done", "Done")]

    action = models.ForeignKey(ProjectAction, on_delete=models.RESTRICT, related_name="tasks")
    description = models.TextField()
    responsible_member = models.ForeignKey(ProjectMember, on_delete=models.RESTRICT, null=True, blank=True, related_name="tasks")
    resources_needed = models.TextField(null=True, blank=True)
    others_involved = models.TextField(null=True, blank=True)
    start_date = models.DateField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="todo")
    completed_at = models.DateTimeField(null=True, blank=True)
    revision = models.IntegerField(default=1)

    class Meta:
        db_table = "project_tasks"
        ordering = ["created_at"]
        constraints = [
            models.CheckConstraint(condition=Q(status__in=["todo", "doing", "done"]), name="project_tasks_status_valid"),
            models.CheckConstraint(
                condition=Q(due_date__isnull=True) | Q(start_date__isnull=True) | Q(due_date__gte=F("start_date")),
                name="project_tasks_dates_valid",
            ),
        ]
        indexes = [models.Index(fields=["responsible_member", "status", "due_date"])]


class ProjectEvaluation(TimeStampedModel):
    project = models.ForeignKey(Project, on_delete=models.RESTRICT, related_name="evaluations")
    evaluator_member = models.ForeignKey(ProjectMember, on_delete=models.RESTRICT, related_name="evaluations")
    payload = models.JSONField(default=dict)
    status = models.CharField(max_length=20, choices=[("draft", "Draft"), ("submitted", "Submitted")], default="draft")
    submitted_at = models.DateTimeField(null=True, blank=True)
    revision = models.IntegerField(default=1)

    class Meta:
        db_table = "project_evaluations"
        constraints = [
            models.UniqueConstraint(fields=["project", "evaluator_member"], name="project_evaluations_unique"),
            models.CheckConstraint(condition=Q(status__in=["draft", "submitted"]), name="project_evaluations_status_valid"),
        ]
