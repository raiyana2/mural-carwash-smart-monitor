"""The logic layer. The web dashboard only needs MonitorController and ControllerError."""

from carwash.controllers.errors import ControllerError
from carwash.controllers.monitor import MonitorController

__all__ = ["ControllerError", "MonitorController"]
