from django import template

register = template.Library()


@register.filter
def get(mapping, key):
    """Dictionary lookup by a variable key: {{ payload|get:field.key }}."""
    if mapping is None:
        return None
    try:
        return mapping.get(key)
    except AttributeError:
        try:
            return mapping[key]
        except (KeyError, IndexError, TypeError):
            return None


@register.filter
def index(seq, i):
    try:
        return seq[int(i)]
    except (IndexError, TypeError, ValueError):
        return ""


@register.filter
def str_(value):
    return "" if value is None else str(value)


@register.filter
def pct(num, den):
    try:
        return int(round(100 * float(num) / float(den)))
    except (TypeError, ValueError, ZeroDivisionError):
        return 0


@register.simple_tag
def range_list(n):
    return range(int(n))


@register.filter
def display(value):
    """Readable form of a stored answer: lists joined, blanks dropped."""
    if isinstance(value, list):
        return "; ".join(str(v) for v in value if str(v).strip())
    return "" if value is None else value
