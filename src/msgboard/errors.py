class ApiError(Exception):
    """An error with an HTTP status and a stable machine-readable code."""

    def __init__(self, status: int, code: str, message: str, headers: dict | None = None, details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers or {}
        self.details = details


def not_found(what: str = "not found") -> ApiError:
    # Used both for "missing" and "exists but you may not see it", so callers
    # cannot probe for channels or agents outside their grants.
    return ApiError(404, "not_found", what)


def forbidden(message: str = "not allowed") -> ApiError:
    return ApiError(403, "forbidden", message)


def bad_request(message: str) -> ApiError:
    return ApiError(400, "invalid_request", message)


def conflict(code: str, message: str) -> ApiError:
    return ApiError(409, code, message)


def unauthorized(message: str = "invalid or missing token") -> ApiError:
    return ApiError(401, "unauthorized", message, {"WWW-Authenticate": "Bearer"})
