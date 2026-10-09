"""TC-FR25..FR30, TC07 (revision conflict) and the full programme-completion journey."""
from datetime import date, timedelta

import pytest

from apps.cohorts import services as cs
from apps.core.errors import Conflict, Forbidden, NotFound, ValidationFailed
from apps.curriculum.models import Lesson
from apps.learning import services as ls
from apps.learning.models import ComponentResponse, ProgrammeCompletionOverride
from apps.projects import services as ps
from apps.projects.models import ProjectTask

from .conftest import complete_lesson, component

pytestmark = pytest.mark.django_db


@pytest.fixture
def project(cohort, lead):
    return ps.create_project(lead, cohort, name="Reading Club", problem_statement="Children cannot read well",
                             idea_summary="Weekly tutoring", community_benefit="Better literacy")


@pytest.fixture
def team(project, lead, enrol):
    a, b = enrol("Leader Lucy"), enrol("Secretary Sam")
    return {"a": ps.add_member(lead, project, a, "leader"), "b": ps.add_member(lead, project, b, "secretary")}


def planned(project, lead, team):
    g = ps.add_goal(lead, project, "Tutor 20 children", project.revision)
    project.refresh_from_db()
    act = ps.add_action(lead, project, problem_to_solve="No venue", action_required="Find a classroom", goal=g, expected_revision=project.revision)
    task = ps.add_task(lead, project, act, {"description": "Ask the head teacher", "responsible_member": team["a"],
                                            "resources_needed": "Airtime", "others_involved": "Head teacher",
                                            "start_date": date.today(), "due_date": date.today() + timedelta(days=3)})
    project.refresh_from_db()
    return g, act, task


class TestFR25FR26Teams:
    def test_project_captures_workbook_fields(self, project):
        assert project.problem_statement and project.idea_summary and project.community_benefit

    def test_several_projects_per_cohort(self, cohort, lead, project):
        ps.create_project(lead, cohort, name="Garden", problem_statement="Hunger", idea_summary="Garden")
        assert cohort.projects.count() == 2

    def test_roles_supported(self, project, lead, enrol):
        for role in ("leader", "secretary", "spokesperson", "resourcer", "member"):
            assert ps.add_member(lead, project, enrol(), role).team_role == role

    def test_member_from_other_cohort_rejected(self, project, lead, partner, geo, version, enrol):
        other = cs.create_cohort(lead, partner=partner, district=geo["kampala"], curriculum_version=version, name="Other",
                                 start_date=date.today(), end_date=None, default_meeting_type="physical")
        _i, code = cs.create_invite(lead, other, expires_at=None, max_uses=None)
        with pytest.raises(ValidationFailed):
            ps.add_member(lead, project, enrol(code=code), "member")

    def test_participants_cannot_add_members(self, project, team, enrol):
        with pytest.raises(Forbidden):
            ps.add_member(team["a"].enrollment.participant_user, project, enrol(), "member")

    def test_other_team_project_is_concealed(self, cohort, lead, project, team, enrol, client_for):
        other = ps.create_project(lead, cohort, name="Garden", problem_statement="Hunger", idea_summary="Garden")
        stranger = enrol()
        ps.add_member(lead, other, stranger, "leader")
        assert client_for(stranger.participant_user).get(f"/projects/{project.pk}/").status_code == 404
        assert client_for(team["a"].enrollment.participant_user).get(f"/projects/{project.pk}/").status_code == 200

    def test_role_assignment_completes_lesson_11_milestone(self, version, team):
        c = component(version, 11, "group_member_roles")
        assert ComponentResponse.objects.get(enrollment=team["a"].enrollment, component=c).status == "completed"


