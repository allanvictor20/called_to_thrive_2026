from django.conf import settings
from django.db import models
from django.db.models import F, Q

from apps.core.models import TimeStampedModel

User = settings.AUTH_USER_MODEL

MEETING_TYPES = [("physical", "Physical"), ("online", "Online (Google Meet)"), ("hybrid", "Hybrid")]


class Cohort(TimeStampedModel):
    PLANNED, ACTIVE, COMPLETED, CANCELLED, ARCHIVED = "planned", "active", "completed", "cancelled", "archived"
    STATUS_CHOICES = [(PLANNED, "Planned"), (ACTIVE, "Active"), (COMPLETED, "Completed"), (CANCELLED, "Cancelled"), (ARCHIVED, "Archived")]
    TRANSITIONS = {
        PLANNED: {ACTIVE, CANCELLED},
        ACTIVE: {COMPLETED, CANCELLED},
        COMPLETED: {ARCHIVED, ACTIVE},  # reopening needs an authorized correction reason
        CANCELLED: {ARCHIVED},
        ARCHIVED: set(),
    }

    partner = models.ForeignKey("programme.Partner", on_delete=models.RESTRICT, related_name="cohorts")
    district = models.ForeignKey("programme.District", on_delete=models.RESTRICT, related_name="cohorts")
    curriculum_version = models.ForeignKey("curriculum.CurriculumVersion", on_delete=models.RESTRICT, related_name="cohorts")
    name = models.CharField(max_length=200)
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=PLANNED)
    timezone = models.CharField(max_length=60, default="Africa/Kampala")
    default_meeting_type = models.CharField(max_length=20, choices=MEETING_TYPES)
    default_meet_url = models.URLField(null=True, blank=True)
    current_lesson = models.ForeignKey("curriculum.Lesson", on_delete=models.RESTRICT, null=True, blank=True, related_name="+")
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "cohorts"
        ordering = ["-start_date", "name"]
        constraints = [
            models.CheckConstraint(condition=Q(end_date__isnull=True) | Q(end_date__gte=F("start_date")), name="cohorts_dates_valid"),
            models.CheckConstraint(
                condition=Q(status__in=["planned", "active", "completed", "cancelled", "archived"]), name="cohorts_status_valid"
            ),
            models.CheckConstraint(
                condition=Q(default_meeting_type__in=["physical", "online", "hybrid"]), name="cohorts_meeting_type_valid"
            ),
        ]
        indexes = [
            models.Index(fields=["district", "start_date"]),
            models.Index(fields=["partner", "status"]),
        ]

    def __str__(self):
        return self.name

    @property
    def lead(self):
        assignment = self.facilitator_assignments.filter(assignment_role="lead", ended_at__isnull=True).select_related("facilitator_user").first()
        return assignment.facilitator_user if assignment else None


class CohortFacilitator(models.Model):
    cohort = models.ForeignKey(Cohort, on_delete=models.RESTRICT, related_name="facilitator_assignments")
    facilitator_user = models.ForeignKey(User, on_delete=models.RESTRICT, related_name="cohort_assignments")
    assignment_role = models.CharField(max_length=20, choices=[("lead", "Lead"), ("assistant", "Assistant")])
    assigned_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "cohort_facilitators"
        constraints = [
            models.UniqueConstraint(fields=["cohort", "facilitator_user"], name="cohort_facilitators_pair_unique"),
            models.CheckConstraint(condition=Q(assignment_role__in=["lead", "assistant"]), name="cohort_facilitators_role_valid"),
            models.UniqueConstraint(
                fields=["cohort"], condition=Q(assignment_role="lead", ended_at__isnull=True), name="cohort_one_active_lead"
            ),
        ]
        indexes = [models.Index(fields=["facilitator_user", "ended_at"])]


class CohortInvite(TimeStampedModel):
    cohort = models.ForeignKey(Cohort, on_delete=models.RESTRICT, related_name="invites")
    code_digest = models.CharField(max_length=64, unique=True)
    code_hint = models.CharField(max_length=8, help_text="Last characters of the code, for identification only.")
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    max_uses = models.IntegerField(null=True, blank=True)
    use_count = models.IntegerField(default=0)
    created_by = models.ForeignKey(User, on_delete=models.RESTRICT, related_name="+")

    class Meta:
        db_table = "cohort_invites"
        constraints = [
            models.CheckConstraint(condition=Q(max_uses__isnull=True) | Q(max_uses__gt=0), name="cohort_invites_max_uses_positive"),
            models.CheckConstraint(
                condition=Q(use_count__gte=0) & (Q(max_uses__isnull=True) | Q(use_count__lte=F("max_uses"))),
                name="cohort_invites_capacity",
            ),
        ]


