"""Facilitator pages: cohorts, invites, sessions, attendance, lesson guide, progress and completion review."""
from datetime import datetime
from zoneinfo import ZoneInfo

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.core.errors import DomainError, NotFound, ValidationFailed
from apps.core.utils import opt_date, opt_int
from apps.curriculum.models import CurriculumVersion, Lesson, LessonComponent
from apps.learning import services as learning
from apps.learning.models import ComponentResponse, LessonProgress
from apps.programme.models import District, Partner
from apps.projects.models import Project
from apps.reporting.services import cohort_dashboard

from . import services
from .models import Cohort, CohortInvite, Enrollment, Session
from .policies import cohort_for_facilitator, enrollment_for_facilitator, facilitator_cohorts, is_lead


def _local_dt(value: str | None):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


@login_required
def cohort_list(request):
    user = request.user
    profile = getattr(user, "facilitator_profile", None) if user.has_role("facilitator") else None
    cohorts = facilitator_cohorts(user).select_related("partner", "district").order_by("-start_date")
    return render(request, "facilitator/cohort_list.html", {"cohorts": cohorts, "profile": profile})


@login_required
def cohort_create(request):
    user = request.user
    partners = Partner.objects.filter(status="active", affiliations__facilitator_user=user, affiliations__status="active").distinct()
    ctx = {
        "partners": partners,
        "districts": District.objects.select_related("region"),
        "versions": CurriculumVersion.objects.filter(status=CurriculumVersion.PUBLISHED),
        "data": {}, "errors": {},
    }
    if request.method == "POST":
        ctx["data"] = request.POST
        try:
            partner = partners.filter(pk=request.POST.get("partner")).first()
            district = District.objects.filter(pk=request.POST.get("district")).first() if request.POST.get("district") else None
            version = ctx["versions"].filter(pk=request.POST.get("curriculum_version")).first() if request.POST.get("curriculum_version") else None
            missing = {k: "This is required." for k, v in (("partner", partner), ("district", district), ("curriculum_version", version)) if v is None}
            if not opt_date(request.POST.get("start_date")):
                missing["start_date"] = "Choose a start date."
            if missing:
                raise ValidationFailed("Please correct the highlighted fields.", fields=missing)
            cohort = services.create_cohort(
                user, partner=partner, district=district, curriculum_version=version, name=request.POST.get("name", ""),
                start_date=opt_date(request.POST.get("start_date")), end_date=opt_date(request.POST.get("end_date")),
                default_meeting_type=request.POST.get("default_meeting_type", ""), default_meet_url=request.POST.get("default_meet_url"),
            )
        except DomainError as exc:
            ctx["errors"], ctx["error_message"] = exc.fields, exc.message
            return render(request, "facilitator/cohort_form.html", ctx, status=exc.status)
        messages.success(request, "Cohort created. Next, create a join code to invite participants.")
        return redirect("facilitator:cohort", cohort_id=cohort.pk)
    return render(request, "facilitator/cohort_form.html", ctx)


@login_required
def cohort_detail(request, cohort_id):
    cohort = cohort_for_facilitator(request.user, cohort_id)
    if request.method == "POST" and request.POST.get("action") == "current_lesson":
        lesson = Lesson.objects.filter(pk=request.POST.get("lesson"), curriculum_version_id=cohort.curriculum_version_id).first()
        services.set_current_lesson(request.user, cohort, lesson)
        messages.success(request, "Current lesson updated.")
        return redirect("facilitator:cohort", cohort_id=cohort.pk)
    dash = cohort_dashboard(cohort)
    sessions = list(cohort.sessions.select_related("lesson").order_by("starts_at"))
    now = timezone.now()
    return render(request, "facilitator/cohort_detail.html", {
        "cohort": cohort,
        "dash": dash,
        # What the facilitator should do next: the coming session, and past sessions still waiting for attendance.
        "next_session": next((s for s in sessions if s.status == Session.SCHEDULED and s.starts_at >= now), None),
        "needs_attendance": [s for s in sessions if s.status == Session.SCHEDULED and s.starts_at < now],
        "is_lead": is_lead(request.user, cohort),
        "lessons": Lesson.objects.filter(curriculum_version_id=cohort.curriculum_version_id).order_by("lesson_number"),
        "sessions": sessions,
        "facilitators": cohort.facilitator_assignments.filter(ended_at__isnull=True).select_related("facilitator_user"),
        "projects": cohort.projects.all(),
        "status_events": cohort.status_events.select_related("changed_by"),
    })


