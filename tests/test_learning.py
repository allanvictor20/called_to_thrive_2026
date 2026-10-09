"""TC-FR15..FR21, FR34 and TC03, TC08, TC12."""
import json
import logging

import pytest
from django.db import DatabaseError, transaction

from apps.cohorts import services as cs
from apps.core.errors import Conflict, Forbidden, NotFound, ValidationFailed
from apps.curriculum.models import CurriculumVersion, Lesson, LessonComponent
from apps.learning import services as ls
from apps.learning.models import ComponentResponse, LessonProgress

from .conftest import component, make_facilitator

pytestmark = pytest.mark.django_db


class TestFR15CurriculumFidelity:
    """Automated spot checks against the workbook. Curriculum-owner review is still required."""

    def test_all_workbook_lessons_present(self, version):
        titles = list(Lesson.objects.filter(curriculum_version=version).order_by("lesson_number").values_list("title", flat=True))
        assert len(titles) == 16
        assert titles[0] == "Being Who I Am Called to Be" and titles[4] == "Called to Responsibility"
        assert titles[14] == "Called to Evaluate" and titles[15] == "Putting it all Together"

    def test_every_lesson_has_gods_truth(self, version):
        for lesson in Lesson.objects.filter(curriculum_version=version):
            assert lesson.components.filter(component_type="truth").exists(), lesson

    @pytest.mark.parametrize("n,reference", [
        (1, "1 Peter 4:10-11"), (1, "Ephesians 2:10"), (3, "Genesis 1:27"), (4, "Psalm 139:1-6"), (5, "John 15:8"),
        (6, "1 Samuel 17:36-37"), (8, "Philippians 4:13"), (9, "Acts 16:13-15"), (11, "Romans 12:4-5"), (12, "Psalm 37:23"),
        (15, "Micah 6:8"), (16, "Psalm 139:1"),
    ])
    def test_scripture_references(self, version, n, reference):
        refs = [b.get("reference") for c in Lesson.objects.get(curriculum_version=version, lesson_number=n).components.all()
                for b in c.participant_content.get("blocks", [])]
        assert reference in refs

    def test_life_work_required_where_present(self, version):
        for c in LessonComponent.objects.filter(lesson__curriculum_version=version, component_type="life_work"):
            assert c.required and c.completion_method == "submission"

    def test_mentorship_payload_shape_matches_design(self, version):
        keys = [f["key"] for f in component(version, 7, "life_work").response_schema["fields"]]
        assert keys == ["areas", "potential_mentor", "reason", "approach"]


class TestFR16Views:
    def test_participant_payload_excludes_facilitator_content(self, version, enrol, client_for):
        e = enrol()
        quiz = component(version, 5, "career_quiz")
        assert quiz.facilitator_content
        view = ls.participant_component_view(quiz)
        assert "facilitator_content" not in view
        html = client_for(e.participant_user).get(f"/my/{e.pk}/lessons/5/").content.decode()
        assert quiz.facilitator_content["notes"][0] not in html
        assert "Facilitator note" not in html

    def test_facilitator_guide_shows_notes(self, cohort, lead, version, client_for):
        html = client_for(lead).get(f"/facilitate/{cohort.pk}/lessons/5/").content.decode()
        assert "Facilitator note" in html


