"""NFR01 negative authorization tests and API error contract; NFR03 page checks; NFR07 migration/seed."""
import pytest
from django.core.management import call_command
from django.test import Client

from apps.accounts.models import UserRole
from apps.assessment import services as quiz
from apps.assessment.models import Assessment
from apps.cohorts import services as cs
from apps.projects import services as ps

from .conftest import component, make_user

pytestmark = pytest.mark.django_db


def test_unauthenticated_api_401(cohort):
    r = Client().post("/api/cohort-invites/join", {"code": "X"}, content_type="application/json")
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthenticated"


def test_malformed_json_422(enrol, client_for):
    e = enrol()
    r = client_for(e.participant_user).post("/api/cohort-invites/preview", data="[1,2", content_type="application/json")
    assert r.status_code == 422 and set(r.json()["error"]) == {"code", "message", "fields"}


def test_missing_revision_422(enrol, version, client_for):
    e = enrol()
    c = component(version, 1, "life_work")
    r = client_for(e.participant_user).put(f"/api/enrollments/{e.pk}/responses/{c.pk}", {"payload": {}}, content_type="application/json")
    assert r.status_code == 422 and "expected_revision" in r.json()["error"]["fields"]


def test_join_api_201_then_200(cohort, invite_code, client_for):
    user = make_user(None, UserRole.PARTICIPANT)
    c = client_for(user)
    r1 = c.post("/api/cohort-invites/join", {"code": invite_code}, content_type="application/json")
    r2 = c.post("/api/cohort-invites/join", {"code": invite_code}, content_type="application/json")
    assert (r1.status_code, r2.status_code) == (201, 200) and r1.json()["enrollment_id"] == r2.json()["enrollment_id"]


def test_invite_endpoint_throttled(cohort, client_for, settings):
    settings.RATE_LIMITS = {**settings.RATE_LIMITS, "invite": (3, 300)}
    c = client_for(make_user(None, UserRole.PARTICIPANT))
    codes = [c.post("/api/cohort-invites/preview", {"code": "WRONG"}, content_type="application/json").status_code for _ in range(4)]
    assert codes == [404, 404, 404, 429]


class TestForeignIdentifiers:
    """NFR01: foreign cohort, enrollment, project and private response identifiers."""

    def test_foreign_enrollment(self, enrol, version, client_for):
        mine, theirs = enrol(), enrol()
        c = client_for(mine.participant_user)
        comp = component(version, 1, "life_work")
        assert c.get(f"/my/{theirs.pk}/").status_code == 404
        assert c.get(f"/my/{theirs.pk}/lessons/1/").status_code == 404
        assert c.put(f"/api/enrollments/{theirs.pk}/responses/{comp.pk}", {"payload": {}, "expected_revision": 0}, content_type="application/json").status_code == 404
        assert c.post(f"/my/{theirs.pk}/components/{comp.pk}/", {"action": "acknowledge"}).status_code == 404

    def test_foreign_cohort(self, cohort, enrol, outsider, client_for):
        participant = enrol()
        assert client_for(participant.participant_user).get(f"/facilitate/{cohort.pk}/").status_code == 403
        assert client_for(outsider).get(f"/facilitate/{cohort.pk}/").status_code == 404

    def test_foreign_session_attendance(self, cohort, lead, outsider, enrol, client_for):
        from datetime import datetime

        e = enrol()
        s = cs.schedule_session(lead, cohort, title="S", starts_at=datetime(2026, 11, 1, 9), ends_at=None, meeting_type="physical", meet_url=None, venue="Hall", lesson=None)
        r = client_for(outsider).put(f"/api/sessions/{s.pk}/attendance", {"statuses": {str(e.pk): "present"}, "expected_revision": s.revision}, content_type="application/json")
        assert r.status_code == 404
        r = client_for(e.participant_user).put(f"/api/sessions/{s.pk}/attendance", {"statuses": {str(e.pk): "present"}, "expected_revision": s.revision}, content_type="application/json")
        assert r.status_code == 403

    def test_foreign_project(self, cohort, lead, enrol, client_for):
        p = ps.create_project(lead, cohort, name="P", problem_statement="x", idea_summary="y")
        member, stranger = enrol(), enrol()
        ps.add_member(lead, p, member, "leader")
        g = ps.add_goal(lead, p, "g", p.revision)
        p.refresh_from_db()
        act = ps.add_action(lead, p, problem_to_solve="p", action_required="a", goal=g, expected_revision=p.revision)
        t = ps.add_task(lead, p, act, {"description": "t"})
        c = client_for(stranger.participant_user)
        assert c.get(f"/projects/{p.pk}/").status_code == 404
        assert c.patch(f"/api/projects/{p.pk}/tasks/{t.pk}", {"status": "done", "expected_revision": 1}, content_type="application/json").status_code == 404
        assert c.post(f"/api/projects/{p.pk}/confirm-completion", {"confirm": True, "expected_revision": p.revision}, content_type="application/json").status_code == 404
        # a member is not a facilitator
        r = client_for(member.participant_user).post(f"/api/projects/{p.pk}/confirm-completion", {"confirm": True, "expected_revision": p.revision}, content_type="application/json")
        assert r.status_code == 403

    def test_foreign_attempt(self, enrol, version, client_for):
        a, b = enrol(), enrol()
        attempt = quiz.open_attempt(a, Assessment.objects.get(component__lesson__curriculum_version=version))
        r = client_for(b.participant_user).post(f"/api/assessment-attempts/{attempt.pk}/submit", {"expected_revision": 1}, content_type="application/json")
        assert r.status_code == 404

    def test_admin_console_requires_admin(self, lead, enrol, client_for):
        for user in (lead, enrol().participant_user):
            assert client_for(user).get("/admin-console/").status_code == 403


def test_csrf_enforced_on_api(enrol, version):
    e = enrol()
    c = Client(enforce_csrf_checks=True)
    c.force_login(e.participant_user)
    comp = component(version, 1, "life_work")
    r = c.put(f"/api/enrollments/{e.pk}/responses/{comp.pk}", {"payload": {}, "expected_revision": 0}, content_type="application/json")
    assert r.status_code == 403


def test_authenticated_pages_not_cached(enrol, client_for):
    e = enrol()
    r = client_for(e.participant_user).get(f"/my/{e.pk}/")
    assert "no-store" in r["Cache-Control"]


def test_healthz():
    r = Client().get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_pages_have_viewport_and_labels(enrol, client_for):
    e = enrol()
    html = client_for(e.participant_user).get(f"/my/{e.pk}/lessons/2/").content.decode()
    assert 'name="viewport"' in html and 'class="skip"' in html
    assert html.count("<label") >= 10


def test_NFR07_seed_and_import_on_fresh_database(settings):
    settings.DEBUG = True
    call_command("seed_demo")
    from apps.cohorts.models import Cohort

    assert Cohort.objects.filter(name="Kampala Youth — Demo").exists()
