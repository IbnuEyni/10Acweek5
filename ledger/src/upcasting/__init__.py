from src.upcasting.registry import UpcasterRegistry
from src.upcasting import upcasters  # noqa: F401 — registers upcasters on import

__all__ = ["UpcasterRegistry"]