class TestFR17Drafts:
    def test_draft_save_and_reload(self, version, enrol, client_for):
        e = enrol()
        c = component(version, 1, "life_work")
        r = client_for(e.participant_user).put(
            f"/api/enrollments/{e.pk}/responses/{c.pk}", {"payload": {"what_is_success": "Faithful work"}, "expected_revision": 0},
            content_type="application/json",
        )
        assert r.status_code == 200 and r.json()["revision"] == 1 and r.json()["status"] == "draft"
        page = client_for(e.participant_user).get(f"/my/{e.pk}/lessons/1/").content.decode()
        assert "Faithful work" in page
        assert not LessonProgress.objects.filter(enrollment=e, completed_at__isnull=False).exists()

    def test_validation_error_preserves_typed_input(self, version, enrol, client_for):
        e = enrol()
        c = component(version, 1, "case_study")
        r = client_for(e.participant_user).post(
            f"/my/{e.pk}/components/{c.pk}/", {"action": "submit", "expected_revision": "0", "f__inspiration": "Typed but incomplete"}
        )
        assert r.status_code == 422
        html = r.content.decode()
        assert "Typed but incomplete" in html and "This answer is required" in html
        assert 'aria-invalid="true"' in html

    def test_stale_revision_conflicts(self, version, enrol):
        e = enrol()
        c = component(version, 1, "life_work")
        ls.save_draft(e, c, {"what_is_success": "v1"}, 0)
        ls.save_draft(e, c, {"what_is_success": "v2"}, 1)
        with pytest.raises(Conflict):
            ls.save_draft(e, c, {"what_is_success": "stale"}, 1)
        assert ComponentResponse.objects.get(enrollment=e, component=c).payload == {"what_is_success": "v2"}

    def test_TC12_retry_of_successful_save_is_safe(self, version, enrol):
        """The reply to a save was lost; the browser retries with the same revision and payload."""
        e = enrol()
        c = component(version, 1, "life_work")
        first = ls.save_draft(e, c, {"what_is_success": "same"}, 0)
        retry = ls.save_draft(e, c, {"what_is_success": "same"}, 0)
        assert retry.pk == first.pk and retry.revision == 1

    def test_invalid_shape_rejected_even_for_drafts(self, version, enrol):
        e = enrol()
        with pytest.raises(ValidationFailed):
            ls.save_draft(e, component(version, 3, "life_work"), {"hard_worker": 11}, 0)

    def test_submitted_answers_are_immutable(self, version, enrol):
        e = enrol()
        c = component(version, 1, "life_work")
        ls.submit_response(e, c, {"what_is_success": "final"}, 0)
        with pytest.raises(Conflict):
            ls.save_draft(e, c, {"what_is_success": "edit"}, 1)
        with pytest.raises(Conflict):
            ls.submit_response(e, c, {"what_is_success": "different"}, 1)
        # Identical retry is idempotent
        assert ls.submit_response(e, c, {"what_is_success": "final"}, 0).payload == {"what_is_success": "final"}

    def test_withdrawn_cannot_save(self, version, enrol):
        e = enrol()
        cs.withdraw_enrollment(e.participant_user, e)
        with pytest.raises(Forbidden):
            ls.save_draft(e, component(version, 1, "life_work"), {"what_is_success": "x"}, 0)


class TestFR18FR19Completion:
    def test_opening_lesson_is_insufficient(self, version, enrol, client_for):
        e = enrol()
        client_for(e.participant_user).get(f"/my/{e.pk}/lessons/1/")
        assert not LessonProgress.objects.filter(enrollment=e, completed_at__isnull=False).exists()

    def test_lesson_completes_only_when_required_parts_done(self, version, enrol):
        e = enrol()
        lesson = Lesson.objects.get(curriculum_version=version, lesson_number=1)
        ls.acknowledge(e, component(version, 1, "scripture"))
        ls.submit_response(e, component(version, 1, "case_study"), {"inspiration": "a", "positive_difference": "b"}, 0)
        assert LessonProgress.objects.get(enrollment=e, lesson=lesson).completed_at is None  # Life Work missing
        ls.submit_response(e, component(version, 1, "life_work"), {"what_is_success": "c"}, 0)
        p = LessonProgress.objects.get(enrollment=e, lesson=lesson)
        assert p.completed_at is not None and p.completion_method == "requirements"

    def test_discussion_confirmation_creates_no_text(self, cohort, lead, version, enrol, client_for):
        a, b = enrol(), enrol()
        d = component(version, 1, "discussion")
        client_for(lead).post(f"/facilitate/{cohort.pk}/lessons/1/", {"component": str(d.pk), "enrollments": [str(a.pk)]})
        ra = ComponentResponse.objects.get(enrollment=a, component=d)
        assert ra.status == "completed" and ra.payload is None and ra.confirmed_by == lead
        assert not ComponentResponse.objects.filter(enrollment=b, component=d).exists()

    def test_discussion_confirm_rejects_foreign_enrollment(self, cohort, lead, version, partner, geo, enrol):
        from datetime import date

        other = cs.create_cohort(lead, partner=partner, district=geo["kampala"], curriculum_version=version, name="O",
                                 start_date=date.today(), end_date=None, default_meeting_type="physical")
        _i, code = cs.create_invite(lead, other, expires_at=None, max_uses=None)
        foreign = enrol(code=code)
        with pytest.raises(ValidationFailed):
            ls.confirm_discussion(lead, cohort, component(version, 1, "discussion"), [str(foreign.pk)])


