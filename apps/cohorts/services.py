import hashlib
import re
import secrets
from datetime import date, datetime
from zoneinfo import ZoneInfo

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from apps.accounts.models import FacilitatorProfile, User
from apps.core.errors import Conflict, Forbidden, NotFound, ValidationFailed
from apps.curriculum.models import CurriculumVersion, Lesson
from apps.programme.models import District, Partner, PartnerFacilitator

from .models import Attendance, Cohort, CohortFacilitator, CohortInvite, CohortStatusEvent, Enrollment, Session

MEET_URL_RE = re.compile(r"^https://meet\.google\.com/[A-Za-z0-9][A-Za-z0-9\-]{2,}(?:\?[A-Za-z0-9=&_\-]*)?$")
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O/1/I/L confusion
CODE_LENGTH = 12


# ------------------------------------------------------------------ helpers

def validate_meet_url(url: str | None, field: str = "meet_url") -> str | None:
    if not url:
        return None
    url = url.strip()
    if not MEET_URL_RE.match(url):
        raise ValidationFailed(
            "Enter a Google Meet link.", fields={field: "Use an https://meet.google.com/... link. Other sites are not accepted."}
        )
    return url


def normalize_code(code: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (code or "").upper())


def digest_code(code: str) -> str:
    return hashlib.sha256(normalize_code(code).encode()).hexdigest()


def _check_affiliation(user: User, partner: Partner):
    profile = getattr(user, "facilitator_profile", None)
    if profile is None or profile.approval_status != FacilitatorProfile.APPROVED:
        raise ValidationFailed("That facilitator is not approved.", fields={"facilitator": "Facilitator must be approved by an administrator."})
    if not PartnerFacilitator.objects.filter(partner=partner, facilitator_user=user, status="active").exists():
        raise ValidationFailed(
            "That facilitator is not affiliated with this partner.",
            fields={"facilitator": "An administrator must affiliate the facilitator with this partner first."},
        )


# ------------------------------------------------------------------ cohorts

@transaction.atomic
def create_cohort(
    actor: User,
    *,
    partner: Partner,
    district: District,
    curriculum_version: CurriculumVersion,
    name: str,
    start_date: date,
    end_date: date | None,
    default_meeting_type: str,
    default_meet_url: str | None = None,
    lead: User | None = None,
) -> Cohort:
    """FR06: create a cohort and assign an approved lead atomically."""
    if not (actor.is_admin or actor.is_approved_facilitator):
        raise Forbidden("Only approved facilitators or administrators can create cohorts.")
    lead = lead or actor
    if lead != actor and not actor.is_admin:
        raise Forbidden("Only an administrator can assign another lead facilitator.")
    errors = {}
    if not name or not name.strip():
        errors["name"] = "Give the cohort a name."
    if end_date and start_date and end_date < start_date:
        errors["end_date"] = "End date cannot be before the start date."
    if curriculum_version.status != CurriculumVersion.PUBLISHED:
        errors["curriculum_version"] = "Choose a published curriculum version."
    if partner.status != "active":
        errors["partner"] = "This partner is archived."
    if default_meeting_type not in {"physical", "online", "hybrid"}:
        errors["default_meeting_type"] = "Choose physical, online or hybrid."
    if default_meeting_type in {"online", "hybrid"} and default_meet_url:
        default_meet_url = validate_meet_url(default_meet_url, "default_meet_url")
    if errors:
        raise ValidationFailed("Please correct the highlighted fields.", fields=errors)
    _check_affiliation(lead, partner)
    cohort = Cohort.objects.create(
        partner=partner,
        district=district,
        curriculum_version=curriculum_version,
        name=name.strip(),
        start_date=start_date,
        end_date=end_date,
        default_meeting_type=default_meeting_type,
        default_meet_url=default_meet_url or None,
    )
    CohortFacilitator.objects.create(cohort=cohort, facilitator_user=lead, assignment_role="lead")
    CohortStatusEvent.objects.create(cohort=cohort, from_status=None, to_status=Cohort.PLANNED, changed_by=actor, reason="Cohort created")
    return cohort


