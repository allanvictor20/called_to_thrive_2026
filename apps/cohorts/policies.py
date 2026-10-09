"""Authorization policies (NFR01): identity, approved role, assigned cohort, own enrollment.

A role alone never grants access to every cohort. Out-of-scope lookups raise
NotFound so that foreign identifiers are consistently concealed.
"""
from apps.accounts.models import UserRole
from apps.core.errors import Forbidden, NotFound

from .models import Cohort, CohortFacilitator, Enrollment


def active_assignment(user, cohort) -> CohortFacilitator | None:
    if not user.is_authenticated or not user.is_approved_facilitator:
        return None
    return CohortFacilitator.objects.filter(cohort=cohort, facilitator_user=user, ended_at__isnull=True).first()


def is_assigned(user, cohort) -> bool:
    return active_assignment(user, cohort) is not None


def is_lead(user, cohort) -> bool:
    a = active_assignment(user, cohort)
    return a is not None and a.assignment_role == "lead"


def facilitator_cohorts(user):
    if not user.is_authenticated or not user.is_approved_facilitator:
        return Cohort.objects.none()
    return Cohort.objects.filter(
        facilitator_assignments__facilitator_user=user, facilitator_assignments__ended_at__isnull=True
    ).distinct()


def require_approved_facilitator(user):
    if not user.is_authenticated or not user.has_role(UserRole.FACILITATOR):
        raise Forbidden("This action is for facilitators.")
    if not user.is_approved_facilitator:
        raise Forbidden("Your facilitator account is waiting for administrator approval.")


def require_admin(user):
    if not user.is_authenticated or not user.is_admin:
        raise Forbidden("This action is for administrators.")


def cohort_for_facilitator(user, cohort_id, *, lead=False) -> Cohort:
    """Return a cohort the user facilitates. Unassigned cohorts are concealed as 404."""
    require_approved_facilitator(user)
    try:
        cohort = Cohort.objects.select_related("partner", "district", "curriculum_version").get(pk=cohort_id)
    except (Cohort.DoesNotExist, ValueError):
        raise NotFound("Cohort not found.")
    assignment = active_assignment(user, cohort)
    if assignment is None:
        raise NotFound("Cohort not found.")
    if lead and assignment.assignment_role != "lead":
        raise Forbidden("Only the lead facilitator can do this.")
    return cohort


def cohort_for_viewer(user, cohort_id) -> Cohort:
    """Facilitator (assigned) or administrator oversight. Admins see programme state, not private payloads."""
    try:
        cohort = Cohort.objects.select_related("partner", "district", "curriculum_version").get(pk=cohort_id)
    except (Cohort.DoesNotExist, ValueError):
        raise NotFound("Cohort not found.")
    if user.is_authenticated and (user.is_admin or is_assigned(user, cohort)):
        return cohort
    raise NotFound("Cohort not found.")


def own_enrollment(user, enrollment_id) -> Enrollment:
    try:
        enrollment = Enrollment.objects.select_related("cohort", "cohort__curriculum_version").get(pk=enrollment_id)
    except (Enrollment.DoesNotExist, ValueError):
        raise NotFound("Enrollment not found.")
    if enrollment.participant_user_id != user.pk:
        raise NotFound("Enrollment not found.")
    return enrollment


def enrollment_for_facilitator(user, enrollment_id, *, lead=False) -> Enrollment:
    try:
        enrollment = Enrollment.objects.select_related("cohort", "participant_user").get(pk=enrollment_id)
    except (Enrollment.DoesNotExist, ValueError):
        raise NotFound("Enrollment not found.")
    cohort_for_facilitator(user, enrollment.cohort_id, lead=lead)
    return enrollment
