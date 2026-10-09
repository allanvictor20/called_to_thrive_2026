"""TC-FR31..FR33, TC09 (reconciliation) and TC11 (export safety)."""
import csv
import io
from datetime import date, timedelta

import pytest
from django.utils import timezone

from apps.accounts.models import UserRole
from apps.cohorts import services as cs
from apps.curriculum.models import Lesson
from apps.learning import services as ls
from apps.projects import services as ps
from apps.reporting import services as rs

from .conftest import complete_lesson, make_user

pytestmark = pytest.mark.django_db


@pytest.fixture
def fixture_world(cohort, lead, partner, partner2, geo, version, enrol, outsider):
    """Repeat enrollment, cancelled cohort, withdrawn member, multiple tasks, incomplete attendance."""
    a, b, c = enrol("Repeat Rita"), enrol("Withdrawn Will"), enrol("Plain Paul")
    cs.withdraw_enrollment(b.participant_user, b)
    # Rita repeats in a second (Jinja) cohort; that cohort is later cancelled.
    other = cs.create_cohort(outsider, partner=partner2, district=geo["jinja"], curriculum_version=version, name="Jinja Intake",
                             start_date=date.today(), end_date=None, default_meeting_type="physical")
    _i, code = cs.create_invite(outsider, other, expires_at=None, max_uses=None)
    a2, _ = cs.join_cohort(a.participant_user, code)
    cs.change_cohort_status(outsider, other, "cancelled", reason="Partner paused programme")
    # Two lessons for Rita, one for Paul
    lessons = list(Lesson.objects.filter(curriculum_version=version).order_by("lesson_number")[:2])
    for lesson in lessons:
        complete_lesson(a, lesson)
    complete_lesson(c, lessons[0])
    # Project with many tasks (must not multiply counts)
    p = ps.create_project(lead, cohort, name="P", problem_statement="x", idea_summary="y")
    m = ps.add_member(lead, p, a, "leader")
    g = ps.add_goal(lead, p, "g", p.revision)
    p.refresh_from_db()
    act = ps.add_action(lead, p, problem_to_solve="p", action_required="a", goal=g, expected_revision=p.revision)
    for i in range(5):
        ps.add_task(lead, p, act, {"description": f"t{i}", "responsible_member": m})
    # Two finalized sessions, one with incomplete attendance; one scheduled (excluded)
    s1 = cs.schedule_session(lead, cohort, title="S1", starts_at=timezone.now() + timedelta(hours=1), ends_at=None, meeting_type="physical", meet_url=None, venue="Hall", lesson=None)
    s1 = cs.record_attendance(lead, s1, {str(a.pk): "present", str(c.pk): "excused"}, s1.revision)
    cs.finalize_session(lead, s1, s1.revision)
    s2 = cs.schedule_session(lead, cohort, title="S2", starts_at=timezone.now() + timedelta(hours=2), ends_at=None, meeting_type="physical", meet_url=None, venue="Hall", lesson=None)
    s2 = cs.record_attendance(lead, s2, {str(a.pk): "present"}, s2.revision)
    cs.finalize_session(lead, s2, s2.revision)
    cs.schedule_session(lead, cohort, title="S3", starts_at=timezone.now() + timedelta(days=2), ends_at=None, meeting_type="physical", meet_url=None, venue="Hall", lesson=None)
    ls.confirm_programme_completion(lead, c, override_reason="Exception for test")
    return {"a": a, "a2": a2, "b": b, "c": c, "other": other}