@transaction.atomic
def assign_facilitator(actor: User, cohort: Cohort, user: User, role: str) -> CohortFacilitator:
    """FR07: scoped lead/assistant assignments. Replacing the lead never leaves two active leads."""
    if role not in {"lead", "assistant"}:
        raise ValidationFailed("Choose lead or assistant.", fields={"role": "Choose lead or assistant."})
    from .policies import is_lead

    if not (actor.is_admin or is_lead(actor, cohort)):
        raise Forbidden("Only the lead facilitator or an administrator can change facilitators.")
    _check_affiliation(user, cohort.partner)
    Cohort.objects.select_for_update().get(pk=cohort.pk)
    now = timezone.now()
    current_lead = CohortFacilitator.objects.filter(cohort=cohort, assignment_role="lead", ended_at__isnull=True).first()
    if role == "assistant" and current_lead is not None and current_lead.facilitator_user_id == user.pk:
        raise ValidationFailed("Assign a new lead first; a cohort always keeps one active lead.")
    if role == "lead":
        CohortFacilitator.objects.filter(cohort=cohort, assignment_role="lead", ended_at__isnull=True).exclude(
            facilitator_user=user
        ).update(assignment_role="assistant")
    assignment, created = CohortFacilitator.objects.get_or_create(
        cohort=cohort, facilitator_user=user, defaults={"assignment_role": role}
    )
    if not created:
        assignment.assignment_role = role
        assignment.ended_at = None
        assignment.assigned_at = now
        assignment.save()
    return assignment


@transaction.atomic
def end_assignment(actor: User, cohort: Cohort, user: User) -> None:
    from .policies import is_lead

    if not (actor.is_admin or is_lead(actor, cohort)):
        raise Forbidden("Only the lead facilitator or an administrator can change facilitators.")
    assignment = CohortFacilitator.objects.select_for_update().filter(cohort=cohort, facilitator_user=user, ended_at__isnull=True).first()
    if assignment is None:
        raise NotFound("Assignment not found.")
    if assignment.assignment_role == "lead":
        raise ValidationFailed("Assign a new lead before removing the current lead.")
    assignment.ended_at = timezone.now()
    assignment.save(update_fields=["ended_at"])


def completion_review(cohort: Cohort) -> dict:
    from apps.learning.services import programme_checklist

    rows = []
    for e in cohort.enrollments.select_related("participant_user").order_by("participant_user__full_name"):
        rows.append({"enrollment": e, "checklist": programme_checklist(e)})
    return {
        "rows": rows,
        "eligible": sum(1 for r in rows if r["checklist"]["eligible"]),
        "completed": sum(1 for r in rows if r["enrollment"].status == Enrollment.COMPLETED),
        "total": len(rows),
    }


@transaction.atomic
def change_cohort_status(actor: User, cohort: Cohort, to_status: str, *, reason: str = "", reviewed: bool = False) -> Cohort:
    """FR10: managed states with history. Completion needs reviewed eligibility or an explicit correction."""
    from .policies import is_lead

    cohort = Cohort.objects.select_for_update().get(pk=cohort.pk)
    if not (actor.is_admin or is_lead(actor, cohort)):
        raise Forbidden("Only the lead facilitator or an administrator can change cohort status.")
    allowed = Cohort.TRANSITIONS.get(cohort.status, set())
    if to_status not in allowed:
        raise ValidationFailed(f"A {cohort.status} cohort cannot move to {to_status}.")
    reason = (reason or "").strip()
    if to_status == Cohort.COMPLETED and not reviewed:
        raise ValidationFailed("Review participant eligibility before completing the cohort.", fields={"reviewed": "Confirm you reviewed eligibility."})
    if cohort.status == Cohort.COMPLETED and to_status == Cohort.ACTIVE:
        if not actor.is_admin:
            raise Forbidden("Reopening a completed cohort is an administrator correction.")
        if not reason:
            raise ValidationFailed("A reason is required to reopen a completed cohort.", fields={"reason": "Give a reason."})
    if to_status == Cohort.CANCELLED and not reason:
        raise ValidationFailed("A reason is required to cancel a cohort.", fields={"reason": "Give a reason."})
    previous = cohort.status
    cohort.status = to_status
    cohort.completed_at = timezone.now() if to_status == Cohort.COMPLETED else (None if to_status == Cohort.ACTIVE else cohort.completed_at)
    cohort.save(update_fields=["status", "completed_at", "updated_at"])
    CohortStatusEvent.objects.create(cohort=cohort, from_status=previous, to_status=to_status, changed_by=actor, reason=reason)
    return cohort


