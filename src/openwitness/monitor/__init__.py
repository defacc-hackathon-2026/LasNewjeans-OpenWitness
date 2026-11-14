"""Monitor web de solo lectura (SPEC §17.5, G14). Dueño: Andres. La interfaz vive en el repo `monitoring-UI`."""
from .server import create_app
from .state import Monitor

__all__ = ["Monitor", "create_app"]
