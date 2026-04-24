"""workos_shared — cross-repo helpers for the Jarvis ecosystem (WorkOS E3 + E16)."""

from workos_shared.anthropic_client import (
    AnthropicClientError,
    CallResult,
    call_claude,
    call_claude_async,
    detect_long_prompt,
    parse_json_response,
)
from workos_shared.logger import get_logger
from workos_shared.openrouter import OpenRouterClient, OpenRouterError
from workos_shared.webhook import (
    PersistentDedup,
    SignatureMismatch,
    verify_hmac_signature,
)

__all__ = [
    # Logging (E3 S2)
    "get_logger",
    # OpenRouter (E2 S6)
    "OpenRouterClient",
    "OpenRouterError",
    # Anthropic client (E16 S2)
    "AnthropicClientError",
    "CallResult",
    "call_claude",
    "call_claude_async",
    "detect_long_prompt",
    "parse_json_response",
    # Webhook (E16 S2)
    "PersistentDedup",
    "SignatureMismatch",
    "verify_hmac_signature",
]
__version__ = "0.3.0"
