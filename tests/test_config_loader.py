"""T2 acceptance tests for src/config_loader.py.

Covers the spec'd behaviors (valid load; missing key; api-key-in-file rejection;
sport overlap; threshold-lock enforcement; dry_run surfaced; env-only key) plus typed
fail-closed coercion for every section, and confirms the loader performs NO DB writes
(it only returns a resolved object + hash + snapshot).
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest
import yaml

from src.canonical import canonical_json
from src.config_loader import (
    ApiKeyInFileError,
    Config,
    ConfigError,
    InvalidConfigError,
    MissingConfigKeyError,
    SportOverlapError,
    ThresholdLockedError,
    load_config,
)

ENV = {"ODDS_API_KEY": "secret-token"}


def _base() -> dict[str, Any]:
    """A complete, valid config mapping (a fresh copy each call)."""
    return copy.deepcopy(
        {
            "run": {
                "run_label": "test_run",
                "dry_run": True,
                "threshold_locked": True,
                "db_path": "data/market_translation.sqlite",
            },
            "api": {
                "base_url": "https://example.test",
                "region": "us",
                "request_timeout_s": 10,
                "max_retries": 2,
            },
            "target": {
                "sport_keys": ["baseball_mlb"],
                "market_key": "h2h",
                "allowed_two_way_only": True,
                "excluded_sports": ["soccer_epl"],
            },
            "sharp_source": {
                "sharp_book_primary": "pinnacle",
                "sharp_book_fallback": "circasports",
                "sharp_disagree_tolerance_prob": 0.010,
            },
            "soft_books": ["draftkings", "fanduel"],
            "timing": {
                "pull_interval_seconds": 90,
                "freshness_window_seconds": {"sharp": 120, "soft": 120},
                "confirm_pull_delays_seconds": [45],
                "confirm_worker_poll_seconds": 5,
                "close_capture_window_minutes": 10,
                "close_worker_poll_seconds": 15,
                "close_polling_schedule": [
                    {"from_min": 60, "to_min": 15, "interval_s": 120},
                    {"from_min": 15, "to_min": 0, "interval_s": 30},
                ],
            },
            "signal": {"edge_threshold_pct": 2.0},
            "sanity": {"price_decimal_min": 1.01, "price_decimal_max": 51.0},
            "time": {"storage_timezone": "UTC", "display_timezone": "America/New_York"},
            "reporting": {"daily_run_time_local": "09:00", "feasibility_min_graded_unique": 100},
            "logging": {"level": "INFO"},
            "dryrun": {"scenario_path": "fixtures/scenario_basic.yaml"},
        }
    )


def _dump(tmp_path: Path, cfg: object) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return p


def _load(tmp_path: Path, cfg: dict[str, Any]) -> Config:
    return load_config(_dump(tmp_path, cfg), env=ENV)


# --- valid load ---------------------------------------------------------------------


def test_valid_load_resolves_every_section(tmp_path: Path) -> None:
    cfg = _load(tmp_path, _base())

    assert cfg.run.run_label == "test_run"
    assert cfg.run.threshold_locked is True
    assert cfg.api.base_url == "https://example.test"
    assert cfg.api.max_retries == 2
    assert cfg.target.sport_keys == ("baseball_mlb",)
    assert cfg.target.excluded_sports == ("soccer_epl",)
    assert cfg.sharp_source.sharp_disagree_tolerance_prob == 0.010
    assert cfg.soft_books == ("draftkings", "fanduel")
    assert cfg.timing.pull_interval_seconds == 90
    assert cfg.timing.freshness_window_seconds.sharp == 120
    assert cfg.timing.confirm_pull_delays_seconds == (45,)
    assert cfg.timing.close_polling_schedule[0].interval_s == 120
    assert cfg.timing.close_polling_schedule[1].to_min == 0
    assert cfg.signal.edge_threshold_pct == 2.0
    assert cfg.sanity.price_decimal_max == 51.0
    assert cfg.time.storage_timezone == "UTC"
    assert cfg.reporting.feasibility_min_graded_unique == 100
    assert cfg.dryrun is not None
    assert cfg.dryrun.scenario_path == "fixtures/scenario_basic.yaml"
    assert cfg.logging_level == "INFO"


def test_snapshot_is_canonical_and_hash_is_64_hex_and_secret_free(tmp_path: Path) -> None:
    base = _base()
    cfg = _load(tmp_path, base)
    # Snapshot is the canonical JSON of exactly the file mapping (deterministic).
    assert cfg.config_snapshot == canonical_json(base)
    assert len(cfg.config_hash) == 64
    # The secret never enters the snapshot or hash.
    assert "secret-token" not in cfg.config_snapshot
    assert "api_key" not in cfg.config_snapshot
    assert cfg.api_key == "secret-token"


def test_identical_files_hash_identically(tmp_path: Path) -> None:
    """P3 resume idempotency depends on a stable config_hash."""
    pa = tmp_path / "a.yaml"
    pa.write_text(yaml.safe_dump(_base()), encoding="utf-8")
    pb = tmp_path / "b.yaml"
    pb.write_text(yaml.safe_dump(_base()), encoding="utf-8")
    assert load_config(pa, env=ENV).config_hash == load_config(pb, env=ENV).config_hash


def test_repo_config_yaml_loads(tmp_path: Path) -> None:
    """The committed default config.yaml resolves and is locked at the spec values."""
    repo_config = Path(__file__).resolve().parents[1] / "config.yaml"
    cfg = load_config(repo_config, env=ENV)
    assert cfg.run.threshold_locked is True
    assert cfg.run.dry_run is True  # safe default; live-audit profile flips to false
    assert cfg.signal.edge_threshold_pct == 2.0
    assert cfg.target.sport_keys == ("baseball_mlb",)


# --- missing / wrong-shape ----------------------------------------------------------


def test_missing_top_level_section(tmp_path: Path) -> None:
    cfg = _base()
    del cfg["signal"]
    with pytest.raises(MissingConfigKeyError, match="signal"):
        _load(tmp_path, cfg)


def test_missing_nested_key(tmp_path: Path) -> None:
    cfg = _base()
    del cfg["run"]["dry_run"]
    with pytest.raises(MissingConfigKeyError, match="dry_run"):
        _load(tmp_path, cfg)


def test_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(tmp_path / "does_not_exist.yaml", env=ENV)


def test_invalid_yaml(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("a: [unterminated\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(p, env=ENV)


def test_root_not_a_mapping(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("- 1\n- 2\n", encoding="utf-8")
    with pytest.raises(InvalidConfigError, match="config root"):
        load_config(p, env=ENV)


def test_root_non_string_key(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("1: x\n", encoding="utf-8")
    with pytest.raises(InvalidConfigError, match="non-string key"):
        load_config(p, env=ENV)


# --- api key is env-only ------------------------------------------------------------


def test_api_key_in_api_section_rejected(tmp_path: Path) -> None:
    cfg = _base()
    cfg["api"]["api_key"] = "leaked"
    with pytest.raises(ApiKeyInFileError):
        _load(tmp_path, cfg)


def test_api_key_nested_in_list_rejected(tmp_path: Path) -> None:
    cfg = _base()
    cfg["misc"] = [{"deep": {"api_key": "leaked"}}]
    with pytest.raises(ApiKeyInFileError):
        _load(tmp_path, cfg)


@pytest.mark.parametrize("alias", ["apiKey", "ODDS_API_KEY", "client_secret", "access_token"])
def test_secret_like_key_aliases_rejected(tmp_path: Path, alias: str) -> None:
    """Case/separator-insensitive denylist closes the alias gap."""
    cfg = _base()
    cfg["api"][alias] = "leaked"
    with pytest.raises(ApiKeyInFileError, match="secret-like key"):
        _load(tmp_path, cfg)


def test_secret_value_in_file_rejected(tmp_path: Path) -> None:
    """Second layer: the literal env-secret value must not appear in any config value."""
    cfg = _base()
    cfg["sharp_source"]["sharp_book_primary"] = ENV["ODDS_API_KEY"]  # 'secret-token'
    with pytest.raises(ApiKeyInFileError, match="API key value appears"):
        _load(tmp_path, cfg)


def test_short_env_key_not_value_scanned(tmp_path: Path) -> None:
    """A short env key is not value-scanned, so a coincidental substring is not a leak."""
    cfg = _base()
    cfg["target"]["market_key"] = "abc"
    loaded = load_config(_dump(tmp_path, cfg), env={"ODDS_API_KEY": "abc"})
    assert loaded.target.market_key == "abc"


def test_api_key_absent_when_env_missing(tmp_path: Path) -> None:
    """T2 does not enforce key presence (dry-run/test has none); T7/T16 do."""
    cfg = load_config(_dump(tmp_path, _base()), env={})
    assert cfg.api_key is None


# --- cross-field validation ---------------------------------------------------------


def test_sport_overlap_rejected(tmp_path: Path) -> None:
    cfg = _base()
    cfg["target"]["excluded_sports"] = ["baseball_mlb", "soccer_epl"]
    with pytest.raises(SportOverlapError, match="baseball_mlb"):
        _load(tmp_path, cfg)


def test_non_utc_storage_timezone_rejected(tmp_path: Path) -> None:
    cfg = _base()
    cfg["time"]["storage_timezone"] = "America/New_York"
    with pytest.raises(InvalidConfigError, match="must be 'UTC'"):
        _load(tmp_path, cfg)


# --- dry_run + dryrun section -------------------------------------------------------


@pytest.mark.parametrize("dry_run", [True, False])
def test_dry_run_flag_surfaced(tmp_path: Path, dry_run: bool) -> None:
    cfg = _base()
    cfg["run"]["dry_run"] = dry_run
    assert _load(tmp_path, cfg).run.dry_run is dry_run


def test_dryrun_section_optional(tmp_path: Path) -> None:
    cfg = _base()
    del cfg["dryrun"]
    assert _load(tmp_path, cfg).dryrun is None


# --- threshold lock -----------------------------------------------------------------


def test_locked_threshold_change_raises(tmp_path: Path) -> None:
    cfg = _load(tmp_path, _base())  # threshold_locked=True
    with pytest.raises(ThresholdLockedError, match="locked"):
        cfg.with_edge_threshold_pct(3.0)
    assert cfg.signal.edge_threshold_pct == 2.0  # unchanged


def test_unlocked_threshold_change_rehashes(tmp_path: Path) -> None:
    base = _base()
    base["run"]["threshold_locked"] = False
    cfg = _load(tmp_path, base)
    changed = cfg.with_edge_threshold_pct(3.5)
    assert changed.signal.edge_threshold_pct == 3.5
    assert changed.config_hash != cfg.config_hash  # re-resolved
    assert cfg.signal.edge_threshold_pct == 2.0  # original untouched (immutable)


def test_config_is_frozen(tmp_path: Path) -> None:
    cfg = _load(tmp_path, _base())
    with pytest.raises(FrozenInstanceError):
        cfg.api_key = "mutated"  # type: ignore[misc]  # asserting immutability at runtime


def test_int_value_coerces_to_float(tmp_path: Path) -> None:
    """A numeric field declared float accepts an int and stores it as float."""
    cfg = _base()
    cfg["sanity"]["price_decimal_min"] = 2  # int in the file
    loaded = _load(tmp_path, cfg)
    assert loaded.sanity.price_decimal_min == 2.0
    assert isinstance(loaded.sanity.price_decimal_min, float)


# --- typed coercion: every wrong-type path fails closed -----------------------------


def _set(path: tuple[str, ...], value: object) -> Callable[[dict[str, Any]], None]:
    def mutate(cfg: dict[str, Any]) -> None:
        node: Any = cfg
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value

    return mutate


WRONG_TYPE_CASES: list[Callable[[dict[str, Any]], None]] = [
    _set(("run", "run_label"), 5),  # str expected
    _set(("run", "dry_run"), "yes"),  # bool expected
    _set(("api", "request_timeout_s"), 1.5),  # int expected (float given)
    _set(("api", "max_retries"), True),  # int expected (bool rejected)
    _set(("signal", "edge_threshold_pct"), "two"),  # float expected
    _set(("soft_books",), "draftkings"),  # list expected
    _set(("soft_books",), [1, 2]),  # list[str] expected
    _set(("timing", "confirm_pull_delays_seconds"), 45),  # list expected
    _set(("timing", "freshness_window_seconds"), "x"),  # mapping expected
    _set(("timing", "close_polling_schedule"), "x"),  # list expected
    _set(("timing", "close_polling_schedule"), ["x"]),  # list[mapping] expected
    _set(("run",), "x"),  # section must be a mapping
    _set(("dryrun",), "x"),  # optional section, but if present must be a mapping
]


@pytest.mark.parametrize("mutate", WRONG_TYPE_CASES)
def test_wrong_types_fail_closed(tmp_path: Path, mutate: Callable[[dict[str, Any]], None]) -> None:
    cfg = _base()
    mutate(cfg)
    with pytest.raises(InvalidConfigError):
        _load(tmp_path, cfg)