class TestFR20Override:
    def test_override_requires_reason_and_records_approver(self, cohort, lead, version, enrol):
        e = enrol()
        lesson = Lesson.objects.get(curriculum_version=version, lesson_number=2)
        with pytest.raises(ValidationFailed):
            ls.override_lesson(lead, e, lesson, "  ")
        p = ls.override_lesson(lead, e, lesson, "Hospitalised; completed orally")
        assert p.completion_method == "override" and p.confirmed_by == lead and p.override_reason and p.completed_at

    def test_assistant_cannot_override(self, cohort, lead, outsider, version, enrol):
        cs.assign_facilitator(lead, cohort, outsider, "assistant")
        with pytest.raises(Forbidden):
            ls.override_lesson(outsider, enrol(), Lesson.objects.get(curriculum_version=version, lesson_number=2), "x")

    def test_override_without_reason_blocked_by_database(self, version, enrol):
        e = enrol()
        lesson = Lesson.objects.get(curriculum_version=version, lesson_number=2)
        from django.db import IntegrityError
        from django.utils import timezone

        with pytest.raises(IntegrityError), transaction.atomic():
            LessonProgress.objects.create(enrollment=e, lesson=lesson, completed_at=timezone.now(), completion_method="override")


class TestFR21TC08CurriculumVersions:
    def test_published_content_cannot_be_edited(self, version):
        lesson = Lesson.objects.get(curriculum_version=version, lesson_number=1)
        with pytest.raises(DatabaseError), transaction.atomic():
            lesson.title = "Edited"
            lesson.save()
        c = lesson.components.first()
        with pytest.raises(DatabaseError), transaction.atomic():
            c.participant_content = {"blocks": []}
            c.save()
        with pytest.raises(DatabaseError), transaction.atomic():
            CurriculumVersion.objects.filter(pk=version.pk).update(title="Edited")

    def test_new_version_leaves_existing_cohort_intact(self, cohort, lead, partner, geo, version, version2, enrol):
        from datetime import date

        e1 = enrol()
        ls.submit_response(e1, component(version, 1, "life_work"), {"what_is_success": "V1 answer"}, 0)
        c2 = cs.create_cohort(lead, partner=partner, district=geo["kampala"], curriculum_version=version2, name="New intake",
                              start_date=date.today(), end_date=None, default_meeting_type="physical")
        assert c2.curriculum_version == version2 and cohort.curriculum_version == version
        r = ComponentResponse.objects.get(enrollment=e1)
        assert r.component.lesson.curriculum_version == version and r.payload == {"what_is_success": "V1 answer"}
        # A V1 enrollment cannot submit a V2 component.
        with pytest.raises(NotFound):
            ls.save_draft(e1, component(version2, 1, "life_work"), {"what_is_success": "x"}, 0)

    def test_cohort_version_cannot_change_after_enrollment(self, cohort, version2, enrol):
        enrol()
        with pytest.raises(DatabaseError), transaction.atomic():
            type(cohort).objects.filter(pk=cohort.pk).update(curriculum_version=version2)


