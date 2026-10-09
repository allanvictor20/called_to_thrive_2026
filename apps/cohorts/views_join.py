"""Participant join flow: enter code -> preview -> confirm (FR08)."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

from apps.core.errors import DomainError
from apps.core.ratelimit import check_rate

from . import services


@login_required
def join(request, code=""):
    ctx = {"code": code or request.GET.get("code", "")}
    if request.method == "POST":
        code = request.POST.get("code", "")
        ctx["code"] = code
        try:
            check_rate("invite", f"user:{request.user.pk}")
            if request.POST.get("confirm") == "1":
                enrollment, created = services.join_cohort(request.user, code)
                messages.success(request, "Welcome to your cohort!" if created else "You are already in this cohort.")
                return redirect("participant:journey", enrollment_id=enrollment.pk)
            ctx["preview"] = services.preview_invite(request.user, code)
        except DomainError as exc:
            ctx["error_message"] = exc.message
            return render(request, "participant/join.html", ctx, status=exc.status)
    elif ctx["code"]:
        try:
            ctx["preview"] = services.preview_invite(request.user, ctx["code"])
        except DomainError as exc:
            ctx["error_message"] = exc.message
    return render(request, "participant/join.html", ctx)
