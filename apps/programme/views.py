"""Administrator console: facilitator approval, partners and affiliations, geography, cohort oversight."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.shortcuts import redirect, render

from apps.accounts import services as account_services
from apps.accounts.models import FacilitatorProfile, User
from apps.cohorts import services as cohort_services
from apps.cohorts.models import Cohort
from apps.cohorts.policies import require_admin
from apps.core.errors import DomainError, NotFound, ValidationFailed
from apps.reporting.services import Filters, enrollment_rows, filtered_cohorts, summarize

from .models import District, Partner, PartnerFacilitator, Region


@login_required
def dashboard(request):
    require_admin(request.user)
    cohorts = filtered_cohorts(Filters())
    rows = enrollment_rows(cohorts)
    return render(request, "admin_console/dashboard.html", {
        "summary": summarize(rows, cohorts),
        "pending": FacilitatorProfile.objects.filter(approval_status="pending").count(),
        "recent_cohorts": Cohort.objects.select_related("partner", "district").order_by("-created_at")[:8],
    })


@login_required
def facilitators(request):
    require_admin(request.user)
    if request.method == "POST":
        profile = FacilitatorProfile.objects.filter(pk=request.POST.get("profile")).select_related("user").first()
        if profile is None:
            raise NotFound("Facilitator not found.")
        try:
            account_services.set_facilitator_status(request.user, profile, request.POST.get("status", ""))
            messages.success(request, f"{profile.user.full_name}: {profile.get_approval_status_display().lower()}.")
        except DomainError as exc:
            messages.error(request, exc.message)
        return redirect("admin_console:facilitators")
    status = request.GET.get("status", "pending")
    profiles = FacilitatorProfile.objects.select_related("user", "approved_by").order_by("-created_at")
    if status != "all":
        profiles = profiles.filter(approval_status=status)
    return render(request, "admin_console/facilitators.html", {"profiles": profiles.prefetch_related("user__affiliations__partner"), "status": status})


@login_required
def partners(request):
    require_admin(request.user)
    ctx = {"data": {}, "errors": {}}
    if request.method == "POST":
        ctx["data"] = request.POST
        name = (request.POST.get("name") or "").strip()
        otype = request.POST.get("organisation_type")
        errors = {}
        if not name:
            errors["name"] = "Enter the partner's name."
        if otype not in {"church", "school", "ministry", "other"}:
            errors["organisation_type"] = "Choose a type."
        if errors:
            ctx["errors"], ctx["error_message"] = errors, "Please correct the highlighted fields."
        else:
            district = District.objects.filter(pk=request.POST.get("district")).first() if request.POST.get("district") else None
            partner = Partner.objects.create(name=name, organisation_type=otype, district=district)
            messages.success(request, "Partner added.")
            return redirect("admin_console:partner", partner_id=partner.pk)
    ctx["partners"] = Partner.objects.select_related("district").annotate(
        n_cohorts=Count("cohorts", distinct=True), n_facilitators=Count("affiliations", filter=Q(affiliations__status="active"), distinct=True)
    )
    ctx["districts"] = District.objects.select_related("region")
    return render(request, "admin_console/partners.html", ctx)


@login_required
def partner_detail(request, partner_id):
    require_admin(request.user)
    partner = Partner.objects.filter(pk=partner_id).first()
    if partner is None:
        raise NotFound("Partner not found.")
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "archive":
                partner.status = "archived" if partner.status == "active" else "active"
                partner.save(update_fields=["status", "updated_at"])
                messages.success(request, f"Partner {partner.status}.")
            else:
                user = User.objects.filter(pk=request.POST.get("user")).first()
                if user is None:
                    raise ValidationFailed("Choose a facilitator.")
                account_services.set_affiliation(request.user, partner, user, active=(action != "deactivate"))
                messages.success(request, "Affiliation updated.")
        except DomainError as exc:
            messages.error(request, exc.message)
        return redirect("admin_console:partner", partner_id=partner.pk)
    affiliations = PartnerFacilitator.objects.filter(partner=partner).select_related("facilitator_user__facilitator_profile")
    candidates = User.objects.filter(roles__role="facilitator").exclude(pk__in=[a.facilitator_user_id for a in affiliations if a.status == "active"]).select_related("facilitator_profile").order_by("full_name")
    return render(request, "admin_console/partner_detail.html", {
        "partner": partner, "affiliations": affiliations, "candidates": candidates,
        "cohorts": partner.cohorts.select_related("district").order_by("-start_date"),
    })


@login_required
def geography(request):
    require_admin(request.user)
    if request.method == "POST":
        name = (request.POST.get("name") or "").strip()
        if not name:
            messages.error(request, "Enter a name.")
        elif request.POST.get("action") == "region":
            Region.objects.get_or_create(name=name)
            messages.success(request, "Region saved.")
        else:
            region = Region.objects.filter(pk=request.POST.get("region")).first()
            if region is None:
                messages.error(request, "Choose a region.")
            else:
                District.objects.get_or_create(region=region, name=name)
                messages.success(request, "District saved.")
        return redirect("admin_console:geography")
    return render(request, "admin_console/geography.html", {"regions": Region.objects.prefetch_related("districts")})


@login_required
def cohort_oversight(request, cohort_id):
    """Programme state for one cohort. No personal response content is shown to administrators."""
    require_admin(request.user)
    cohort = Cohort.objects.select_related("partner", "district__region", "curriculum_version").filter(pk=cohort_id).first()
    if cohort is None:
        raise NotFound("Cohort not found.")
    if request.method == "POST":
        try:
            cohort_services.change_cohort_status(
                request.user, cohort, request.POST.get("to_status", ""), reason=request.POST.get("reason", ""), reviewed=request.POST.get("reviewed") == "1"
            )
            messages.success(request, "Cohort status updated.")
        except DomainError as exc:
            messages.error(request, exc.message)
        return redirect("admin_console:cohort", cohort_id=cohort.pk)
    rows = enrollment_rows([cohort])
    return render(request, "admin_console/cohort.html", {
        "cohort": cohort, "rows": rows, "summary": summarize(rows, [cohort]),
        "transitions": sorted(Cohort.TRANSITIONS.get(cohort.status, set())),
        "facilitators": cohort.facilitator_assignments.filter(ended_at__isnull=True).select_related("facilitator_user"),
        "events": cohort.status_events.select_related("changed_by"),
    })