@transaction.atomic
def set_current_lesson(actor: User, cohort: Cohort, lesson: Lesson | None) -> Cohort:
    if lesson is not None and lesson.curriculum_version_id != cohort.curriculum_version_id:
        raise ValidationFailed("That lesson is not part of this cohort's curriculum.")
    cohort.current_lesson = lesson
    cohort.save(update_fields=["current_lesson", "updated_at"])
    return cohort


# ------------------------------------------------------------------ invites

@transaction.atomic
def create_invite(actor: User, cohort: Cohort, *, expires_at: datetime | None, max_uses: int | None) -> tuple[CohortInvite, str]:
    """Generate a random join code. Only its digest is stored; the code is shown once."""
    if max_uses is not None and max_uses < 1:
        raise ValidationFailed("Seats must be at least 1.", fields={"max_uses": "Use a positive number or leave blank."})
    if expires_at is not None and expires_at <= timezone.now():
        raise ValidationFailed("Expiry must be in the future.", fields={"expires_at": "Choose a future date."})
    code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
    invite = CohortInvite.objects.create(
        cohort=cohort, code_digest=digest_code(code), code_hint=code[-4:], expires_at=expires_at, max_uses=max_uses, created_by=actor
    )
    return invite, code


def format_code(code: str) -> str:
    return "-".join(code[i : i + 4] for i in range(0, len(code), 4))


@transaction.atomic
def revoke_invite(actor: User, invite: CohortInvite) -> CohortInvite:
    if invite.revoked_at is None:
        invite.revoked_at = timezone.now()
        invite.save(update_fields=["revoked_at", "updated_at"])
    return invite


def _find_invite(code: str, *, lock: bool = False) -> CohortInvite:
    qs = CohortInvite.objects.select_related("cohort", "cohort__partner", "cohort__district")
    if lock:
        qs = qs.select_for_update(of=("self",))
    invite = qs.filter(code_digest=digest_code(code)).first()
    if invite is None:
        raise NotFound("That join code was not found. Check it and try again.", code="invite_not_found")
    return invite


def _invite_problem(invite: CohortInvite) -> str | None:
    if invite.revoked_at is not None:
        return "This join code has been withdrawn. Ask your facilitator for a new one."
    if invite.expires_at is not None and invite.expires_at <= timezone.now():
        return "This join code has expired. Ask your facilitator for a new one."
    if invite.cohort.status not in {Cohort.PLANNED, Cohort.ACTIVE}:
        return "This cohort is no longer taking new participants."
    if invite.max_uses is not None and invite.use_count >= invite.max_uses:
        return "This cohort is full."
    return None


def preview_invite(user: User, code: str) -> dict:
    """FR08: safe preview — cohort, partner and lead facilitator only. Does not enroll or show the roster."""
    invite = _find_invite(code)
    cohort = invite.cohort
    lead = cohort.lead
    existing = Enrollment.objects.filter(cohort=cohort, participant_user=user).first() if user.is_authenticated else None
    return {
        "cohort_id": str(cohort.pk),
        "cohort_name": cohort.name,
        "partner": cohort.partner.name,
        "district": cohort.district.name,
        "lead_facilitator": lead.full_name if lead else None,
        "start_date": cohort.start_date.isoformat(),
        "delivery": cohort.get_default_meeting_type_display(),
        "already_enrolled": existing is not None,
        "problem": None if existing else _invite_problem(invite),
    }


