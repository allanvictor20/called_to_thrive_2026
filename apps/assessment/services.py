"""Career assessment (FR22–FR24): 24 questions rated 1–5, six category sums, ties preserved."""
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from apps.cohorts.models import Enrollment
from apps.core.errors import Conflict, NotFound, ValidationFailed
from apps.learning.services import _check_can_work, complete_component

from .models import Assessment, AssessmentAnswer, AssessmentAttempt, AssessmentScore


def _check_assessment(enrollment: Enrollment, assessment: Assessment):
    if assessment.component.lesson.curriculum_version_id != enrollment.cohort.curriculum_version_id:
        raise NotFound("Assessment not found.")


@transaction.atomic
def open_attempt(enrollment: Enrollment, assessment: Assessment, *, retake: bool = False) -> AssessmentAttempt:
    """Return the open draft, or start one. A retake creates a new attempt; earlier ones stay untouched."""
    _check_assessment(enrollment, assessment)
    _check_can_work(enrollment)
    draft = AssessmentAttempt.objects.select_for_update().filter(enrollment=enrollment, assessment=assessment, status="draft").first()
    if draft:
        return draft
    has_submitted = AssessmentAttempt.objects.filter(enrollment=enrollment, assessment=assessment, status="submitted").exists()
    if has_submitted and not retake:
        return latest_submitted(enrollment, assessment)
    last = AssessmentAttempt.objects.filter(enrollment=enrollment, assessment=assessment).aggregate(n=Max("attempt_number"))["n"] or 0
    return AssessmentAttempt.objects.create(
        enrollment=enrollment, assessment=assessment, attempt_number=last + 1, scoring_version=assessment.scoring_version
    )


def _clean_answers(assessment: Assessment, answers: dict, *, complete: bool) -> dict[int, int]:
    questions = {q.question_number: q for q in assessment.questions.all()}
    cleaned, errors = {}, {}
    for raw_key, raw_value in (answers or {}).items():
        try:
            number = int(raw_key)
        except (TypeError, ValueError):
            errors[str(raw_key)] = "Unknown question."
            continue
        if number not in questions:
            errors[str(raw_key)] = "Unknown question."
            continue
        if raw_value in (None, ""):
            continue
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            errors[str(number)] = f"Choose a value from {assessment.min_answer} to {assessment.max_answer}."
            continue
        if not assessment.min_answer <= value <= assessment.max_answer:
            errors[str(number)] = f"Choose a value from {assessment.min_answer} to {assessment.max_answer}."
            continue
        cleaned[number] = value
    if complete:
        for number in questions:
            if number not in cleaned and str(number) not in errors:
                errors[str(number)] = "Please answer this question."
    if errors:
        raise ValidationFailed(
            "Answer every question with a value from 1 to 5." if complete else "Some answers are not valid.", fields=errors
        )
    return cleaned


def _write_answers(attempt: AssessmentAttempt, cleaned: dict[int, int]):
    questions = {q.question_number: q for q in attempt.assessment.questions.all()}
    for number, value in cleaned.items():
        AssessmentAnswer.objects.update_or_create(attempt=attempt, question=questions[number], defaults={"value": value})


@transaction.atomic
def save_answers(attempt: AssessmentAttempt, answers: dict, expected_revision: int) -> AssessmentAttempt:
    attempt = AssessmentAttempt.objects.select_for_update().select_related("assessment", "enrollment__cohort").get(pk=attempt.pk)
    _check_can_work(attempt.enrollment)
    if attempt.status != "draft":
        raise Conflict("This attempt has already been submitted.", code="already_submitted")
    if attempt.revision != int(expected_revision):
        raise Conflict("Your answers were changed from another window. Reload to continue.")
    _write_answers(attempt, _clean_answers(attempt.assessment, answers, complete=False))
    attempt.revision += 1
    attempt.save(update_fields=["revision", "updated_at"])
    return attempt


def compute_scores(assessment: Assessment, values_by_question: dict[int, int]) -> dict[str, int]:
    """Pure scoring rule: sum the four answers in each category (4–20)."""
    totals = {c.code: 0 for c in assessment.categories.all()}
    for q in assessment.questions.select_related("category"):
        totals[q.category.code] += values_by_question[q.question_number]
    return totals


@transaction.atomic
def submit_attempt(attempt: AssessmentAttempt, answers: dict | None, expected_revision: int) -> AssessmentAttempt:
    """Lock the draft, validate all 24 values, compute six sums, store immutable scores,
    mark submitted and complete the lesson component — all in one transaction.

    Resubmitting an already-submitted attempt returns the prior result.
    """
    attempt = AssessmentAttempt.objects.select_for_update().select_related("assessment__component__lesson", "enrollment__cohort").get(pk=attempt.pk)
    if attempt.status == "submitted":
        return attempt
    _check_can_work(attempt.enrollment)
    if attempt.revision != int(expected_revision):
        raise Conflict("Your answers were changed from another window. Reload to continue.")
    assessment = attempt.assessment
    if answers is not None:
        _write_answers(attempt, _clean_answers(assessment, answers, complete=False))
    stored = {a.question.question_number: a.value for a in attempt.answers.select_related("question")}
    cleaned = _clean_answers(assessment, stored, complete=True)
    totals = compute_scores(assessment, cleaned)
    categories = {c.code: c for c in assessment.categories.all()}
    AssessmentScore.objects.bulk_create(
        [AssessmentScore(attempt=attempt, category=categories[code], score=score) for code, score in totals.items()]
    )
    attempt.status = "submitted"
    attempt.submitted_at = timezone.now()
    attempt.revision += 1
    attempt.save(update_fields=["status", "submitted_at", "revision", "updated_at"])
    complete_component(attempt.enrollment, assessment.component)
    return attempt


def latest_submitted(enrollment: Enrollment, assessment: Assessment) -> AssessmentAttempt | None:
    return (
        AssessmentAttempt.objects.filter(enrollment=enrollment, assessment=assessment, status="submitted")
        .order_by("-attempt_number")
        .first()
    )


def results(attempt: AssessmentAttempt) -> dict:
    """All six scores, highest first; every category tied for the top is marked (no tie-breaker)."""
    scores = list(attempt.scores.select_related("category").order_by("category__display_order"))
    top = max((s.score for s in scores), default=None)
    rows = [
        {
            "code": s.category.code,
            "label": s.category.label,
            "score": s.score,
            "description": s.category.description,
            "sample_careers": s.category.sample_careers,
            "is_top": s.score == top,
        }
        for s in scores
    ]
    rows.sort(key=lambda r: (-r["score"], [c.code for c in attempt.assessment.categories.all()].index(r["code"])))
    return {"attempt": attempt, "rows": rows, "top": [r for r in rows if r["is_top"]], "max_possible": 4 * attempt.assessment.max_answer}