class TestFR27FR28Planning:
    def test_planning_sheet_and_lesson_12_milestone(self, project, lead, team, version):
        _g, _a, task = planned(project, lead, team)
        assert task.resources_needed == "Airtime" and task.others_involved == "Head teacher"
        c = component(version, 12, "project_planning")
        assert ComponentResponse.objects.get(enrollment=team["b"].enrollment, component=c).status == "completed"

    def test_assignee_from_other_project_rejected(self, cohort, project, lead, team, enrol):
        _g, act, _t = planned(project, lead, team)
        other = ps.create_project(lead, cohort, name="Garden", problem_statement="Hunger", idea_summary="Garden")
        foreign_member = ps.add_member(lead, other, enrol(), "leader")
        with pytest.raises(ValidationFailed):
            ps.add_task(lead, project, act, {"description": "x", "responsible_member": foreign_member})

    def test_goal_from_other_project_rejected(self, cohort, project, lead, team):
        other = ps.create_project(lead, cohort, name="Garden", problem_statement="Hunger", idea_summary="Garden")
        foreign_goal = ps.add_goal(lead, other, "Other goal", other.revision)
        with pytest.raises(ValidationFailed):
            ps.add_action(lead, project, problem_to_solve="p", action_required="a", goal=foreign_goal, expected_revision=project.revision)

    def test_contradictory_dates_rejected(self, project, lead, team):
        _g, act, task = planned(project, lead, team)
        with pytest.raises(ValidationFailed):
            ps.add_task(lead, project, act, {"description": "x", "start_date": date.today(), "due_date": date.today() - timedelta(days=1)})
        with pytest.raises(ValidationFailed):
            ps.update_project(lead, project, {"target_start_date": date.today(), "target_end_date": date.today() - timedelta(days=1)}, project.revision)

    def test_TC07_stale_task_update_gets_409(self, project, lead, team, client_for):
        _g, _a, task = planned(project, lead, team)
        alice, bob = team["a"].enrollment.participant_user, team["b"].enrollment.participant_user
        url = f"/api/projects/{project.pk}/tasks/{task.pk}"
        r1 = client_for(alice).patch(url, {"status": "doing", "expected_revision": 1}, content_type="application/json")
        assert r1.status_code == 200 and r1.json()["revision"] == 2
        r2 = client_for(bob).patch(url, {"status": "done", "expected_revision": 1}, content_type="application/json")
        assert r2.status_code == 409
        task.refresh_from_db()
        assert task.status == "doing" and task.revision == 2
        r3 = client_for(bob).patch(url, {"status": "done", "expected_revision": 2}, content_type="application/json")
        assert r3.status_code == 200 and ProjectTask.objects.get(pk=task.pk).completed_at is not None

    def test_stale_project_edit_conflicts(self, project, lead, team):
        rev = project.revision
        ps.update_project(lead, project, {"name": "Reading Club 2"}, rev)
        with pytest.raises(Conflict):
            ps.update_project(team["a"].enrollment.participant_user, project, {"name": "Overwrite"}, rev)

    def test_task_patch_rejects_unknown_fields(self, project, lead, team, client_for):
        _g, _a, task = planned(project, lead, team)
        r = client_for(lead).patch(f"/api/projects/{project.pk}/tasks/{task.pk}", {"action_id": "x", "expected_revision": 1}, content_type="application/json")
        assert r.status_code == 422


class TestFR29Evaluation:
    def payload(self, value=3, text="Start earlier"):
        keys = ["self_role", "self_communication", "self_ideas", "self_teammate", "team_roles", "team_time",
                "team_communication", "team_help", "project_relevant", "project_complete"]
        return {**{k: value for k in keys}, "do_differently": text}

    def test_ratings_use_0_to_3(self, project, team):
        user = team["a"].enrollment.participant_user
        with pytest.raises(ValidationFailed):
            ps.save_evaluation(user, project, self.payload(4), 0, submit=True)
        ev = ps.save_evaluation(user, project, self.payload(0), 0, submit=True)
        assert ev.status == "submitted" and ev.payload["self_role"] == 0

    def test_incomplete_evaluation_cannot_submit(self, project, team):
        p = self.payload()
        p.pop("team_help")
        with pytest.raises(ValidationFailed) as exc:
            ps.save_evaluation(team["a"].enrollment.participant_user, project, p, 0, submit=True)
        assert "team_help" in exc.value.fields

    def test_non_member_cannot_evaluate(self, project, lead, enrol):
        with pytest.raises(Forbidden):
            ps.save_evaluation(enrol().participant_user, project, self.payload(), 0, submit=True)

    def test_evaluation_separate_from_final_reflection(self, project, team, version):
        e = team["a"].enrollment
        ps.save_evaluation(e.participant_user, project, self.payload(), 0, submit=True)
        reflection = component(version, 15, "life_work")
        assert not ComponentResponse.objects.filter(enrollment=e, component=reflection).exists()
        assert ComponentResponse.objects.get(enrollment=e, component=component(version, 15, "project_evaluation")).status == "completed"


