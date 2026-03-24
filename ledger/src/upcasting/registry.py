from __future__ import annotations

from typing import Callable

from src.event_store import RecordedEvent


class UpcasterRegistry:
    """
    Centralised registry for event schema migrations.

    Upcasters are registered as (event_type, from_version) → fn.
    When an event is loaded, the chain is walked automatically:
      v1 → v2 → v3 ... until no further upcaster is registered.

    The stored payload is NEVER modified — upcasting is a read-time transform.
    This is the core immutability guarantee of event sourcing.

    Usage:
        registry = UpcasterRegistry()

        @registry.register("CreditAnalysisCompleted", from_version=1)
        def upcast_v1_to_v2(payload: dict) -> dict:
            return {**payload, "model_version": "legacy-pre-2026"}
    """

    def __init__(self) -> None:
        self._upcasters: dict[tuple[str, int], Callable[[dict], dict]] = {}

    def register(self, event_type: str, from_version: int) -> Callable:
        """Decorator. Registers fn as upcaster from event_type@from_version → from_version+1."""
        def decorator(fn: Callable[[dict], dict]) -> Callable[[dict], dict]:
            self._upcasters[(event_type, from_version)] = fn
            return fn
        return decorator

    def upcast(self, event: RecordedEvent) -> RecordedEvent:
        """
        Apply all registered upcasters for this event type in version order.
        Returns a new RecordedEvent with the upcasted payload and incremented version.
        The original stored event is never touched.
        """
        current_payload = event.payload
        v = event.event_version
        while (event.event_type, v) in self._upcasters:
            current_payload = self._upcasters[(event.event_type, v)](current_payload)
            v += 1
        if v == event.event_version:
            return event
        # Return a new RecordedEvent with updated payload and version
        from dataclasses import replace
        return replace(event, payload=current_payload, event_version=v)

    def upcast_payload(self, event_type: str, version: int, payload: dict) -> tuple[int, dict]:
        """
        Walk the upcast chain for (event_type, version).
        Returns (final_version, upcasted_payload).
        Used by EventStore._apply_upcasts internally.
        """
        v = version
        p = payload
        while (event_type, v) in self._upcasters:
            p = self._upcasters[(event_type, v)](p)
            v += 1
        return v, p
