"""Canonical JSON serialization + hashing — the single source of reproducible hashes.

Ticket: T2 (spec/00_build_plan.md §2; config snapshot + ``config_hash``).
Reused at: T20 for the IA-1.2 request-signature hash
(spec/07_IA1_implementation_invariants.md §1.2), so the config hash and the historical
cache key share ONE deterministic serializer. P3 resume-idempotency (P3 §13) depends on
a stable ``config_hash``; sorted keys + whitespace-stable separators guarantee it.

Pure functions: no IO, no clock, no randomness (CLAUDE.md §7).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(obj: Any) -> str:
    """Serialize ``obj`` to canonical JSON: keys sorted, no incidental whitespace.

    Structurally equal objects produce byte-identical output regardless of dict
    insertion order, so a hash over this string is reproducible. ``separators`` strips
    the spaces ``json.dumps`` inserts by default; ``ensure_ascii`` is left at its default
    (``True``) to match the IA-1.2 reference exactly so the T20 signature hash agrees.

    Contract: ``obj`` must be JSON-serializable with string-only dict keys (true for
    YAML-loaded config and the T20 request-signature object). Raises ``TypeError`` from
    ``json`` otherwise — surfaced, never swallowed.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def sha256_hex(text: str) -> str:
    """Return the hex SHA-256 of ``text`` encoded as UTF-8.

    Used for ``config_hash`` (T2) and, at T20, the request-signature and
    bookmaker-set hashes (IA-1.2).
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


__all__ = ["canonical_json", "sha256_hex"]
