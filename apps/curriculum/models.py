from django.db import models
from django.db.models import Q

from apps.core.models import TimeStampedModel


class CurriculumVersion(TimeStampedModel):
    DRAFT, PUBLISHED, RETIRED = "draft", "published", "retired"
    STATUS_CHOICES = [(DRAFT, "Draft"), (PUBLISHED, "Published"), (RETIRED, "Retired")]

    code = models.CharField(max_length=50, unique=True)
    title = models.CharField(max_length=200)
    source_reference = models.TextField()
    language_code = models.CharField(max_length=10, default="en")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=DRAFT)
    published_at = models.DateTimeField(null=True, blank=True)
    completion_policy = models.JSONField(default=dict)

    class Meta:
        db_table = "curriculum_versions"
        constraints = [
            models.CheckConstraint(condition=Q(status__in=["draft", "published", "retired"]), name="curriculum_versions_status_valid"),
            models.CheckConstraint(
                condition=Q(status="draft") | Q(published_at__isnull=False), name="curriculum_versions_published_at"
            ),
        ]

    def __str__(self):
        return f"{self.title} ({self.code})"


class Lesson(TimeStampedModel):
    curriculum_version = models.ForeignKey(CurriculumVersion, on_delete=models.RESTRICT, related_name="lessons")
    lesson_number = models.SmallIntegerField()
    title = models.CharField(max_length=200)
    summary = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "lessons"
        ordering = ["lesson_number"]
        constraints = [
            models.UniqueConstraint(fields=["curriculum_version", "lesson_number"], name="lessons_version_number_unique"),
            models.CheckConstraint(condition=Q(lesson_number__gt=0), name="lessons_number_positive"),
        ]

    def __str__(self):
        return f"Lesson {self.lesson_number}: {self.title}"


TYPES = ["truth", "scripture", "vocabulary", "teaching", "discussion", "activity", "life_work", "assessment", "project_milestone"]
METHODS = ["acknowledgment", "submission", "facilitator_confirmation", "assessment", "project_milestone"]
VISIBILITIES = ["private", "facilitator", "project_team"]


class LessonComponent(TimeStampedModel):
    TYPES = TYPES
    METHODS = METHODS
    VISIBILITIES = VISIBILITIES

    lesson = models.ForeignKey(Lesson, on_delete=models.RESTRICT, related_name="components")
    component_key = models.CharField(max_length=80)
    display_order = models.SmallIntegerField()
    component_type = models.CharField(max_length=30, choices=[(t, t) for t in TYPES])
    title = models.CharField(max_length=200)
    participant_content = models.JSONField(default=dict)
    facilitator_content = models.JSONField(null=True, blank=True)
    response_schema = models.JSONField(null=True, blank=True)
    required = models.BooleanField(default=False)
    completion_method = models.CharField(max_length=30, choices=[(m, m) for m in METHODS], null=True, blank=True)
    visibility = models.CharField(max_length=20, choices=[(v, v) for v in VISIBILITIES], default="private")

    class Meta:
        db_table = "lesson_components"
        ordering = ["display_order"]
        constraints = [
            models.UniqueConstraint(fields=["lesson", "component_key"], name="lesson_components_key_unique"),
            models.UniqueConstraint(fields=["lesson", "display_order"], name="lesson_components_order_unique"),
            models.CheckConstraint(condition=Q(component_type__in=TYPES), name="lesson_components_type_valid"),
            models.CheckConstraint(
                condition=Q(completion_method__isnull=True) | Q(completion_method__in=METHODS), name="lesson_components_method_valid"
            ),
            models.CheckConstraint(condition=Q(visibility__in=VISIBILITIES), name="lesson_components_visibility_valid"),
            models.CheckConstraint(
                condition=Q(required=False) | Q(completion_method__isnull=False), name="lesson_components_required_has_method"
            ),
        ]

    def __str__(self):
        return f"{self.lesson} / {self.title}"

    @property
    def needs_response(self) -> bool:
        return self.completion_method == "submission"

    @property
    def visibility_statement(self) -> str:
        return {
            "private": "Private: only you can see what you write here. Your facilitator sees only whether it is complete.",
            "facilitator": "Shared with your facilitator: your assigned facilitators can read this answer.",
            "project_team": "Shared with your project team and facilitator.",
        }[self.visibility]
