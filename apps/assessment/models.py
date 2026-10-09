from django.db import models
from django.db.models import F, Q

from apps.core.models import TimeStampedModel


class Assessment(TimeStampedModel):
    component = models.OneToOneField("curriculum.LessonComponent", on_delete=models.RESTRICT, related_name="assessment")
    code = models.CharField(max_length=50)
    scoring_version = models.CharField(max_length=50)
    min_answer = models.SmallIntegerField()
    max_answer = models.SmallIntegerField()

    class Meta:
        db_table = "assessments"
        constraints = [models.CheckConstraint(condition=Q(max_answer__gt=F("min_answer")), name="assessments_scale_valid")]


class AssessmentCategory(TimeStampedModel):
    assessment = models.ForeignKey(Assessment, on_delete=models.RESTRICT, related_name="categories")
    code = models.CharField(max_length=50)
    label = models.CharField(max_length=100)
    description = models.TextField(null=True, blank=True)
    sample_careers = models.JSONField(default=list)
    display_order = models.SmallIntegerField(default=0)

    class Meta:
        db_table = "assessment_categories"
        ordering = ["display_order"]
        constraints = [models.UniqueConstraint(fields=["assessment", "code"], name="assessment_categories_unique")]


class AssessmentQuestion(TimeStampedModel):
    assessment = models.ForeignKey(Assessment, on_delete=models.RESTRICT, related_name="questions")
    category = models.ForeignKey(AssessmentCategory, on_delete=models.RESTRICT, related_name="questions")
    question_number = models.SmallIntegerField()
    prompt = models.TextField()

    class Meta:
        db_table = "assessment_questions"
        ordering = ["question_number"]
        constraints = [models.UniqueConstraint(fields=["assessment", "question_number"], name="assessment_questions_unique")]


class AssessmentAttempt(TimeStampedModel):
    DRAFT, SUBMITTED = "draft", "submitted"

    enrollment = models.ForeignKey("cohorts.Enrollment", on_delete=models.RESTRICT, related_name="assessment_attempts")
    assessment = models.ForeignKey(Assessment, on_delete=models.RESTRICT, related_name="attempts")
    attempt_number = models.IntegerField()
    status = models.CharField(max_length=20, choices=[(DRAFT, "Draft"), (SUBMITTED, "Submitted")], default=DRAFT)
    submitted_at = models.DateTimeField(null=True, blank=True)
    scoring_version = models.CharField(max_length=50)
    revision = models.IntegerField(default=1)

    class Meta:
        db_table = "assessment_attempts"
        ordering = ["attempt_number"]
        constraints = [
            models.UniqueConstraint(fields=["enrollment", "assessment", "attempt_number"], name="assessment_attempts_unique"),
            models.CheckConstraint(condition=Q(status__in=["draft", "submitted"]), name="assessment_attempts_status_valid"),
            models.CheckConstraint(condition=~Q(status="submitted") | Q(submitted_at__isnull=False), name="assessment_attempts_submitted_at"),
            # At most one open draft per enrollment and assessment.
            models.UniqueConstraint(fields=["enrollment", "assessment"], condition=Q(status="draft"), name="assessment_attempts_one_draft"),
        ]
        indexes = [models.Index(fields=["enrollment", "assessment", "submitted_at"])]


class AssessmentAnswer(models.Model):
    attempt = models.ForeignKey(AssessmentAttempt, on_delete=models.RESTRICT, related_name="answers")
    question = models.ForeignKey(AssessmentQuestion, on_delete=models.RESTRICT, related_name="+")
    value = models.SmallIntegerField()

    class Meta:
        db_table = "assessment_answers"
        constraints = [models.UniqueConstraint(fields=["attempt", "question"], name="assessment_answers_unique")]


class AssessmentScore(models.Model):
    attempt = models.ForeignKey(AssessmentAttempt, on_delete=models.RESTRICT, related_name="scores")
    category = models.ForeignKey(AssessmentCategory, on_delete=models.RESTRICT, related_name="+")
    score = models.SmallIntegerField()

    class Meta:
        db_table = "assessment_scores"
        constraints = [models.UniqueConstraint(fields=["attempt", "category"], name="assessment_scores_unique")]
