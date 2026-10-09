"""TC-FR11..FR14 and TC06 (attendance independence)."""
from datetime import datetime, timedelta

import pytest
from django.utils import timezone

from apps.cohorts import services as cs
from apps.cohorts.models import Attendance
from apps.core.errors import Conflict, ValidationFailed
from apps.curriculum.models import Lesson
from apps.learning import services as ls
from apps.learning.models import LessonProgress

from .conftest import component

pytestmark = pytest.mark.django_db
MEET = "https://meet.google.com/abc-defg-hij"


def schedule(cohort, lead, **kw):
    base = dict(title="Week 1", starts_at=datetime(2026, 11, 7, 10, 0), ends_at=datetime(2026, 11, 7, 12, 0),
                meeting_type="physical", meet_url=None, venue="Church hall", lesson=None)
    base.update(kw)
    return cs.schedule_session(lead, cohort, **base)


class TestFR11FR12Scheduling:
    def test_local_time_stored_as_utc(self, cohort, lead):
        s = schedule(cohort, lead)
        assert s.starts_at.astimezone(timezone.get_fixed_timezone(180)).hour == 10
        assert s.starts_at.astimezone(timezone.get_fixed_timezone(0)).hour == 7

    @pytest.mark.parametrize("kw,field", [
        ({"meeting_type": "physical", "venue": ""}, "venue"),
        ({"meeting_type": "online", "venue": None, "meet_url": None}, "meet_url"),
        ({"meeting_type": "hybrid", "meet_url": MEET, "venue": ""}, "venue"),
        ({"meeting_type": "hybrid", "meet_url": None, "venue": "Hall"}, "meet_url"),
        ({"ends_at": datetime(2026, 11, 7, 9, 0)}, "ends_at"),
        ({"meeting_type": "online", "meet_url": "https://zoom.us/j/123"}, "meet_url"),
        ({"meeting_type": "online", "meet_url": "http://meet.google.com/abc-defg-hij"}, "meet_url"),
        ({"meeting_type": "online", "meet_url": "javascript:alert(1)"}, "meet_url"),
        ({"meeting_type": "online", "meet_url": "https://meet.google.com.evil.example/abc"}, "meet_url"),
    ])
    def test_invalid_sessions_rejected(self, cohort, lead, kw, field):
        with pytest.raises(ValidationFailed) as exc:
            schedule(cohort, lead, **kw)
        assert field in exc.value.fields

    def test_valid_online_and_hybrid(self, cohort, lead):
        assert schedule(cohort, lead, meeting_type="online", meet_url=MEET, venue=None).meet_url == MEET
        assert schedule(cohort, lead, meeting_type="hybrid", meet_url=MEET, venue="Hall").venue == "Hall"

    def test_lesson_from_other_version_rejected(self, cohort, lead, version2):
        other = Lesson.objects.get(curriculum_version=version2, lesson_number=1)
        with pytest.raises(ValidationFailed):
            schedule(cohort, lead, lesson=other)

    def test_join_link_opens_external_meet_without_session_token(self, cohort, lead, enrol, client_for):
        e = enrol()
        cs.schedule_session(lead, cohort, title="Online", starts_at=timezone.now() + timedelta(days=1), ends_at=None,
                            meeting_type="online", meet_url=MEET, venue=None, lesson=None)
        html = client_for(e.participant_user).get(f"/my/{e.pk}/").content.decode()
        assert f'href="{MEET}" target="_blank" rel="noopener noreferrer"' in html