class TestFR34TC03Privacy:
    def _private_and_shared(self, version, e):
        private = component(version, 1, "life_work")
        shared = component(version, 9, "project_brainstorm")
        assert private.visibility == "private" and shared.visibility == "facilitator"
        ls.submit_response(e, private, {"what_is_success": "MY-PRIVATE-REFLECTION"}, 0)
        ls.submit_response(e, shared, {"problem": "SHARED-PROBLEM", "idea": "SHARED-IDEA"}, 0)
        return private, shared

    def test_owner_reads_private_text(self, version, enrol, client_for):
        e = enrol()
        private, _ = self._private_and_shared(version, e)
        r = client_for(e.participant_user).get(f"/api/enrollments/{e.pk}/responses/{private.pk}")
        assert r.json()["response"]["payload"] == {"what_is_success": "MY-PRIVATE-REFLECTION"}

    def test_assigned_facilitator_sees_completion_only(self, cohort, lead, version, enrol, client_for):
        e = enrol()
        self._private_and_shared(version, e)
        html = client_for(lead).get(f"/facilitate/{cohort.pk}/participants/{e.pk}/").content.decode()
        assert "MY-PRIVATE-REFLECTION" not in html
        assert "SHARED-PROBLEM" in html  # explicitly facilitator-visible component

    def test_unrelated_facilitator_and_admin_get_nothing(self, cohort, lead, admin, outsider, version, enrol, client_for, caplog):
        e = enrol()
        private, _ = self._private_and_shared(version, e)
        caplog.set_level(logging.INFO)
        for user in (outsider, admin, lead):
            r = client_for(user).get(f"/api/enrollments/{e.pk}/responses/{private.pk}")
            assert r.status_code == 404
            assert b"MY-PRIVATE-REFLECTION" not in r.content
        assert client_for(outsider).get(f"/facilitate/{cohort.pk}/participants/{e.pk}/").status_code == 404
        admin_page = client_for(admin).get(f"/admin-console/cohorts/{cohort.pk}/").content.decode()
        export = client_for(admin).get("/reports/export/").content.decode()
        assert "MY-PRIVATE-REFLECTION" not in admin_page and "MY-PRIVATE-REFLECTION" not in export
        assert "SHARED-PROBLEM" not in export
        assert "MY-PRIVATE-REFLECTION" not in caplog.text

    def test_logs_contain_ids_not_payloads(self, version, enrol, client_for, caplog):
        e = enrol()
        c = component(version, 1, "life_work")
        caplog.set_level(logging.INFO, logger="ctt.request")
        client_for(e.participant_user).put(
            f"/api/enrollments/{e.pk}/responses/{c.pk}", json.dumps({"payload": {"what_is_success": "SECRET-TEXT"}, "expected_revision": 0}),
            content_type="application/json",
        )
        assert "SECRET-TEXT" not in caplog.text
        assert str(e.pk) in caplog.text and "status=200" in caplog.text
        assert e.participant_user.email not in caplog.text

    def test_project_team_visibility(self, cohort, lead, version, enrol):
        from apps.projects import services as ps

        a, b, outsider_e = enrol(), enrol(), enrol()
        p = ps.create_project(lead, cohort, name="Clean-up", problem_statement="Litter", idea_summary="Pick up")
        ps.add_member(lead, p, a, "leader")
        ps.add_member(lead, p, b, "secretary")
        r = ls.submit_response(a, component(version, 16, "presentation"), {"project_name": "TEAM-NAME", "project_summary": "s", "blessing": "b"}, 0)
        assert ls.can_read_payload(b.participant_user, r)
        assert not ls.can_read_payload(outsider_e.participant_user, r)
        private = ls.submit_response(a, component(version, 1, "life_work"), {"what_is_success": "x"}, 0)
        assert not ls.can_read_payload(b.participant_user, private)


def test_facilitator_cannot_submit_for_participant(cohort, lead, version, enrol, client_for):
    e = enrol()
    c = component(version, 1, "life_work")
    r = client_for(lead).put(f"/api/enrollments/{e.pk}/responses/{c.pk}", {"payload": {"what_is_success": "x"}, "expected_revision": 0}, content_type="application/json")
    assert r.status_code == 404


def test_lesson_pages_render_for_every_lesson(version, enrol, client_for):
    e = enrol()
    c = client_for(e.participant_user)
    for n in range(1, 17):
        assert c.get(f"/my/{e.pk}/lessons/{n}/").status_code == 200


def test_pending_facilitator_sees_status(admin, partner, cohort, client_for):
    pending = make_facilitator(admin, partner, status="pending")
    page = client_for(pending).get("/facilitate/").content.decode()
    assert "<b>pending</b>" in page and cohort.name not in page
