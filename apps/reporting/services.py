"""Programme reporting (FR31–FR33) using the definitions in the database design §11.

Every metric is computed from source records. Progress, attendance and projects
are pre-aggregated separately per enrollment before being combined, so joined
tasks or sessions can never multiply counts. No response payloads are read.
"""
import csv
import io
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date

from apps.accounts.models import User
from apps.cohorts.models import Attendance, Cohort, Enrollment, Session
from apps.curriculum.models import Lesson
from apps.learning.models import LessonProgress, ProgrammeCompletionOverride
from apps.projects.models import Project, ProjectMember

DEFINITIONS = {
    "unique_participants": "Distinct participants across the selected enrollments. Reported separately from enrollments.",
    "enrollments": "Enrollments in the selected cohorts, all statuses (active, completed, withdrawn).",
    "started": "Enrollments with at least one started lesson.",
    "lesson_completion": "Completed lessons divided by the number of lessons in each cohort's curriculum version, across selected enrollments.",
    "reached_project_stage": "Enrollments whose policy pre-project lessons are all complete.",
    "completion_rate": "Completed enrollments divided by eligible enrollments (active, completed and withdrawn) in cohorts that are not cancelled.",
    "attendance_rate": "Present records divided by eligible participant-sessions in finalized, completed sessions, excluding excused. Unrecorded entries are shown as incomplete data.",
    "active_facilitators": "Distinct approved facilitators with an active assignment in the selected cohorts.",
    "projects_completed": "Projects at the completed stage with facilitator confirmation.",
    "intake_year": "Filters on cohort start year (intake), not completion date.",
    "completion_period": "Completion period filters on the enrollment completion date.",
}


@dataclass
class Filters:
    year: int | None = None
    region: str | None = None
    district: str | None = None
    partner: str | None = None
    facilitator: str | None = None
    cohort: str | None = None
    completed_from: date | None = None
    completed_to: date | None = None
    applied: dict = field(default_factory=dict)

    @classmethod
    def from_query(cls, q) -> "Filters":
        def val(name):
            v = (q.get(name) or "").strip()
            return v or None

        f = cls()
        try:
            f.year = int(val("year")) if val("year") else None
        except ValueError:
            f.year = None
        f.region, f.district, f.partner = val("region"), val("district"), val("partner")
        f.facilitator, f.cohort = val("facilitator"), val("cohort")
        for name in ("completed_from", "completed_to"):
            raw = val(name)
            if raw:
                try:
                    setattr(f, name, date.fromisoformat(raw))
                except ValueError:
                    pass
        f.applied = {k: v for k, v in vars(f).items() if v not in (None, {}) and k != "applied"}
        return f


def filtered_cohorts(filters: Filters, base=None):
    qs = base if base is not None else Cohort.objects.all()
    qs = qs.select_related("partner", "district", "district__region", "curriculum_version")
    try:
        if filters.year:
            qs = qs.filter(start_date__year=filters.year)
        if filters.region:
            qs = qs.filter(district__region_id=filters.region)
        if filters.district:
            qs = qs.filter(district_id=filters.district)
        if filters.partner:
            qs = qs.filter(partner_id=filters.partner)
        if filters.cohort:
            qs = qs.filter(pk=filters.cohort)
        if filters.facilitator:
            qs = qs.filter(facilitator_assignments__facilitator_user_id=filters.facilitator).distinct()
        list(qs[:1])  # surface invalid UUIDs here
    except Exception:  # invalid identifiers in filters select nothing
        return Cohort.objects.none()
    return qs


