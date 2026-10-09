from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordResetForm
from django.shortcuts import redirect, render
from django.urls import reverse_lazy
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from apps.core.errors import Throttled, ValidationFailed
from apps.core.ratelimit import check_rate, client_ip
from apps.core.utils import opt_date
from apps.programme.models import District

from . import services
from .models import FacilitatorProfile, ParticipantProfile, User


def _safe_next(request, default="core:home"):
    nxt = request.POST.get("next") or request.GET.get("next")
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return nxt
    return reverse_lazy(default)


def register(request):
    if request.user.is_authenticated:
        return redirect("core:home")
    ctx = {"data": {}, "errors": {}, "districts": District.objects.select_related("region"), "next": request.GET.get("next", "")}
    if request.method == "POST":
        ctx["data"] = request.POST
        try:
            check_rate("register", client_ip(request))
            user = services.register_participant(
                full_name=request.POST.get("full_name"),
                email=request.POST.get("email"),
                phone=request.POST.get("phone"),
                password=request.POST.get("password"),
                date_of_birth=opt_date(request.POST.get("date_of_birth")),
                gender=request.POST.get("gender") or None,
                district=District.objects.filter(pk=request.POST.get("district")).first() if request.POST.get("district") else None,
            )
        except ValidationFailed as exc:
            ctx["errors"] = exc.fields
            ctx["error_message"] = exc.message
            return render(request, "accounts/register.html", ctx, status=422)
        except Throttled as exc:
            ctx["error_message"] = exc.message
            return render(request, "accounts/register.html", ctx, status=429)
        login(request, user, backend="apps.accounts.backends.ContactBackend")
        services.start_verification(user, "email" if user.email else "phone", request)
        messages.success(request, "Your account is ready. We sent you a verification message.")
        return redirect(f"{reverse_lazy('accounts:verify')}?next={_safe_next(request)}")
    return render(request, "accounts/register.html", ctx)


def login_view(request):
    if request.user.is_authenticated:
        return redirect("core:home")
    ctx = {"contact": "", "next": request.GET.get("next", "")}
    if request.method == "POST":
        contact = (request.POST.get("contact") or "").strip()
        ctx["contact"] = contact
        try:
            check_rate("login", client_ip(request))
            check_rate("login", f"contact:{contact.lower()}")
        except Throttled as exc:
            ctx["error_message"] = exc.message
            return render(request, "accounts/login.html", ctx, status=429)
        user = authenticate(request, contact=contact, password=request.POST.get("password"))
        if user is None:
            ctx["error_message"] = "That contact and password do not match an active account."
            return render(request, "accounts/login.html", ctx, status=401)
        login(request, user)  # rotates the session key
        return redirect(_safe_next(request))
    return render(request, "accounts/login.html", ctx)


@require_POST
def logout_view(request):
    logout(request)
    return redirect("accounts:login")


@login_required
def verify(request):
    user = request.user
    ctx = {"next": request.GET.get("next", "")}
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "resend_email" and user.email:
            services.start_verification(user, "email", request)
            messages.info(request, "We sent a new link to your email.")
        elif action == "send_phone" and user.phone_e164:
            services.start_verification(user, "phone", request)
            messages.info(request, "We sent a code to your phone.")
        elif action == "check_phone":
            if services.verify_phone_code(user, request.POST.get("code", "")):
                messages.success(request, "Your phone number is verified.")
                return redirect(_safe_next(request))
            ctx["error_message"] = "That code is not valid or has expired."
        return render(request, "accounts/verify.html", ctx)
    return render(request, "accounts/verify.html", ctx)


def verify_email(request, token):
    user = services.verify_email_token(token)
    if user is None:
        return render(request, "accounts/verify_failed.html", status=400)
    messages.success(request, "Your email address is verified.")
    if request.user.is_authenticated:
        return redirect("core:home")
    return redirect("accounts:login")


@login_required
def profile(request):
    user = request.user
    prof, _ = ParticipantProfile.objects.get_or_create(user=user)
    ctx = {"profile": prof, "districts": District.objects.select_related("region"), "errors": {}}
    if request.method == "POST":
        name = (request.POST.get("full_name") or "").strip()
        if not name:
            ctx["errors"] = {"full_name": "Enter your name."}
            return render(request, "accounts/profile.html", ctx, status=422)
        user.full_name = name
        user.save(update_fields=["full_name", "updated_at"])
        prof.date_of_birth = opt_date(request.POST.get("date_of_birth"))
        prof.gender = request.POST.get("gender") or None
        prof.district = District.objects.filter(pk=request.POST.get("district")).first() if request.POST.get("district") else None
        prof.save()
        messages.success(request, "Profile saved.")
        return redirect("accounts:profile")
    return render(request, "accounts/profile.html", ctx)


@login_required
@require_POST
def switch_role(request):
    role = request.POST.get("role")
    if role in request.user.role_set():
        request.session["active_role"] = role  # same user identity, different navigation
    return redirect("core:home")


@login_required
def apply_facilitator(request):
    profile = FacilitatorProfile.objects.filter(user=request.user).first()
    if request.method == "POST":
        services.apply_as_facilitator(request.user, request.POST.get("note", ""))
        request.session["active_role"] = "facilitator"
        messages.success(request, "Thank you. An administrator will review your facilitator application.")
        return redirect("facilitator:cohorts")
    return render(request, "accounts/apply_facilitator.html", {"profile": profile})


class VerifiedPasswordResetForm(PasswordResetForm):
    """Recovery uses verified channels only (FR01)."""

    def get_users(self, email):
        return User.objects.filter(email__iexact=email, email_verified_at__isnull=False, status="active")


class PasswordResetView(auth_views.PasswordResetView):
    form_class = VerifiedPasswordResetForm
    template_name = "registration/password_reset_form.html"
    success_url = reverse_lazy("accounts:password_reset_done")
    email_template_name = "registration/password_reset_email.txt"

    def post(self, request, *args, **kwargs):
        try:
            check_rate("password_reset", client_ip(request))
        except Throttled as exc:
            return render(request, "error.html", {"error": exc}, status=429)
        return super().post(request, *args, **kwargs)


class PasswordResetConfirmView(auth_views.PasswordResetConfirmView):
    template_name = "registration/password_reset_confirm.html"
    success_url = reverse_lazy("accounts:password_reset_complete")
