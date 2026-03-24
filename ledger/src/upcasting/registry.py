from __future__ import annotations

from dataclasses import replace
from typing import Callable

from src.event_store import RecordedEvent


class UpcasterRegistry:
    """
    Centralised registry for event schema migrations.

    Upcasters are registered as (event_type, from_version) → fn.
    When an event is loaded, the chain is walked automatically:
      v1 → v2 → v3 ... until no further upcaster is registered.

    The stored payload is NEVER modified — upcasting is a pure read-time transform.
    This is the core immutability guarantee of event sourcing: the past is immutable,
    schema evolution happens at read time only.

    Design:
    - register() is a decorator factory — clean, composable, zero boilerplate.
    - upcast() returns a NEW RecordedEvent via dataclasses.replace(); the original
      is never mutated. Callers can safely hold references to both.
    - upcast_payload() is the low-level path used by EventStore._apply_upcasts
      so the registry integrates transparently into every load_stream() / load_all().

    Usage:
        registry = UpcasterRegistry()

        @registry.register("CreditAnalysisCompleted", from_version=1)
        def upcast_v1_to_v2(payload: dict) -> dict:
            return {**payload, "model_version": "legacy-pre-2026", "confidence_score": None}
    """

    def __init__(self) -> None:
        self._upcasters: dict[tuple[str, int], Callable[[dict], dict]] = {}

    def register(self, event_type: str, from_version: int) -> Callable:
        """
        Decorator. Registers fn as upcaster from event_type@from_version → from_version+1.

        Chaining: register v1→v2 and v2→v3 independently; upcast() walks the full chain.
        """
        def decorator(fn: Callable[[dict], dict]) -> Callable[[dict], dict]:
            self._upcasters[(event_type, from_version)] = fn
            return fn
        return decorator

    def upcast(self, event: RecordedEvent) -> RecordedEvent:
        """
        Apply all registered upcasters for this event type in version order.

        Returns a new RecordedEvent with the upcasted payload and final version.
        The original RecordedEvent is never touched — immutability guaranteed.

        If no upcaster is registered for this event, returns the original object
        unchanged (identity — no allocation).
        """
        current = event
        v = event.event_version
        while (event.event_type, v) in self._upcasters:
            new_payload = self._upcasters[(event.event_type, v)](current.payload)
            current = replace(current, payload=new_payload, event_version=v + 1)
            v += 1
        return current  # same object if no upcasters matched

    def upcast_payload(self, event_type: str, version: int, payload: dict) -> tuple[int, dict]:
        """
        Walk the upcast chain for (event_type, version).
        Returns (final_version, upcasted_payload).

        This is the integration point for EventStore._apply_upcasts — called on
        every row returned from load_stream() and load_all(). Callers never invoke
        upcasters manually; the store does it transparently.
        """
        v = version
        p = payload
        while (event_type, v) in self._upcasters:
            p = self._upcasters[(event_type, v)](p)
            v += 1
        return v, p

    def __contains__(self, key: tuple[str, int]) -> bool:
        """Support `(event_type, version) in registry` for introspection."""
        return key in self._upcasters

    def registered_types(self) -> list[tuple[str, int]]:
        """Return all registered (event_type, from_version) pairs — useful for diagnostics."""
        return sorted(self._upcasters.keys())
