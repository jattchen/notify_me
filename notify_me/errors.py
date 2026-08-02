"""Errors with deliberately stable, secret-free public classifications."""


class NotifyMeError(Exception):
    """An expected failure that is safe to expose to the CLI caller."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.safe_message = message
