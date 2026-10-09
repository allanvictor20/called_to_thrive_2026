from django.contrib.auth.decorators import login_required
from django.db import connection
from django.http import JsonResponse
from django.shortcuts import redirect


@login_required
def home(request):
    role = getattr(request, "active_role", None)
    if role == "admin":
        return redirect("admin_console:dashboard")
    if role == "facilitator":
        return redirect("facilitator:cohorts")
    return redirect("participant:home")


def healthz(request):
    """Readiness check: application up and database reachable. Exposes no data."""
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
        return JsonResponse({"status": "ok"})
    except Exception:  # pragma: no cover
        return JsonResponse({"status": "database_unavailable"}, status=503)
