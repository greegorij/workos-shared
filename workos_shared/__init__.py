"""workos_shared — cross-repo helpers for the Jarvis ecosystem (WorkOS E3)."""

from workos_shared.logger import get_logger
from workos_shared.openrouter import OpenRouterClient, OpenRouterError

__all__ = ["get_logger", "OpenRouterClient", "OpenRouterError"]
__version__ = "0.2.0"
