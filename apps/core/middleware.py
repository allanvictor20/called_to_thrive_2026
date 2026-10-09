import logging
import time

from django.http import JsonResponse
from django.shortcuts import render

from .errors import DomainError

logger = logging.getLogger("ctt.request")


class SafeRequestLogMiddleware:
    """NFR08: log record IDs, outcome and timing only.

    Logs the resolved route name (not the raw path or query string), the user
    id, status code and duration. Never bodies, tokens, contact values or
    reflection payloads.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        started = time.monotonic()
        response = self.get_response(request)
        match = getattr(request, "resolver_match", None)
        route = match.view_name if match else "unresolved"
        user_id = getattr(getattr(request, "user", None), "pk", None)
        ids = {k: str(v) for k, v in (match.kwargs.items() if match else []) if k.endswith("id") or k == "pk"}
        logger.info(
            "method=%s route=%s user=%s ids=%s status=%s ms=%d",
            request.method,
            route,
            user_id,
            ids,
            response.status_code,
            (time.monotonic() - started) * 1000,
        )
        return response

    def process_exception(self, request, exception):
        if isinstance(exception, DomainError):
            wants_json = request.path.startswith("/api/") or "application/json" in request.headers.get("Accept", "")
            if wants_json:
                return JsonResponse({"error": exception.as_dict()}, status=exception.status)
            return render(request, "error.html", {"error": exception}, status=exception.status)
        return None


class NoStoreAuthenticatedMiddleware:
    """D06: authenticated personal pages must not be cached by browsers or workers."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if getattr(request, "user", None) is not None and request.user.is_authenticated:
            response["Cache-Control"] = "no-store, private"
        return response


class ActiveRoleMiddleware:
    """FR02: one account, several roles; the active role is a session preference.

    Switching role never changes the user identity. Authorization is still
    evaluated per resource; the active role only selects navigation.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.active_role = None
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            roles = user.role_set()
            role = request.session.get("active_role")
            if role not in roles:
                for candidate in ("admin", "facilitator", "participant"):
                    if candidate in roles:
                        role = candidate
                        break
                request.session["active_role"] = role
            request.active_role = role
        return self.get_response(request)
