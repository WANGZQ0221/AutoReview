"""Domain errors raised by the OPPO automation client."""


class OppoError(RuntimeError):
    """Base class for OPPO release automation failures."""


class OppoConfigError(OppoError):
    """Raised when the local submission config is invalid."""


class OppoApiError(OppoError):
    """Raised when the OPPO Open Platform API returns an error."""

    def __init__(self, message: str, *, status_code: int | None = None, payload=None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class OppoReviewTimeout(OppoError):
    """Raised when polling finishes before the app reaches a terminal state."""

