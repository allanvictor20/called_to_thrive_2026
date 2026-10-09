"""Domain errors with the status codes defined in the SDD service contracts.

422 malformed input, 403 forbidden scope, 404 concealed, 409 stale revision,
429 throttled. Messages are safe for display: never SQL or secrets.
"""


class DomainError(Exception):
    status = 400
    code = "error"

    def __init__(self, message: str, fields: dict | None = None, code: str | None = None):
        super().__init__(message)
        self.message = message
        self.fields = fields or {}
        if code:
            self.code = code

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "fields": self.fields}


class ValidationFailed(DomainError):
    status = 422
    code = "validation_failed"


class Forbidden(DomainError):
    status = 403
    code = "forbidden"


class NotFound(DomainError):
    status = 404
    code = "not_found"


class Conflict(DomainError):
    status = 409
    code = "conflict"


class Throttled(DomainError):
    status = 429
    code = "throttled"