class TestFR30Completion:
    def test_unmet_conditions_listed(self, project, lead):
        cond = ps.completion_conditions(project)
        assert not cond["ready"] and any("no team members" in u for u in cond["unmet"])
        with pytest.raises(ValidationFailed):
            ps.confirm_completion(lead, project, project.revision)

    def test_only_lead_confirms(self, cohort, project, lead, outsider, team):
        cs.assign_facilitator(lead, cohort, outsider, "assistant")
        with pytest.raises(Forbidden):
            ps.confirm_completion(outsider, project, project.revision)

    def test_attendance_alone_is_insufficient(self, cohort, lead, enrol):
        e = enrol()
        from datetime import datetime

        s = cs.schedule_session(lead, cohort, title="S", starts_at=datetime(2026, 10, 1, 9), ends_at=None,
                                meeting_type="physical", meet_url=None, venue="Hall", lesson=None)
        s = cs.record_attendance(lead, s, {str(e.pk): "present"}, s.revision)
        cs.finalize_session(lead, s, s.revision)
        check = ls.programme_checklist(e)
        assert not check["eligible"]
        with pytest.raises(ValidationFailed):
            ls.confirm_programme_completion(lead, e)

    def test_programme_override_records_reason(self, cohort, lead, enrol):
        e = enrol()
        e = ls.confirm_programme_completion(lead, e, override_reason="Relocated; completed work on paper")
        o = ProgrammeCompletionOverride.objects.get(enrollment=e)
        assert e.status == "completed" and o.authorized_by == lead and o.reason

    def test_full_journey_to_programme_completion(self, cohort, lead, project, version, enrol, client_for):
        a = enrol("Allan Learner")
        member = ps.add_member(lead, project, a, "secretary")
        planned(project, lead, {"a": member})
        ps.update_project(lead, project, {"stage": "implementation"}, project.revision)
        ps.save_evaluation(a.participant_user, project, TestFR29Evaluation().payload(), 0, submit=True)
        for lesson in Lesson.objects.filter(curriculum_version=version).order_by("lesson_number"):
            complete_lesson(a, lesson)
        check = ls.programme_checklist(a)
        assert check["lessons_completed"] == 16
        assert not check["eligible"] and "confirmed as complete" in " ".join(check["unmet"])  # project not yet confirmed
        project.refresh_from_db()
        r = client_for(lead).post(f"/api/projects/{project.pk}/confirm-completion", {"confirm": True, "expected_revision": project.revision}, content_type="application/json")
        assert r.status_code == 200 and r.json()["stage"] == "completed"
        assert ls.programme_checklist(a)["eligible"]
        done = ls.confirm_programme_completion(lead, a)
        assert done.status == "completed" and done.completion_confirmed_by == lead
        assert not ProgrammeCompletionOverride.objects.filter(enrollment=a).exists()

    def test_withdrawn_member_keeps_contributions(self, project, lead, team):
        planned(project, lead, team)
        e = team["a"].enrollment
        cs.withdraw_enrollment(e.participant_user, e)
        assert ProjectTask.objects.filter(responsible_member=team["a"]).exists()
        with pytest.raises(NotFound):
            ps.project_for_viewer(e.participant_user, project.pk)
