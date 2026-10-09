"""TC-FR22..FR24, TC04 (workbook scoring) and TC05 (replay and rollback)."""
from unittest import mock

import pytest

from apps.assessment import services as quiz
from apps.assessment.models import Assessment, AssessmentAttempt, AssessmentScore
from apps.core.errors import ValidationFailed
from apps.learning.models import ComponentResponse, LessonProgress

pytestmark = pytest.mark.django_db

MAPPING = {
    "investigative": [11, 17, 20, 23], "social": [7, 12, 13, 24], "artistic": [4, 8, 18, 19],
    "realistic": [3, 5, 6, 16], "conventional": [1, 2, 10, 22], "enterprising": [9, 14, 15, 21],
}


@pytest.fixture
def assessment(version):
    return Assessment.objects.get(component__lesson__curriculum_version=version)


def answers(value_for):
    return {str(n): value_for(n) for n in range(1, 25)}


def submit(enrollment, assessment, values):
    attempt = quiz.open_attempt(enrollment, assessment, retake=True)
    return quiz.submit_attempt(attempt, values, attempt.revision)


def scores(attempt):
    return {s.category.code: s.score for s in attempt.scores.select_related("category")}


class TestTC04Scoring:
    def test_definition_matches_workbook(self, assessment):
        assert assessment.questions.count() == 24 and (assessment.min_answer, assessment.max_answer) == (1, 5)
        for code, numbers in MAPPING.items():
            assert sorted(assessment.questions.filter(category__code=code).values_list("question_number", flat=True)) == numbers

    def test_all_ones_and_all_fives(self, enrol, assessment):
        e = enrol()
        assert set(scores(submit(e, assessment, answers(lambda n: 1))).values()) == {4}
        assert set(scores(submit(e, assessment, answers(lambda n: 5))).values()) == {20}

    def test_asymmetric_fixture_exact_mapping(self, enrol, assessment):
        e = enrol()
        weights = {code: i + 1 for i, code in enumerate(MAPPING)}  # investigative=1 ... enterprising=6 (capped at 5)
        q_to_code = {q: code for code, qs in MAPPING.items() for q in qs}
        values = answers(lambda n: min(weights[q_to_code[n]], 5))
        values["11"] = 5  # one investigative answer raised
        got = scores(submit(e, assessment, values))
        assert got == {"investigative": 8, "social": 8, "artistic": 12, "realistic": 16, "conventional": 20, "enterprising": 20}

    def test_ties_preserved(self, enrol, assessment):
        e = enrol()
        attempt = submit(e, assessment, answers(lambda n: 5 if n in MAPPING["social"] + MAPPING["artistic"] else 2))
        top = [r["code"] for r in quiz.results(attempt)["top"]]
        assert sorted(top) == ["artistic", "social"]

    @pytest.mark.parametrize("bad", [{"5": None}, {"5": 0}, {"5": 6}, {"5": "x"}, {"99": 3}])
    def test_invalid_or_missing_answers_rejected(self, enrol, assessment, bad):
        e = enrol()
        attempt = quiz.open_attempt(e, assessment)
        values = answers(lambda n: 3)
        for k, v in bad.items():
            if v is None:
                values.pop(k)
            else:
                values[k] = v
        with pytest.raises(ValidationFailed):
            quiz.submit_attempt(attempt, values, attempt.revision)
        attempt.refresh_from_db()
        assert attempt.status == "draft" and not attempt.scores.exists()


class TestTC05ReplayRollback:
    def test_submit_twice_scores_once(self, enrol, assessment):
        e = enrol()
        attempt = quiz.open_attempt(e, assessment)
        a1 = quiz.submit_attempt(attempt, answers(lambda n: 4), attempt.revision)
        a2 = quiz.submit_attempt(attempt, answers(lambda n: 1), attempt.revision)
        assert a1.pk == a2.pk and AssessmentScore.objects.filter(attempt=attempt).count() == 6
        assert set(scores(a2).values()) == {16}

    def test_api_replay(self, enrol, assessment, client_for):
        e = enrol()
        attempt = quiz.open_attempt(e, assessment)
        c = client_for(e.participant_user)
        body = {"answers": answers(lambda n: 2), "expected_revision": attempt.revision}
        r1 = c.post(f"/api/assessment-attempts/{attempt.pk}/submit", body, content_type="application/json")
        r2 = c.post(f"/api/assessment-attempts/{attempt.pk}/submit", body, content_type="application/json")
        assert r1.status_code == 200 and r1.json() == r2.json()
        assert AssessmentScore.objects.filter(attempt=attempt).count() == 6

    def test_failure_during_progress_write_rolls_back_everything(self, enrol, assessment):
        e = enrol()
        attempt = quiz.open_attempt(e, assessment)
        with mock.patch("apps.assessment.services.complete_component", side_effect=RuntimeError("disk full")):
            with pytest.raises(RuntimeError):
                quiz.submit_attempt(attempt, answers(lambda n: 3), attempt.revision)
        attempt.refresh_from_db()
        assert attempt.status == "draft" and not attempt.scores.exists() and not attempt.answers.exists()
        assert not ComponentResponse.objects.filter(enrollment=e, component=assessment.component).exists()
        # Retry succeeds with one consistent result
        done = quiz.submit_attempt(attempt, answers(lambda n: 3), attempt.revision)
        assert done.status == "submitted" and done.scores.count() == 6
        assert ComponentResponse.objects.get(enrollment=e, component=assessment.component).status == "completed"


class TestFR24Attempts:
    def test_retake_keeps_earlier_attempt(self, enrol, assessment):
        e = enrol()
        first = submit(e, assessment, answers(lambda n: 1))
        second = submit(e, assessment, answers(lambda n: 5))
        assert (first.attempt_number, second.attempt_number) == (1, 2)
        first.refresh_from_db()
        assert set(scores(first).values()) == {4} and first.answers.filter(value=1).count() == 24
        assert quiz.latest_submitted(e, assessment).pk == second.pk
        assert second.scoring_version == assessment.scoring_version

    def test_submission_completes_component_and_updates_progress(self, enrol, assessment, version):
        e = enrol()
        submit(e, assessment, answers(lambda n: 3))
        assert ComponentResponse.objects.get(enrollment=e, component=assessment.component).status == "completed"
        assert LessonProgress.objects.filter(enrollment=e, lesson=assessment.component.lesson).exists()

    def test_quiz_pages(self, enrol, assessment, client_for):
        e = enrol()
        c = client_for(e.participant_user)
        c.post(f"/my/{e.pk}/quiz/", {"action": "start"})
        attempt = AssessmentAttempt.objects.get(enrollment=e)
        data = {"action": "submit", "expected_revision": attempt.revision, **{f"q_{n}": "3" for n in range(1, 24)}}
        r = c.post(f"/my/{e.pk}/quiz/", data)
        assert r.status_code == 422 and b"Please answer this question" in r.content
        data["q_24"] = "4"
        r = c.post(f"/my/{e.pk}/quiz/", data)
        assert r.status_code == 302
        page = c.get(r["Location"]).content.decode()
        assert "Social" in page and "/ 20" in page
