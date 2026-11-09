"""The one exception the controllers raise when they refuse something."""


class ControllerError(Exception):
    """An action was refused. str(error) is a message for the user; `kind` says why:
    not_found, invalid (bad input), conflict (not allowed right now), unavailable (hardware)."""

    def __init__(self, message: str, kind: str = "conflict"):
        super().__init__(message)
        self.kind = kind
