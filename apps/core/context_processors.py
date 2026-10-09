def roles(request):
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {"user_roles": [], "active_role": None}
    return {"user_roles": sorted(user.role_set()), "active_role": getattr(request, "active_role", None)}