def enrollment_rows(cohorts) -> list[dict]:
    """One row per enrollment with programme state only (no personal answers)."""
    cohorts = list(cohorts)
    if not cohorts:
        return []
    cohort_ids = [c.pk for c in cohorts]
    version_ids = {c.curriculum_version_id for c in cohorts}
    lessons_by_version = defaultdict(list)
    for lesson in Lesson.objects.filter(curriculum_version_id__in=version_ids):
        lessons_by_version[lesson.curriculum_version_id].append(lesson)

    enrollments = list(
        Enrollment.objects.filter(cohort_id__in=cohort_ids).select_related("participant_user", "cohort").order_by("cohort__name", "participant_user__full_name")
    )
    eids = [e.pk for e in enrollments]

    # Pre-aggregated progress
    progress = defaultdict(lambda: {"started": 0, "completed": 0, "overrides": 0, "completed_lessons": set()})
    for p in LessonProgress.objects.filter(enrollment_id__in=eids).values("enrollment_id", "lesson_id", "started_at", "completed_at", "completion_method"):
        row = progress[p["enrollment_id"]]
        if p["started_at"] or p["completed_at"]:
            row["started"] += 1
        if p["completed_at"]:
            row["completed"] += 1
            row["completed_lessons"].add(p["lesson_id"])
            if p["completion_method"] == "override":
                row["overrides"] += 1

    # Pre-aggregated attendance over finalized completed sessions
    sessions_by_cohort = defaultdict(list)
    for s in Session.objects.filter(cohort_id__in=cohort_ids, status=Session.COMPLETED, attendance_finalized_at__isnull=False):
        sessions_by_cohort[s.cohort_id].append(s)
    att = defaultdict(lambda: {"present": 0, "absent": 0, "excused": 0})
    att_seen = defaultdict(set)
    for a in Attendance.objects.filter(enrollment_id__in=eids, session__status=Session.COMPLETED, session__attendance_finalized_at__isnull=False).values(
        "enrollment_id", "session_id", "status"
    ):
        att[a["enrollment_id"]][a["status"]] += 1
        att_seen[a["enrollment_id"]].add(a["session_id"])

    # Pre-aggregated projects
    memberships = defaultdict(list)
    for m in ProjectMember.objects.filter(enrollment_id__in=eids, ended_at__isnull=True).values("enrollment_id", "project__stage", "project__completion_confirmed_by"):
        memberships[m["enrollment_id"]].append(m)
    overrides = set(ProgrammeCompletionOverride.objects.filter(enrollment_id__in=eids, revoked_at__isnull=True).values_list("enrollment_id", flat=True))

    rows = []
    for e in enrollments:
        c = e.cohort
        lessons = lessons_by_version[c.curriculum_version_id]
        pre_numbers = set(c.curriculum_version.completion_policy.get("pre_project_lessons", []))
        pre_ids = {lesson.pk for lesson in lessons if lesson.lesson_number in pre_numbers}
        prog = progress[e.pk]
        eligible_sessions = [
            s for s in sessions_by_cohort[c.pk]
            if s.pk in att_seen[e.pk]
            or (e.joined_at <= s.starts_at and (e.withdrawn_at is None or e.withdrawn_at > s.starts_at))
        ]
        a = att[e.pk]
        unrecorded = max(len(eligible_sessions) - (a["present"] + a["absent"] + a["excused"]), 0)
        projects = memberships[e.pk]
        rows.append({
            "enrollment_id": str(e.pk),
            "participant_user_id": str(e.participant_user_id),
            "participant_name": e.participant_user.full_name,
            "cohort_id": str(c.pk),
            "cohort_name": c.name,
            "cohort_status": c.status,
            "cohort_start_date": c.start_date.isoformat(),
            "partner": c.partner.name,
            "district": c.district.name,
            "region": c.district.region.name,
            "curriculum_version": c.curriculum_version.code,
            "enrollment_status": e.status,
            "joined_at": e.joined_at.date().isoformat(),
            "completed_at": e.completed_at.date().isoformat() if e.completed_at else "",
            "lessons_total": len(lessons),
            "lessons_started": prog["started"],
            "lessons_completed": prog["completed"],
            "lesson_overrides": prog["overrides"],
            "reached_project_stage": bool(pre_ids) and pre_ids <= prog["completed_lessons"],
            "finalized_sessions_eligible": len(eligible_sessions),
            "attendance_present": a["present"],
            "attendance_absent": a["absent"],
            "attendance_excused": a["excused"],
            "attendance_unrecorded": unrecorded,
            "in_project": bool(projects),
            "project_completed": any(p["project__stage"] == "completed" and p["project__completion_confirmed_by"] for p in projects),
            "programme_completed": e.status == Enrollment.COMPLETED,
            "programme_override": e.pk in overrides,
            "_completed_on": e.completed_at.date() if e.completed_at else None,
        })
    return rows


def _rate(num, den):
    return None if not den else round(100.0 * num / den, 1)


