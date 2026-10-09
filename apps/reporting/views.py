from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone

from apps.accounts.models import User
from apps.cohorts.models import Cohort
from apps.cohorts.policies import require_admin
from apps.programme.models import District, Partner, Region

from . import services


@login_required
def programme(request):
    """GET /reports/programme — aggregate summary with filters (FR32)."""
    require_admin(request.user)
    filters = services.Filters.from_query(request.GET)
    cohorts = list(services.filtered_cohorts(filters))
    rows = services.enrollment_rows(cohorts)
    return render(request, "reporting/programme.html", {
        "filters": filters,
        "summary": services.summarize(rows, cohorts, filters),
        "by_region": services.breakdown(rows, "region"),
        "by_district": services.breakdown(rows, "district"),
        "by_partner": services.breakdown(rows, "partner"),
        "by_cohort": services.breakdown(rows, "cohort_name"),
        "regions": Region.objects.all(),
        "districts": District.objects.select_related("region"),
        "partners": Partner.objects.all(),
        "facilitators": User.objects.filter(facilitator_profile__approval_status="approved").order_by("full_name"),
        "cohorts": Cohort.objects.order_by("name"),
        "years": sorted({c.start_date.year for c in Cohort.objects.only("start_date")}, reverse=True),
        "query": request.GET.urlencode(),
        "definitions": services.DEFINITIONS,
    })


@login_required
def export(request):
    """GET /reports/export — sanitized CSV of whitelisted programme fields (FR33)."""
    require_admin(request.user)
    filters = services.Filters.from_query(request.GET)
    cohorts = list(services.filtered_cohorts(filters))
    body = services.export_csv(services.enrollment_rows(cohorts))
    response = HttpResponse(body, content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="called-to-thrive-enrollments-{timezone.localdate():%Y%m%d}.csv"'
    return response
