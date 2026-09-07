"""Shared service-layer exception."""


class ServiceError(Exception):
    """A business-rule violation. `status_code` is an HTTP hint for the API layer."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