def summarize(rows: list[dict], cohorts, filters: Filters | None = None) -> dict:
    cohorts = list(cohorts)
    not_cancelled = {str(c.pk) for c in cohorts if c.status != Cohort.CANCELLED}
    eligible = [r for r in rows if r["cohort_id"] in not_cancelled]
    completed = [r for r in eligible if r["programme_completed"]]
    if filters and (filters.completed_from or filters.completed_to):
        completed_in_period = [
            r for r in completed
            if r["_completed_on"]
            and (not filters.completed_from or r["_completed_on"] >= filters.completed_from)
            and (not filters.completed_to or r["_completed_on"] <= filters.completed_to)
        ]
    else:
        completed_in_period = None
    lessons_done = sum(r["lessons_completed"] for r in rows)
    lessons_total = sum(r["lessons_total"] for r in rows)
    present = sum(r["attendance_present"] for r in rows)
    excused = sum(r["attendance_excused"] for r in rows)
    slots = sum(r["finalized_sessions_eligible"] for r in rows)
    cohort_ids = [c.pk for c in cohorts]
    projects = Project.objects.filter(cohort_id__in=cohort_ids)
    return {
        "cohorts": len(cohorts),
        "cohorts_by_status": dict(Counter(c.status for c in cohorts)),
        "enrollments": len(rows),
        "enrollments_by_status": dict(Counter(r["enrollment_status"] for r in rows)),
        "unique_participants": len({r["participant_user_id"] for r in rows}),
        "started": sum(1 for r in rows if r["lessons_started"] > 0),
        "lesson_completion_rate": _rate(lessons_done, lessons_total),
        "lesson_overrides": sum(r["lesson_overrides"] for r in rows),
        "reached_project_stage": sum(1 for r in rows if r["reached_project_stage"]),
        "programme_completed": len(completed),
        "programme_completed_by_override": sum(1 for r in completed if r["programme_override"]),
        "completed_in_period": None if completed_in_period is None else len(completed_in_period),
        "completion_rate": _rate(len(completed), len(eligible)),
        "completion_denominator": len(eligible),
        "attendance_present": present,
        "attendance_excused": excused,
        "attendance_unrecorded": sum(r["attendance_unrecorded"] for r in rows),
        "attendance_slots": slots,
        "attendance_rate": _rate(present, slots - excused),
        "active_facilitators": User.objects.filter(
            cohort_assignments__cohort_id__in=cohort_ids,
            cohort_assignments__ended_at__isnull=True,
            facilitator_profile__approval_status="approved",
        ).distinct().count(),
        "projects": projects.count(),
        "projects_completed": projects.filter(stage="completed", completion_confirmed_by__isnull=False).count(),
        "definitions": DEFINITIONS,
    }


def breakdown(rows: list[dict], key: str) -> list[dict]:
    groups = defaultdict(list)
    for r in rows:
        groups[r[key]].append(r)
    out = []
    for name, rs in sorted(groups.items()):
        out.append({
            "name": name,
            "enrollments": len(rs),
            "unique_participants": len({r["participant_user_id"] for r in rs}),
            "completed": sum(1 for r in rs if r["programme_completed"]),
            "lesson_completion_rate": _rate(sum(r["lessons_completed"] for r in rs), sum(r["lessons_total"] for r in rs)),
        })
    return out


# ------------------------------------------------------------------ CSV export (FR33)

EXPORT_COLUMNS = [
    "enrollment_id", "participant_user_id", "participant_name", "cohort_id", "cohort_name", "cohort_status", "cohort_start_date",
    "partner", "district", "region", "curriculum_version", "enrollment_status", "joined_at", "completed_at",
    "lessons_total", "lessons_started", "lessons_completed", "lesson_overrides", "reached_project_stage",
    "finalized_sessions_eligible", "attendance_present", "attendance_absent", "attendance_excused", "attendance_unrecorded",
    "in_project", "project_completed", "programme_completed", "programme_override",
]
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def neutralize(value) -> str:
    """Prevent spreadsheet formula execution for cells beginning with formula characters."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    text = "" if value is None else str(value)
    if text.startswith(FORMULA_PREFIXES):
        return "'" + text
    return text


def export_csv(rows: list[dict]) -> str:
    """Whitelisted columns only; private payloads are never part of the row model."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(EXPORT_COLUMNS)
    for r in rows:
        writer.writerow([neutralize(r[c]) for c in EXPORT_COLUMNS])
    return "﻿" + buf.getvalue()  # BOM so Excel opens UTF-8 correctly


# ------------------------------------------------------------------ facilitator dashboard (FR31)


def cohort_dashboard(cohort: Cohort) -> dict:
    rows = enrollment_rows([cohort])
    return {"rows": rows, "summary": summarize(rows, [cohort])}
