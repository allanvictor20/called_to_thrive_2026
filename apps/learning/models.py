from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.core.models import TimeStampedModel

User = settings.AUTH_USER_MODEL


class ComponentResponse(TimeStampedModel):
    DRAFT, SUBMITTED, COMPLETED, RETURNED = "draft", "submitted", "completed", "returned"
    STATUS_CHOICES = [(DRAFT, "Draft"), (SUBMITTED, "Submitted"), (COMPLETED, "Completed"), (RETURNED, "Returned")]

    enrollment = models.ForeignKey("cohorts.Enrollment", on_delete=models.RESTRICT, related_name="responses")
    component = models.ForeignKey("curriculum.LessonComponent", on_delete=models.RESTRICT, related_name="responses")
    payload = models.JSONField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=DRAFT)
    submitted_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    confirmed_by = models.ForeignKey(User, on_delete=models.RESTRICT, null=True, blank=True, related_name="+")
    revision = models.IntegerField(default=1)

    class Meta:
        db_table = "component_responses"
        constraints = [
            models.UniqueConstraint(fields=["enrollment", "component"], name="component_responses_unique"),
            models.CheckConstraint(
                condition=Q(status__in=["draft", "submitted", "completed", "returned"]), name="component_responses_status_valid"
            ),
            models.CheckConstraint(
                condition=~Q(status="completed") | Q(completed_at__isnull=False), name="component_responses_completed_at"
            ),
        ]
        indexes = [models.Index(fields=["component", "status"])]

    @property
    def is_complete(self) -> bool:
        return self.status == self.COMPLETED


class LessonProgress(TimeStampedModel):
    enrollment = models.ForeignKey("cohorts.Enrollment", on_delete=models.RESTRICT, related_name="lesson_progress")
    lesson = models.ForeignKey("curriculum.Lesson", on_delete=models.RESTRICT, related_name="progress")
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    completion_method = models.CharField(
        max_length=20, choices=[("requirements", "Requirements met"), ("override", "Override")], null=True, blank=True
    )
    override_reason = models.TextField(null=True, blank=True)
    confirmed_by = models.ForeignKey(User, on_delete=models.RESTRICT, null=True, blank=True, related_name="+")

    class Meta:
        db_table = "lesson_progress"
        constraints = [
            models.UniqueConstraint(fields=["enrollment", "lesson"], name="lesson_progress_unique"),
            models.CheckConstraint(
                condition=Q(completion_method__isnull=True) | Q(completion_method__in=["requirements", "override"]),
                name="lesson_progress_method_valid",
            ),
            models.CheckConstraint(
                condition=~Q(completion_method="override")
                | (Q(override_reason__isnull=False) & ~Q(override_reason="") & Q(confirmed_by__isnull=False)),
                name="lesson_progress_override_recorded",
            ),
            models.CheckConstraint(
                condition=Q(completed_at__isnull=True) | Q(completion_method__isnull=False), name="lesson_progress_completed_method"
            ),
        ]
        indexes = [models.Index(fields=["enrollment", "completed_at"])]


class ProgrammeCompletionOverride(TimeStampedModel):
    """Explicit programme completion override, distinct from a lesson override."""

    enrollment = models.ForeignKey("cohorts.Enrollment", on_delete=models.RESTRICT, related_name="programme_overrides")
    authorized_by = models.ForeignKey(User, on_delete=models.RESTRICT, related_name="+")
    reason = models.TextField()
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "programme_completion_overrides"
        constraints = [models.CheckConstraint(condition=~Q(reason=""), name="programme_overrides_reason_required")]