class Enrollment(TimeStampedModel):
    ACTIVE, COMPLETED, WITHDRAWN = "active", "completed", "withdrawn"
    STATUS_CHOICES = [(ACTIVE, "Active"), (COMPLETED, "Completed"), (WITHDRAWN, "Withdrawn")]

    cohort = models.ForeignKey(Cohort, on_delete=models.RESTRICT, related_name="enrollments")
    participant_user = models.ForeignKey(User, on_delete=models.RESTRICT, related_name="enrollments")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=ACTIVE)
    joined_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    completion_confirmed_by = models.ForeignKey(User, on_delete=models.RESTRICT, null=True, blank=True, related_name="+")
    withdrawn_at = models.DateTimeField(null=True, blank=True)
    invite = models.ForeignKey(CohortInvite, on_delete=models.RESTRICT, null=True, blank=True, related_name="enrollments")

    class Meta:
        db_table = "enrollments"
        constraints = [
            models.UniqueConstraint(fields=["cohort", "participant_user"], name="enrollments_cohort_participant_unique"),
            models.CheckConstraint(condition=Q(status__in=["active", "completed", "withdrawn"]), name="enrollments_status_valid"),
            models.CheckConstraint(condition=~Q(status="completed") | Q(completed_at__isnull=False), name="enrollments_completed_at"),
        ]
        indexes = [models.Index(fields=["participant_user", "status"]), models.Index(fields=["cohort", "status"])]

    def __str__(self):
        return f"{self.participant_user} in {self.cohort}"


class CohortStatusEvent(TimeStampedModel):
    """Immutable history of cohort status changes (SDD refinement)."""

    cohort = models.ForeignKey(Cohort, on_delete=models.RESTRICT, related_name="status_events")
    from_status = models.CharField(max_length=20, null=True, blank=True)
    to_status = models.CharField(max_length=20)
    changed_by = models.ForeignKey(User, on_delete=models.RESTRICT, related_name="+")
    reason = models.TextField(blank=True, default="")

    class Meta:
        db_table = "cohort_status_events"
        ordering = ["created_at"]


class Session(TimeStampedModel):
    SCHEDULED, COMPLETED, CANCELLED = "scheduled", "completed", "cancelled"
    STATUS_CHOICES = [(SCHEDULED, "Scheduled"), (COMPLETED, "Completed"), (CANCELLED, "Cancelled")]

    cohort = models.ForeignKey(Cohort, on_delete=models.RESTRICT, related_name="sessions")
    lesson = models.ForeignKey("curriculum.Lesson", on_delete=models.RESTRICT, null=True, blank=True, related_name="sessions")
    title = models.CharField(max_length=200)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField(null=True, blank=True)
    meeting_type = models.CharField(max_length=20, choices=MEETING_TYPES)
    meet_url = models.URLField(null=True, blank=True)
    venue = models.CharField(max_length=300, null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=SCHEDULED)
    attendance_finalized_at = models.DateTimeField(null=True, blank=True)
    conducted_by = models.ForeignKey(User, on_delete=models.RESTRICT, null=True, blank=True, related_name="+")
    revision = models.IntegerField(default=1)

    class Meta:
        db_table = "sessions"
        ordering = ["starts_at"]
        constraints = [
            models.CheckConstraint(condition=Q(ends_at__isnull=True) | Q(ends_at__gt=F("starts_at")), name="sessions_end_after_start"),
            models.CheckConstraint(condition=Q(meeting_type__in=["physical", "online", "hybrid"]), name="sessions_meeting_type_valid"),
            models.CheckConstraint(condition=Q(status__in=["scheduled", "completed", "cancelled"]), name="sessions_status_valid"),
            models.CheckConstraint(
                condition=(
                    (Q(meeting_type="physical") & Q(venue__isnull=False) & ~Q(venue=""))
                    | (Q(meeting_type="online") & Q(meet_url__isnull=False) & ~Q(meet_url=""))
                    | (Q(meeting_type="hybrid") & Q(venue__isnull=False) & ~Q(venue="") & Q(meet_url__isnull=False) & ~Q(meet_url=""))
                ),
                name="sessions_meeting_details_required",
            ),
            models.CheckConstraint(
                condition=Q(meet_url__isnull=True) | Q(meet_url="") | Q(meet_url__startswith="https://meet.google.com/"),
                name="sessions_meet_url_host",
            ),
        ]
        indexes = [models.Index(fields=["cohort", "starts_at"]), models.Index(fields=["status", "starts_at"])]

    def __str__(self):
        return self.title


class Attendance(TimeStampedModel):
    STATUS_CHOICES = [("present", "Present"), ("absent", "Absent"), ("excused", "Excused")]

    session = models.ForeignKey(Session, on_delete=models.RESTRICT, related_name="attendance")
    enrollment = models.ForeignKey(Enrollment, on_delete=models.RESTRICT, related_name="attendance")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES)
    recorded_by = models.ForeignKey(User, on_delete=models.RESTRICT, related_name="+")
    recorded_at = models.DateTimeField()

    class Meta:
        db_table = "attendance"
        constraints = [
            models.UniqueConstraint(fields=["session", "enrollment"], name="attendance_session_enrollment_unique"),
            models.CheckConstraint(condition=Q(status__in=["present", "absent", "excused"]), name="attendance_status_valid"),
        ]
        indexes = [models.Index(fields=["enrollment", "session"])]
