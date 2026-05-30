"""Tests for the shared canonical serializer (src/canonical.py).

Reproducible hashing is load-bearing: the same config must always hash the same
(P3 resume idempotency), and at T20 the same request must always hash the same
(IA-1.2 cache key). These tests pin sorted-key, whitespace-stable, order-independent
behavior and the exact SHA-256.
"""

from __future__ import annotations

import hashlib

from src.canonical import canonical_json, sha256_hex


def test_keys_are_sorted_and_whitespace_stripped() -> None:
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'


def test_order_independent_for_equal_objects() -> None:
    a = {"x": 1, "y": {"q": 2, "p": 3}, "z": [1, 2, 3]}
    b = {"z": [1, 2, 3], "y": {"p": 3, "q": 2}, "x": 1}
    assert canonical_json(a) == canonical_json(b)
    assert sha256_hex(canonical_json(a)) == sha256_hex(canonical_json(b))


def test_list_order_is_preserved() -> None:
    """Dict key order is normalized; list order is meaningful and must be kept."""
    assert canonical_json([3, 1, 2]) == "[3,1,2]"
    assert canonical_json([1, 2, 3]) != canonical_json([3, 2, 1])


def test_non_ascii_is_escaped_matching_ia1_2_reference() -> None:
    """ensure_ascii defaults to True (matches the IA-1.2 json.dumps reference) so the
    T20 signature hash will agree byte-for-byte."""
    assert canonical_json({"k": "é"}) == '{"k":"\\u00e9"}'


def test_sha256_hex_matches_hashlib_and_is_64_hex() -> None:
    text = '{"a":1}'
    digest = sha256_hex(text)
    assert digest == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)
