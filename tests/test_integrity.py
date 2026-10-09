"""TC02 and NFR02: direct database writes that break scope invariants must fail without partial records."""
from datetime import date

import pytest
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from apps.assessment.models import Assessment, AssessmentAnswer, AssessmentAttempt
from apps.cohorts import services as cs
from apps.cohorts.models import Attendance, CohortFacilitator, CohortInvite, Session
from apps.learning.models import ComponentResponse, LessonProgress
from apps.projects import services as ps
from apps.projects.models import ProjectMember, ProjectTask

from .conftest import component, make_facilitator

pytestmark = pytest.mark.django_db


def fails(fn):
    with pytest.raises(DatabaseError), transaction.atomic():
        fn()


@pytest.fixture
def two_cohorts(cohort, lead, partner, geo, version2, enrol):
    other = cs.create_cohort(lead, partner=partner, district=geo["kampala"], curriculum_version=version2, name="Cohort B",
                             start_date=date.today(), end_date=None, default_meeting_type="physical")
    _i, code = cs.create_invite(lead, other, expires_at=None, max_uses=None)
    return {"a": cohort, "b": other, "ea": enrol(), "eb": enrol(code=code)}


def test_attendance_cross_cohort(two_cohorts, lead):
    s = Session.objects.create(cohort=two_cohorts["a"], title="S", starts_at=timezone.now(), meeting_type="physical", venue="Hall")
    fails(lambda: Attendance.objects.create(session=s, enrollment=two_cohorts["eb"], status="present", recorded_by=lead, recorded_at=timezone.now()))
    assert not Attendance.objects.exists()


def test_response_from_other_curriculum_version(two_cohorts, version2):
    fails(lambda: ComponentResponse.objects.create(enrollment=two_cohorts["ea"], component=component(version2, 1, "life_work"), payload={}))
    assert not ComponentResponse.objects.exists()


def test_progress_from_other_curriculum_version(two_cohorts, version2):
    from apps.curriculum.models import Lesson

    fails(lambda: LessonProgress.objects.create(enrollment=two_cohorts["ea"], lesson=Lesson.objects.get(curriculum_version=version2, lesson_number=1)))


def test_attempt_and_answers_scope(two_cohorts, version, version2):
    a_v2 = Assessment.objects.get(component__lesson__curriculum_version=version2)
    fails(lambda: AssessmentAttempt.objects.create(enrollment=two_cohorts["ea"], assessment=a_v2, attempt_number=1, scoring_version="x"))
    a_v1 = Assessment.objects.get(component__lesson__curriculum_version=version)
    attempt = AssessmentAttempt.objects.create(enrollment=two_cohorts["ea"], assessment=a_v1, attempt_number=1, scoring_version="x")
    fails(lambda: AssessmentAnswer.objects.create(attempt=attempt, question=a_v2.questions.first(), value=3))
    fails(lambda: AssessmentAnswer.objects.create(attempt=attempt, question=a_v1.questions.first(), value=6))
    fails(lambda: AssessmentAnswer.objects.create(attempt=attempt, question=a_v1.questions.first(), value=0))
    assert not AssessmentAnswer.objects.exists()


def test_project_member_cross_cohort(two_cohorts, lead):
    p = ps.create_project(lead, two_cohorts["a"], name="P", problem_statement="x", idea_summary="y")
    fails(lambda: ProjectMember.objects.create(project=p, enrollment=two_cohorts["eb"], team_role="member"))


def test_task_assignee_cross_project(cohort, lead, enrol):
    p1 = ps.create_project(lead, cohort, name="P1", problem_statement="x", idea_summary="y")
    p2 = ps.create_project(lead, cohort, name="P2", problem_statement="x", idea_summary="y")
    m2 = ps.add_member(lead, p2, enrol(), "member")
    act = ps.add_action(lead, p1, problem_to_solve="p", action_required="a", goal=None, expected_revision=p1.revision)
    fails(lambda: ProjectTask.objects.create(action=act, description="t", responsible_member=m2))
    assert not ProjectTask.objects.exists()


def test_unapproved_facilitator_assignment_blocked(cohort, admin, partner):
    pending = make_facilitator(admin, partner, status="pending")
    fails(lambda: CohortFacilitator.objects.create(cohort=cohort, facilitator_user=pending, assignment_role="assistant"))


def test_unaffiliated_facilitator_assignment_blocked(cohort, admin):
    stranger = make_facilitator(admin, None)
    fails(lambda: CohortFacilitator.objects.create(cohort=cohort, facilitator_user=stranger, assignment_role="assistant"))


def test_invite_capacity_constraint(cohort, lead):
    invite, _ = cs.create_invite(lead, cohort, expires_at=None, max_uses=1)
    fails(lambda: CohortInvite.objects.filter(pk=invite.pk).update(use_count=2))


def test_session_details_constraint(cohort):
    fails(lambda: Session.objects.create(cohort=cohort, title="x", starts_at=timezone.now(), meeting_type="online"))
    fails(lambda: Session.objects.create(cohort=cohort, title="x", starts_at=timezone.now(), meeting_type="online", meet_url="https://zoom.us/j/1"))


def test_scores_are_immutable(enrol, version):
    from apps.assessment import services as quiz

    e = enrol()
    a = Assessment.objects.get(component__lesson__curriculum_version=version)
    attempt = quiz.open_attempt(e, a)
    quiz.submit_attempt(attempt, {str(n): 3 for n in range(1, 25)}, attempt.revision)
    with connection.cursor() as cur:
        with pytest.raises(DatabaseError), transaction.atomic():
            cur.execute("UPDATE assessment_scores SET score = 20")
        with pytest.raises(DatabaseError), transaction.atomic():
            cur.execute("UPDATE assessment_answers SET value = 5")