@login_required
def invites(request, cohort_id):
    cohort = cohort_for_facilitator(request.user, cohort_id, lead=True)
    ctx = {"cohort": cohort, "errors": {}, "data": {}}
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "revoke":
            invite = CohortInvite.objects.filter(pk=request.POST.get("invite"), cohort=cohort).first()
            if invite is None:
                raise NotFound("Invite not found.")
            services.revoke_invite(request.user, invite)
            messages.success(request, "Join code withdrawn. It can no longer be used.")
            return redirect("facilitator:invites", cohort_id=cohort.pk)
        ctx["data"] = request.POST
        try:
            expires = opt_date(request.POST.get("expires_on"))
            expires_at = timezone.make_aware(datetime.combine(expires, datetime.max.time().replace(microsecond=0))) if expires else None
            invite, code = services.create_invite(request.user, cohort, expires_at=expires_at, max_uses=opt_int(request.POST.get("max_uses")))
        except DomainError as exc:
            ctx["errors"], ctx["error_message"] = exc.fields, exc.message
        else:
            ctx["new_code"] = services.format_code(code)
            ctx["new_link"] = request.build_absolute_uri(reverse("cohorts:join_code", args=[ctx["new_code"]]))
    ctx["invites"] = cohort.invites.order_by("-created_at")
    ctx["now"] = timezone.now()
    return render(request, "facilitator/invites.html", ctx)


@login_required
def session_form(request, cohort_id, session_id=None):
    cohort = cohort_for_facilitator(request.user, cohort_id)
    session = Session.objects.filter(pk=session_id, cohort=cohort).first() if session_id else None
    if session_id and session is None:
        raise NotFound("Session not found.")
    lessons = Lesson.objects.filter(curriculum_version_id=cohort.curriculum_version_id).order_by("lesson_number")
    tz = ZoneInfo(cohort.timezone)
    if session:
        data = {
            "title": session.title, "meeting_type": session.meeting_type, "meet_url": session.meet_url or "", "venue": session.venue or "",
            "lesson": str(session.lesson_id or ""), "starts_at": session.starts_at.astimezone(tz).strftime("%Y-%m-%dT%H:%M"),
            "ends_at": session.ends_at.astimezone(tz).strftime("%Y-%m-%dT%H:%M") if session.ends_at else "",
        }
    else:
        data = {"meeting_type": cohort.default_meeting_type, "meet_url": cohort.default_meet_url or "", "lesson": str(cohort.current_lesson_id or "")}
    ctx = {"cohort": cohort, "session": session, "lessons": lessons, "data": data, "errors": {}}
    if request.method == "POST":
        ctx["data"] = request.POST
        lesson = lessons.filter(pk=request.POST.get("lesson")).first() if request.POST.get("lesson") else None
        try:
            saved = services.schedule_session(
                request.user, cohort, title=request.POST.get("title", ""), starts_at=_local_dt(request.POST.get("starts_at")),
                ends_at=_local_dt(request.POST.get("ends_at")), meeting_type=request.POST.get("meeting_type", ""),
                meet_url=request.POST.get("meet_url"), venue=request.POST.get("venue"), lesson=lesson, session=session,
            )
        except DomainError as exc:
            ctx["errors"], ctx["error_message"] = exc.fields, exc.message
            return render(request, "facilitator/session_form.html", ctx, status=exc.status)
        messages.success(request, "Session saved.")
        return redirect("facilitator:attendance", session_id=saved.pk)
    return render(request, "facilitator/session_form.html", ctx)


