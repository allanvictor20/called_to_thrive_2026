"""Reviewed content import (SDD: published curriculum updates use an import, not a course builder)."""
import json
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from apps.assessment.models import Assessment, AssessmentCategory, AssessmentQuestion

from .models import METHODS, TYPES, VISIBILITIES, CurriculumVersion, Lesson, LessonComponent
from .schema import validate_schema_definition

MILESTONES = {"team_role_assigned", "plan_recorded", "evaluation_submitted"}


class ManifestError(ValueError):
    pass


def validate_manifest(m: dict) -> None:
    if m.get("format") != "ctt-curriculum-manifest/1":
        raise ManifestError("Unknown manifest format.")
    for key in ("code", "title", "source_reference", "lessons", "completion_policy"):
        if not m.get(key):
            raise ManifestError(f"Manifest is missing {key}.")
    numbers = [lesson["number"] for lesson in m["lessons"]]
    if sorted(numbers) != list(range(1, len(numbers) + 1)):
        raise ManifestError("Lesson numbers must be consecutive from 1.")
    assessment_refs = {(a["lesson"], a["component"]) for a in m.get("assessments", [])}
    for lesson in m["lessons"]:
        keys = set()
        for c in lesson["components"]:
            if c["key"] in keys:
                raise ManifestError(f"Lesson {lesson['number']}: duplicate component key {c['key']}.")
            keys.add(c["key"])
            if c["type"] not in TYPES:
                raise ManifestError(f"Lesson {lesson['number']}/{c['key']}: unknown type {c['type']}.")
            method = c.get("completion_method")
            if method and method not in METHODS:
                raise ManifestError(f"Lesson {lesson['number']}/{c['key']}: unknown completion method {method}.")
            if c.get("required") and not method:
                raise ManifestError(f"Lesson {lesson['number']}/{c['key']}: required components need a completion method.")
            if c.get("visibility", "private") not in VISIBILITIES:
                raise ManifestError(f"Lesson {lesson['number']}/{c['key']}: unknown visibility.")
            if method == "submission":
                if not c.get("response_schema"):
                    raise ManifestError(f"Lesson {lesson['number']}/{c['key']}: submissions need a response schema.")
                try:
                    validate_schema_definition(c["response_schema"])
                except ValueError as exc:
                    raise ManifestError(f"Lesson {lesson['number']}/{c['key']}: {exc}") from exc
            if method == "project_milestone" and c["content"].get("milestone") not in MILESTONES:
                raise ManifestError(f"Lesson {lesson['number']}/{c['key']}: unknown project milestone.")
            if method == "assessment" and (lesson["number"], c["key"]) not in assessment_refs:
                raise ManifestError(f"Lesson {lesson['number']}/{c['key']}: assessment definition missing.")
    for a in m.get("assessments", []):
        qnums = sorted(q for cat in a["categories"] for q in cat["questions"])
        if qnums != sorted(q["number"] for q in a["questions"]):
            raise ManifestError(f"Assessment {a['code']}: every question must map to exactly one category.")
    evaluation = m.get("project_evaluation")
    if not evaluation or evaluation["scale"]["min"] != 0 or evaluation["scale"]["max"] != 3:
        raise ManifestError("Project evaluation must use the workbook 0 to 3 scale.")


@transaction.atomic
def import_manifest(path: str | Path, *, publish: bool = False, code_override: str | None = None) -> CurriculumVersion:
    m = json.loads(Path(path).read_text(encoding="utf-8"))
    validate_manifest(m)
    code = code_override or m["code"]
    if CurriculumVersion.objects.filter(code=code).exists():
        raise ManifestError(f"Curriculum version {code} already exists. Published versions are immutable; use a new code.")
    policy = dict(m["completion_policy"])
    policy["project_evaluation"] = m["project_evaluation"]
    policy["review_notes"] = m.get("review_notes", [])
    version = CurriculumVersion.objects.create(
        code=code,
        title=m["title"],
        source_reference=m["source_reference"],
        language_code=m.get("language_code", "en"),
        completion_policy=policy,
    )
    components_by_ref = {}
    for lesson_def in m["lessons"]:
        lesson = Lesson.objects.create(
            curriculum_version=version, lesson_number=lesson_def["number"], title=lesson_def["title"], summary=lesson_def.get("summary")
        )
        for order, c in enumerate(lesson_def["components"], start=1):
            comp = LessonComponent.objects.create(
                lesson=lesson,
                component_key=c["key"],
                display_order=order,
                component_type=c["type"],
                title=c["title"],
                participant_content=c.get("content", {}),
                facilitator_content=c.get("facilitator_content"),
                response_schema=c.get("response_schema"),
                required=bool(c.get("required", False)),
                completion_method=c.get("completion_method"),
                visibility=c.get("visibility", "private"),
            )
            components_by_ref[(lesson_def["number"], c["key"])] = comp
    for a in m.get("assessments", []):
        assessment = Assessment.objects.create(
            component=components_by_ref[(a["lesson"], a["component"])],
            code=a["code"],
            scoring_version=a["scoring_version"],
            min_answer=a["min_answer"],
            max_answer=a["max_answer"],
        )
        cat_for_q = {}
        for order, cat in enumerate(a["categories"], start=1):
            category = AssessmentCategory.objects.create(
                assessment=assessment,
                code=cat["code"],
                label=cat["label"],
                description=cat.get("description"),
                sample_careers=cat.get("sample_careers", []),
                display_order=order,
            )
            for q in cat["questions"]:
                cat_for_q[q] = category
        for q in a["questions"]:
            AssessmentQuestion.objects.create(
                assessment=assessment, category=cat_for_q[q["number"]], question_number=q["number"], prompt=q["prompt"]
            )
    if publish:
        publish_version(version)
    return version


def publish_version(version: CurriculumVersion) -> CurriculumVersion:
    """Validate and freeze a draft version. Database triggers block later edits."""
    if version.status != CurriculumVersion.DRAFT:
        raise ManifestError("Only draft versions can be published.")
    if not version.lessons.exists():
        raise ManifestError("A version needs lessons before publication.")
    for comp in LessonComponent.objects.filter(lesson__curriculum_version=version, completion_method="assessment"):
        if not Assessment.objects.filter(component=comp).exists():
            raise ManifestError(f"{comp}: assessment definition missing.")
    version.status = CurriculumVersion.PUBLISHED
    version.published_at = timezone.now()
    version.save(update_fields=["status", "published_at", "updated_at"])
    return version