def join_cohort(user: User, code: str) -> tuple[Enrollment, bool]:
    """FR08/FR09/NFR06: atomic redemption.

    Lock invite; return an existing enrollment first (replay consumes no use);
    check revocation, expiry, capacity and cohort state; insert; increment once.
    The unique (cohort, participant) constraint resolves parallel requests.
    """
    if not user.has_verified_contact:
        raise Forbidden("Verify your email or phone before joining a cohort.", code="contact_unverified")
    try:
        with transaction.atomic():
            invite = _find_invite(code, lock=True)
            existing = Enrollment.objects.filter(cohort=invite.cohort, participant_user=user).first()
            if existing is not None:
                if existing.status == Enrollment.WITHDRAWN:
                    # Rejoining resumes the same enrollment and its earlier work.
                    existing.status = Enrollment.ACTIVE
                    existing.withdrawn_at = None
                    existing.save(update_fields=["status", "withdrawn_at", "updated_at"])
                return existing, False
            problem = _invite_problem(invite)
            if problem:
                raise ValidationFailed(problem, code="invite_unavailable")
            enrollment = Enrollment.objects.create(cohort=invite.cohort, participant_user=user, invite=invite)
            CohortInvite.objects.filter(pk=invite.pk).update(use_count=F("use_count") + 1)
            return enrollment, True
    except IntegrityError:
        # A parallel request for the same participant won the race: return its enrollment.
        enrollment = Enrollment.objects.filter(cohort__invites__code_digest=digest_code(code), participant_user=user).first()
        if enrollment is None:
            raise
        return enrollment, False


@transaction.atomic
def withdraw_enrollment(actor: User, enrollment: Enrollment) -> Enrollment:
    """Withdrawal preserves prior work."""
    if enrollment.status == Enrollment.ACTIVE:
        enrollment.status = Enrollment.WITHDRAWN
        enrollment.withdrawn_at = timezone.now()
        enrollment.save(update_fields=["status", "withdrawn_at", "updated_at"])
    return enrollment


# ------------------------------------------------------------------ sessions

def local_to_utc(cohort: Cohort, value: datetime) -> datetime:
    if timezone.is_aware(value):
        return value
    return value.replace(tzinfo=ZoneInfo(cohort.timezone))


@transaction.atomic
def schedule_session(
    actor: User,
    cohort: Cohort,
    *,
    title: str,
    starts_at: datetime,
    ends_at: datetime | None,
    meeting_type: str,
    meet_url: str | None,
    venue: str | None,
    lesson: Lesson | None,
    session: Session | None = None,
) -> Session:
    """FR11: physical needs venue; online needs a Meet link; hybrid needs both. Times entered in cohort local time."""
    errors = {}
    starts_at = local_to_utc(cohort, starts_at) if starts_at else None
    ends_at = local_to_utc(cohort, ends_at) if ends_at else None
    venue = (venue or "").strip() or None
    if not title or not title.strip():
        errors["title"] = "Give the session a title."
    if starts_at is None:
        errors["starts_at"] = "Choose a start time."
    if ends_at and starts_at and ends_at <= starts_at:
        errors["ends_at"] = "End time must be after the start time."
    if meeting_type not in {"physical", "online", "hybrid"}:
        errors["meeting_type"] = "Choose physical, online or hybrid."
    if meeting_type in {"physical", "hybrid"} and not venue:
        errors["venue"] = "A venue is required for physical and hybrid sessions."
    if meeting_type in {"online", "hybrid"}:
        if not meet_url:
            errors["meet_url"] = "A Google Meet link is required for online and hybrid sessions."
        else:
            try:
                meet_url = validate_meet_url(meet_url)
            except ValidationFailed as exc:
                errors.update(exc.fields)
    else:
        meet_url = None
    if meeting_type == "online":
        venue = None
    if lesson is not None and lesson.curriculum_version_id != cohort.curriculum_version_id:
        errors["lesson"] = "That lesson is not part of this cohort's curriculum."
    if errors:
        raise ValidationFailed("Please correct the highlighted fields.", fields=errors)
    fields = dict(title=title.strip(), starts_at=starts_at, ends_at=ends_at, meeting_type=meeting_type, meet_url=meet_url, venue=venue, lesson=lesson)
    if session is None:
        return Session.objects.create(cohort=cohort, **fields)
    if session.status != Session.SCHEDULED:
        raise ValidationFailed("Only scheduled sessions can be edited.")
    for k, v in fields.items():
        setattr(session, k, v)
    session.save()
    return session