@login_required
def attendance(request, session_id):
    """FR13: record statuses, review totals, then save; finalize separately (FR14)."""
    session = Session.objects.select_related("cohort", "lesson").filter(pk=session_id).first()
    if session is None:
        raise NotFound("Session not found.")
    cohort = cohort_for_facilitator(request.user, session.cohort_id)
    eligible = list(services.eligible_enrollments(session))
    recorded = {a.enrollment_id: a.status for a in session.attendance.all()}
    ctx = {"session": session, "cohort": cohort, "stage": "edit"}
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "cancel":
                services.cancel_session(request.user, session)
                messages.info(request, "Session cancelled. Learning records are unchanged.")
                return redirect("facilitator:cohort", cohort_id=cohort.pk)
            if action == "finalize":
                services.finalize_session(request.user, session, int(request.POST.get("revision", 0)))
                messages.success(request, "Attendance finalized. Lesson work is unaffected.")
                return redirect("facilitator:attendance", session_id=session.pk)
            statuses = {str(e.pk): request.POST.get(f"att_{e.pk}", "") or None for e in eligible}
            if action == "review":
                ctx["stage"] = "review"
                ctx["statuses"] = statuses
                ctx["rows"] = [{"e": e, "status": statuses[str(e.pk)], "before": recorded.get(e.pk)} for e in eligible]
                ctx["totals"] = {k: sum(1 for v in statuses.values() if v == k) for k in ("present", "absent", "excused")}
                ctx["totals"]["unrecorded"] = sum(1 for v in statuses.values() if not v)
                ctx["changes"] = sum(1 for e in eligible if statuses[str(e.pk)] != recorded.get(e.pk))
                ctx["revision"] = request.POST.get("revision")
                return render(request, "facilitator/attendance.html", ctx)
            if action == "save":
                services.record_attendance(request.user, session, statuses, int(request.POST.get("revision", 0)))
                messages.success(request, "Attendance saved.")
                return redirect("facilitator:attendance", session_id=session.pk)
        except DomainError as exc:
            messages.error(request, exc.message)
            return redirect("facilitator:attendance", session_id=session.pk)
    ctx["rows"] = [{"e": e, "status": recorded.get(e.pk)} for e in eligible]
    ctx["summary"] = services.attendance_summary(session)
    return render(request, "facilitator/attendance.html", ctx)


@login_required
def lesson_guide(request, cohort_id, number):
    """FR16 facilitator view + FR18 discussion confirmation + completion counts (no private text)."""
    cohort = cohort_for_facilitator(request.user, cohort_id)
    lesson = Lesson.objects.filter(curriculum_version_id=cohort.curriculum_version_id, lesson_number=number).first()
    if lesson is None:
        raise NotFound("Lesson not found.")
    enrollments = list(cohort.enrollments.exclude(status=Enrollment.WITHDRAWN).select_related("participant_user").order_by("participant_user__full_name"))
    if request.method == "POST":
        component = LessonComponent.objects.filter(pk=request.POST.get("component"), lesson=lesson).first()
        if component is None:
            raise NotFound("Activity not found.")
        try:
            n = learning.confirm_discussion(request.user, cohort, component, request.POST.getlist("enrollments"))
            messages.success(request, f"Confirmed participation for {n} participant(s).")
        except DomainError as exc:
            messages.error(request, exc.message)
        return redirect("facilitator:lesson_guide", cohort_id=cohort.pk, number=number)
    components = list(lesson.components.order_by("display_order"))
    done = {}
    for r in ComponentResponse.objects.filter(component__in=components, enrollment__in=enrollments, status=ComponentResponse.COMPLETED):
        done.setdefault(r.component_id, set()).add(r.enrollment_id)
    items = [{"c": learning.facilitator_component_view(c), "obj": c, "done_ids": done.get(c.pk, set()), "done_count": len(done.get(c.pk, set()))} for c in components]
    lessons = Lesson.objects.filter(curriculum_version_id=cohort.curriculum_version_id).order_by("lesson_number")
    return render(request, "facilitator/lesson_guide.html", {"cohort": cohort, "lesson": lesson, "items": items, "enrollments": enrollments, "lessons": lessons})


@login_required
def participant_detail(request, cohort_id, enrollment_id):
    enrollment = enrollment_for_facilitator(request.user, enrollment_id)
    cohort = enrollment.cohort
    if str(cohort.pk) != str(cohort_id):
        raise NotFound("Participant not found.")
    if request.method == "POST":
        lesson = Lesson.objects.filter(pk=request.POST.get("lesson"), curriculum_version_id=cohort.curriculum_version_id).first()
        try:
            if lesson is None:
                raise NotFound("Lesson not found.")
            learning.override_lesson(request.user, enrollment, lesson, request.POST.get("reason", ""))
            messages.success(request, f"Lesson {lesson.lesson_number} completed by exception. The reason is recorded.")
        except DomainError as exc:
            messages.error(request, exc.message)
        return redirect("facilitator:participant", cohort_id=cohort.pk, enrollment_id=enrollment.pk)
    lessons = list(Lesson.objects.filter(curriculum_version_id=cohort.curriculum_version_id).order_by("lesson_number").prefetch_related("components"))
    progress = {p.lesson_id: p for p in LessonProgress.objects.filter(enrollment=enrollment).select_related("confirmed_by")}
    responses = {r.component_id: r for r in ComponentResponse.objects.filter(enrollment=enrollment).select_related("component", "enrollment__cohort")}
    rows = []
    for lesson in lessons:
        comps = []
        for c in lesson.components.all():
            if not c.required and c.completion_method is None:
                continue
            r = responses.get(c.pk)
            view = learning.response_for_viewer(request.user, r) if r else None
            comps.append({"c": c, "r": r, "view": view})
        rows.append({"lesson": lesson, "progress": progress.get(lesson.pk), "components": comps})
    return render(request, "facilitator/participant.html", {
        "cohort": cohort, "enrollment": enrollment, "rows": rows, "is_lead": is_lead(request.user, cohort),
        "checklist": learning.programme_checklist(enrollment),
        "attendance": enrollment.attendance.select_related("session").order_by("session__starts_at"),
    })


