"""Fixed response schemas for workbook activities.

Schemas are imported with a published curriculum version; there is no runtime
form builder (V1 scope). Supported field types:

    text, textarea          -> string
    list (count n)          -> list of n strings
    rating (min, max)       -> integer
    choice (options)        -> one option value
    multi_choice (options)  -> list of option values
"""
from apps.core.errors import ValidationFailed

MAX_TEXT = 5000
FIELD_TYPES = {"text", "textarea", "list", "rating", "choice", "multi_choice"}


def validate_schema_definition(schema: dict | None) -> None:
    if schema is None:
        return
    fields = schema.get("fields")
    if not isinstance(fields, list) or not fields:
        raise ValueError("response_schema.fields must be a non-empty list")
    keys = set()
    for f in fields:
        if f.get("type") not in FIELD_TYPES:
            raise ValueError(f"unsupported field type {f.get('type')!r}")
        if not f.get("key") or f["key"] in keys:
            raise ValueError(f"missing or duplicate field key {f.get('key')!r}")
        keys.add(f["key"])
        if f["type"] == "list" and int(f.get("count", 0)) < 1:
            raise ValueError("list fields need a positive count")
        if f["type"] == "rating" and not (int(f["min"]) < int(f["max"])):
            raise ValueError("rating fields need min < max")
        if f["type"] in {"choice", "multi_choice"} and not f.get("options"):
            raise ValueError("choice fields need options")


def _blank(v) -> bool:
    if v is None:
        return True
    if isinstance(v, str):
        return not v.strip()
    if isinstance(v, list):
        return all(_blank(x) for x in v)
    return False


def clean_payload(schema: dict, payload, *, for_submit: bool) -> dict:
    """Normalize a payload to the schema. Drafts allow blanks; submissions enforce required fields.

    Unknown keys are dropped. Type and range violations are rejected in both modes
    so a draft can never store an invalid shape.
    """
    if not isinstance(payload, dict):
        raise ValidationFailed("Your answers could not be read. Please try again.")
    errors: dict[str, str] = {}
    cleaned: dict = {}
    for f in schema["fields"]:
        key, ftype = f["key"], f["type"]
        raw = payload.get(key)
        try:
            if ftype in {"text", "textarea"}:
                value = "" if raw is None else str(raw)
                if len(value) > MAX_TEXT:
                    raise ValueError(f"Please keep this under {MAX_TEXT} characters.")
            elif ftype == "list":
                count = int(f["count"])
                items = raw if isinstance(raw, list) else []
                items = [("" if x is None else str(x)) for x in items][:count]
                items += [""] * (count - len(items))
                if any(len(x) > MAX_TEXT for x in items):
                    raise ValueError(f"Please keep each item under {MAX_TEXT} characters.")
                value = items
            elif ftype == "rating":
                if raw in (None, ""):
                    value = None
                else:
                    value = int(raw)
                    if not int(f["min"]) <= value <= int(f["max"]):
                        raise ValueError(f"Choose a value from {f['min']} to {f['max']}.")
            elif ftype == "choice":
                allowed = {o["value"] for o in f["options"]}
                value = None if raw in (None, "") else str(raw)
                if value is not None and value not in allowed:
                    raise ValueError("Choose one of the listed options.")
            elif ftype == "multi_choice":
                allowed = {o["value"] for o in f["options"]}
                values = raw if isinstance(raw, list) else ([] if raw in (None, "") else [raw])
                values = [str(v) for v in values]
                if any(v not in allowed for v in values):
                    raise ValueError("Choose from the listed options.")
                value = [o["value"] for o in f["options"] if o["value"] in values]
            else:  # pragma: no cover - guarded by validate_schema_definition
                raise ValueError("Unsupported field.")
        except (TypeError, ValueError) as exc:
            msg = str(exc)
            errors[key] = msg if msg and not msg.startswith("invalid literal") else "This value is not valid."
            continue
        if for_submit and f.get("required", False) and _blank(value):
            errors[key] = "This answer is required before you submit." if ftype != "list" else "Please fill in every line before you submit."
            continue
        if for_submit and ftype == "list" and f.get("required", False) and any(_blank(x) for x in value):
            errors[key] = "Please fill in every line before you submit."
            continue
        cleaned[key] = value
    if errors:
        raise ValidationFailed("Some answers need attention.", fields=errors)
    return cleaned