class TestFR13Attendance:
    def test_missing_is_unrecorded_not_absent(self, cohort, lead, enrol):
        a, b = enrol(), enrol()
        s = schedule(cohort, lead)
        s = cs.record_attendance(lead, s, {str(a.pk): "present"}, s.revision)
        summary = cs.attendance_summary(s)
        assert summary == {"eligible": 2, "present": 1, "absent": 0, "excused": 0, "unrecorded": 1}
        assert not Attendance.objects.filter(enrollment=b).exists()

    def test_cross_cohort_attendance_rejected(self, cohort, lead, partner, geo, version, enrol):
        from datetime import date

        other = cs.create_cohort(lead, partner=partner, district=geo["kampala"], curriculum_version=version, name="Other",
                                 start_date=date.today(), end_date=None, default_meeting_type="physical")
        _i, code = cs.create_invite(lead, other, expires_at=None, max_uses=None)
        foreign = enrol(code=code)
        s = schedule(cohort, lead)
        with pytest.raises(ValidationFailed):
            cs.record_attendance(lead, s, {str(foreign.pk): "present"}, s.revision)

    def test_stale_batch_revision_conflicts(self, cohort, lead, enrol):
        a = enrol()
        s = schedule(cohort, lead)
        rev = s.revision
        cs.record_attendance(lead, s, {str(a.pk): "present"}, rev)
        with pytest.raises(Conflict):
            cs.record_attendance(lead, s, {str(a.pk): "absent"}, rev)
        assert Attendance.objects.get(enrollment=a).status == "present"

    def test_review_step_before_save(self, cohort, lead, enrol, client_for):
        a = enrol()
        s = schedule(cohort, lead)
        c = client_for(lead)
        r = c.post(f"/facilitate/sessions/{s.pk}/attendance/", {"action": "review", "revision": s.revision, f"att_{a.pk}": "present"})
        assert r.status_code == 200 and b"Review before saving" in r.content
        assert not Attendance.objects.exists()
        c.post(f"/facilitate/sessions/{s.pk}/attendance/", {"action": "save", "revision": s.revision, f"att_{a.pk}": "present"})
        assert Attendance.objects.get(enrollment=a).status == "present"

    def test_attendance_api(self, cohort, lead, enrol, client_for):
        a = enrol()
        s = schedule(cohort, lead)
        r = client_for(lead).put(f"/api/sessions/{s.pk}/attendance", {"statuses": {str(a.pk): "excused"}, "expected_revision": s.revision}, content_type="application/json")
        assert r.status_code == 200 and r.json()["summary"]["excused"] == 1
        r = client_for(lead).put(f"/api/sessions/{s.pk}/attendance", {"statuses": {str(a.pk): "absent"}, "expected_revision": s.revision}, content_type="application/json")
        assert r.status_code == 409

    def test_retry_of_same_attendance_creates_no_duplicates(self, cohort, lead, enrol):
        a = enrol()
        s = schedule(cohort, lead)
        s2 = cs.record_attendance(lead, s, {str(a.pk): "present"}, s.revision)
        cs.record_attendance(lead, s2, {str(a.pk): "present"}, s2.revision)
        assert Attendance.objects.filter(enrollment=a, session=s).count() == 1


class TestFR14TC06Independence:
    def test_finalizing_does_not_complete_life_work(self, cohort, lead, enrol, version):
        e = enrol()
        lesson1 = Lesson.objects.get(curriculum_version=version, lesson_number=1)
        s = schedule(cohort, lead, lesson=lesson1)
        s = cs.record_attendance(lead, s, {str(e.pk): "present"}, s.revision)
        s = cs.finalize_session(lead, s, s.revision)
        assert s.status == "completed" and Attendance.objects.get(enrollment=e).status == "present"
        assert not LessonProgress.objects.filter(enrollment=e, lesson=lesson1, completed_at__isnull=False).exists()
        # Complete Life Work later: progress completes; attendance unchanged.
        ls.acknowledge(e, component(version, 1, "scripture"))
        ls.submit_response(e, component(version, 1, "case_study"), {"inspiration": "x", "positive_difference": "y"}, 0)
        ls.submit_response(e, component(version, 1, "life_work"), {"what_is_success": "z"}, 0)
        assert LessonProgress.objects.get(enrollment=e, lesson=lesson1).completed_at is not None
        assert Attendance.objects.get(enrollment=e).status == "present"

    def test_cancel_keeps_learning(self, cohort, lead, enrol, version):
        e = enrol()
        ls.acknowledge(e, component(version, 1, "scripture"))
        s = cs.cancel_session(lead, schedule(cohort, lead))
        assert s.status == "cancelled"
        assert e.responses.count() == 1
        with pytest.raises(ValidationFailed):
            cs.record_attendance(lead, s, {str(e.pk): "present"}, s.revision)