def eligible_enrollments(session: Session):
    """Participants who can have attendance recorded: enrolled in the cohort and not withdrawn before the session."""
    from django.db.models import Q

    return (
        Enrollment.objects.filter(cohort_id=session.cohort_id)
        .filter(Q(withdrawn_at__isnull=True) | Q(withdrawn_at__gt=session.starts_at))
        .select_related("participant_user")
        .order_by("participant_user__full_name")
    )


@transaction.atomic
def record_attendance(actor: User, session: Session, statuses: dict, expected_revision: int) -> Session:
    """FR13/NFR06: batch attendance under a session lock and revision.

    statuses maps enrollment id -> 'present' | 'absent' | 'excused' | None.
    None (or missing) leaves the participant unrecorded; it is never treated as absent.
    """
    session = Session.objects.select_for_update().get(pk=session.pk)
    if session.status == Session.CANCELLED:
        raise ValidationFailed("Attendance cannot be recorded for a cancelled session.")
    if session.revision != int(expected_revision):
        raise Conflict("Attendance was changed by someone else. Reload to see the latest before saving.")
    eligible = {str(e.pk): e for e in eligible_enrollments(session)}
    errors = {}
    for enrollment_id, status in statuses.items():
        if str(enrollment_id) not in eligible:
            errors[str(enrollment_id)] = "This participant is not enrolled in this cohort."
        elif status not in (None, "", "present", "absent", "excused"):
            errors[str(enrollment_id)] = "Choose present, absent or excused."
    if errors:
        raise ValidationFailed("Some attendance entries are not valid.", fields=errors)
    now = timezone.now()
    for enrollment_id, status in statuses.items():
        enrollment = eligible[str(enrollment_id)]
        if not status:
            Attendance.objects.filter(session=session, enrollment=enrollment).delete()
            continue
        Attendance.objects.update_or_create(
            session=session, enrollment=enrollment, defaults={"status": status, "recorded_by": actor, "recorded_at": now}
        )
    session.revision += 1
    session.conducted_by = session.conducted_by or actor
    session.save(update_fields=["revision", "conducted_by", "updated_at"])
    return session


@transaction.atomic
def finalize_session(actor: User, session: Session, expected_revision: int) -> Session:
    """FR14: finalize independently of lesson work. Does not complete anyone's Life Work."""
    session = Session.objects.select_for_update().get(pk=session.pk)
    if session.revision != int(expected_revision):
        raise Conflict("Attendance changed since you reviewed it. Reload before finalizing.")
    if session.status == Session.CANCELLED:
        raise ValidationFailed("A cancelled session cannot be finalized.")
    session.status = Session.COMPLETED
    session.attendance_finalized_at = timezone.now()
    session.conducted_by = session.conducted_by or actor
    session.revision += 1
    session.save(update_fields=["status", "attendance_finalized_at", "conducted_by", "revision", "updated_at"])
    return session


@transaction.atomic
def cancel_session(actor: User, session: Session) -> Session:
    """Cancelling a meeting never erases learning or attendance history."""
    session = Session.objects.select_for_update().get(pk=session.pk)
    if session.status == Session.COMPLETED:
        raise ValidationFailed("A finalized session cannot be cancelled.")
    session.status = Session.CANCELLED
    session.revision += 1
    session.save(update_fields=["status", "revision", "updated_at"])
    return session


def attendance_summary(session: Session) -> dict:
    eligible = list(eligible_enrollments(session))
    recorded = {a.enrollment_id: a.status for a in session.attendance.all()}
    counts = {"present": 0, "absent": 0, "excused": 0, "unrecorded": 0}
    for e in eligible:
        counts[recorded.get(e.pk, "unrecorded")] += 1
    return {"eligible": len(eligible), **counts}
