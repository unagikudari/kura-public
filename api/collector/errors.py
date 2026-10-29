"""Shared application errors for collector adapters."""


class Conflict(ValueError):
    pass


class NotFound(ValueError):
    pass


class InvalidPath(ValueError):
    pass
