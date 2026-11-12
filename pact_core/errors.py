from __future__ import annotations


class PactError(Exception):
    """Domain error with a stable machine-readable code."""

    status_code = 400

    def __init__(self, code: str, message: str, *, status_code: int | None = None, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        if status_code is not None:
            self.status_code = status_code


class NotFound(PactError):
    status_code = 404


class Forbidden(PactError):
    status_code = 403


class Conflict(PactError):
    status_code = 409


class InvalidTransition(Conflict):
    pass