@login_required
def completion_review(request, cohort_id):
    cohort = cohort_for_facilitator(request.user, cohort_id)
    lead = is_lead(request.user, cohort)
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "confirm":
                enrollment = Enrollment.objects.filter(pk=request.POST.get("enrollment"), cohort=cohort).first()
                if enrollment is None:
                    raise NotFound("Participant not found.")
                learning.confirm_programme_completion(request.user, enrollment, override_reason=request.POST.get("override_reason", ""))
                messages.success(request, f"{enrollment.participant_user.full_name}: programme completion confirmed.")
            elif action == "status":
                services.change_cohort_status(
                    request.user, cohort, request.POST.get("to_status", ""), reason=request.POST.get("reason", ""), reviewed=request.POST.get("reviewed") == "1"
                )
                messages.success(request, "Cohort status updated.")
        except DomainError as exc:
            messages.error(request, exc.message)
        return redirect("facilitator:completion", cohort_id=cohort.pk)
    review = services.completion_review(cohort)
    transitions = set(Cohort.TRANSITIONS.get(cohort.status, set()))
    if cohort.status == Cohort.COMPLETED:
        transitions.discard(Cohort.ACTIVE)  # reopening is an administrator correction
    return render(request, "facilitator/completion.html", {"cohort": cohort, "review": review, "is_lead": lead, "transitions": sorted(transitions)})


@login_required
def facilitators(request, cohort_id):
    cohort = cohort_for_facilitator(request.user, cohort_id, lead=True)
    if request.method == "POST":
        try:
            user = User.objects.filter(pk=request.POST.get("user")).first()
            if user is None:
                raise ValidationFailed("Choose a facilitator.")
            if request.POST.get("action") == "end":
                services.end_assignment(request.user, cohort, user)
                messages.success(request, "Assignment ended.")
            else:
                services.assign_facilitator(request.user, cohort, user, request.POST.get("role", "assistant"))
                messages.success(request, f"{user.full_name} assigned.")
        except DomainError as exc:
            messages.error(request, exc.message)
        return redirect("facilitator:facilitators", cohort_id=cohort.pk)
    assigned = cohort.facilitator_assignments.filter(ended_at__isnull=True).select_related("facilitator_user")
    candidates = User.objects.filter(
        affiliations__partner=cohort.partner, affiliations__status="active", facilitator_profile__approval_status="approved"
    ).exclude(pk__in=[a.facilitator_user_id for a in assigned]).distinct()
    return render(request, "facilitator/facilitators.html", {"cohort": cohort, "assigned": assigned, "candidates": candidates})


@login_required
def projects(request, cohort_id):
    from apps.projects import services as project_services

    cohort = cohort_for_facilitator(request.user, cohort_id)
    ctx = {"cohort": cohort, "data": {}, "errors": {}}
    if request.method == "POST":
        ctx["data"] = request.POST
        try:
            project = project_services.create_project(
                request.user, cohort, name=request.POST.get("name", ""), problem_statement=request.POST.get("problem_statement", ""),
                idea_summary=request.POST.get("idea_summary", ""), community_benefit=request.POST.get("community_benefit", ""),
                target_start_date=opt_date(request.POST.get("target_start_date")), target_end_date=opt_date(request.POST.get("target_end_date")),
            )
        except DomainError as exc:
            ctx["errors"], ctx["error_message"] = exc.fields, exc.message
        else:
            messages.success(request, "Project created. Add team members and roles next.")
            return redirect("projects:detail", project_id=project.pk)
    ctx["projects"] = Project.objects.filter(cohort=cohort).prefetch_related("members__enrollment__participant_user")
    return render(request, "facilitator/projects.html", ctx)