def test_TC09_reconciliation(fixture_world, cohort):
    cohorts = list(rs.filtered_cohorts(rs.Filters()))
    rows = rs.enrollment_rows(cohorts)
    s = rs.summarize(rows, cohorts)
    assert s["enrollments"] == 4                         # Rita x2, Will, Paul
    assert s["unique_participants"] == 3                 # distinct people
    assert s["started"] == 2                             # Rita (main cohort), Paul
    assert s["lesson_completion_rate"] == round(100 * 3 / (16 * 4), 1)
    assert s["completion_denominator"] == 3              # cancelled cohort excluded; withdrawn included
    assert s["programme_completed"] == 1 and s["programme_completed_by_override"] == 1
    assert s["completion_rate"] == round(100 / 3, 1)
    # Attendance: S1 eligible = Rita, Paul (Will withdrew before S1); S2 = Rita, Paul. Paul excused S1, unrecorded S2.
    assert s["attendance_slots"] == 4 and s["attendance_present"] == 2 and s["attendance_excused"] == 1
    assert s["attendance_unrecorded"] == 1
    assert s["attendance_rate"] == round(100 * 2 / 3, 1)
    assert s["projects"] == 1 and s["projects_completed"] == 0
    assert s["cohorts_by_status"] == {"active": 1, "cancelled": 1}
    rita = next(r for r in rows if r["enrollment_id"] == str(fixture_world["a"].pk))
    assert rita["lessons_completed"] == 2 and rita["in_project"]


def test_filters(fixture_world, cohort, geo, partner2):
    by_district = rs.filtered_cohorts(rs.Filters(district=str(geo["jinja"].pk)))
    assert [c.name for c in by_district] == ["Jinja Intake"]
    by_region = rs.filtered_cohorts(rs.Filters(region=str(geo["central"].pk)))
    assert [c.name for c in by_region] == ["Kampala Youth"]
    assert list(rs.filtered_cohorts(rs.Filters(year=1999))) == []
    assert list(rs.filtered_cohorts(rs.Filters(partner="not-a-uuid"))) == []
    f = rs.Filters(completed_from=timezone.localdate(), completed_to=timezone.localdate())
    cohorts = list(rs.filtered_cohorts(f))
    assert rs.summarize(rs.enrollment_rows(cohorts), cohorts, f)["completed_in_period"] == 1


def test_admin_only_reports(fixture_world, lead, client_for, admin):
    assert client_for(lead).get("/reports/programme/").status_code == 403
    assert client_for(lead).get("/reports/export/").status_code == 403
    assert client_for(lead).get("/api/reports/programme").status_code == 403
    r = client_for(admin).get("/api/reports/programme")
    assert r.status_code == 200 and r.json()["summary"]["unique_participants"] == 3
    assert client_for(admin).get("/reports/programme/?year=2026&region=").status_code == 200


def test_TC11_export_safe(cohort, invite_code, admin, client_for, version):
    from apps.learning.services import submit_response

    from .conftest import component

    evil = make_user("=HYPERLINK(\"http://x\",\"click\")", UserRole.PARTICIPANT)
    e, _ = cs.join_cohort(evil, invite_code)
    for name in ("+SUM(A1)", "-2+3", "@cmd"):
        cs.join_cohort(make_user(name, UserRole.PARTICIPANT), invite_code)
    submit_response(e, component(version, 1, "life_work"), {"what_is_success": "PRIVATE-ANSWER"}, 0)
    r = client_for(admin).get("/reports/export/")
    assert r["Content-Type"].startswith("text/csv")
    body = r.content.decode("utf-8-sig")
    assert "PRIVATE-ANSWER" not in body
    reader = list(csv.DictReader(io.StringIO(body)))
    assert set(reader[0].keys()) == set(rs.EXPORT_COLUMNS)
    names = {row["participant_name"] for row in reader}
    assert "'=HYPERLINK(\"http://x\",\"click\")" in names and "'+SUM(A1)" in names and "'-2+3" in names and "'@cmd" in names
    assert not any(row["participant_name"][0] in "=+-@" for row in reader)
    assert not any("payload" in k or "reflection" in k or "score" in k for k in reader[0].keys())


def test_FR31_facilitator_dashboard_matches_records(cohort, lead, enrol, client_for, version):
    e = enrol("Dashboard Dora")
    complete_lesson(e, Lesson.objects.get(curriculum_version=version, lesson_number=1))
    s = cs.schedule_session(lead, cohort, title="S", starts_at=timezone.now() + timedelta(hours=1), ends_at=None, meeting_type="physical", meet_url=None, venue="Hall", lesson=None)
    s = cs.record_attendance(lead, s, {str(e.pk): "present"}, s.revision)
    cs.finalize_session(lead, s, s.revision)
    html = client_for(lead).get(f"/facilitate/{cohort.pk}/").content.decode()
    assert "Dashboard Dora" in html and "1/16" in html
