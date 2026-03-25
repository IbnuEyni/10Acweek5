# Canonical implementation lives in src/whatif/projector.py.
# This module exists so the spec path src/what_if/projector.py resolves.
from src.whatif.projector import (  # noqa: F401
    run_what_if,
    WhatIfResult,
    _SEMANTIC_DEPENDENTS,
    _build_causation_index,
    _find_dependent_event_ids,
    _make_synthetic_recorded,
    _apply_to_projections_in_memory,
    _extract_application_id,
    _read_projection_state,
    _compute_divergence,
)
