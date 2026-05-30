"""Load, validate, hash, and snapshot ``config.yaml`` into an immutable resolved object.

Ticket: T2 (spec/00_build_plan.md §2). Config shape: spec/00_build_plan.md §4
(superset of spec/01_v0.1_base_spec.md §4).

Contract:
  - inputs: a YAML path + the process environment (injectable for tests).
  - output: a frozen :class:`Config` carrying every resolved value, plus ``config_hash``
    and a canonical, secret-free ``config_snapshot`` string.
  - failure modes (all typed, fail-closed, no DB writes — T3/T4 own persistence):
      * missing required key ......... :class:`MissingConfigKeyError`
      * wrong value type ............. :class:`InvalidConfigError`
      * ``api_key`` present in file ... :class:`ApiKeyInFileError` (key is env-only)
      * sport in both lists .......... :class:`SportOverlapError`
      * change a locked threshold .... :class:`ThresholdLockedError`

Scope (CLAUDE.md §6 / errata E3): this is the LIVE base loader. The IA-1.1 historical
boot checks and the E1/E2 historical config validations are added at T20, gated on
``run.mode == 'HISTORICAL'`` — they do not run here.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import yaml

from src.canonical import canonical_json, sha256_hex


class ConfigError(Exception):
    """Base class for all configuration failures."""


class MissingConfigKeyError(ConfigError):
    """A required configuration key is absent."""


class InvalidConfigError(ConfigError):
    """A configuration value has the wrong type or an out-of-contract value."""


class ApiKeyInFileError(ConfigError):
    """An ``api_key`` was found in the config file; it must live only in the env."""


class SportOverlapError(ConfigError):
    """A sport appears in both ``target.sport_keys`` and ``target.excluded_sports``."""


class ThresholdLockedError(ConfigError):
    """An attempt was made to change ``edge_threshold_pct`` while it is locked."""


# --- resolved config sections (all frozen => immutable => deterministic) ------------


@dataclass(frozen=True)
class RunConfig:
    run_label: str
    dry_run: bool
    threshold_locked: bool
    db_path: str


@dataclass(frozen=True)
class ApiConfig:
    base_url: str
    region: str
    request_timeout_s: int
    max_retries: int


@dataclass(frozen=True)
class TargetConfig:
    sport_keys: tuple[str, ...]
    market_key: str
    allowed_two_way_only: bool
    excluded_sports: tuple[str, ...]


@dataclass(frozen=True)
class SharpSourceConfig:
    sharp_book_primary: str
    sharp_book_fallback: str
    sharp_disagree_tolerance_prob: float


@dataclass(frozen=True)
class FreshnessWindow:
    sharp: int
    soft: int


@dataclass(frozen=True)
class ClosePollRule:
    from_min: int
    to_min: int
    interval_s: int


@dataclass(frozen=True)
class TimingConfig:
    pull_interval_seconds: int
    freshness_window_seconds: FreshnessWindow
    confirm_pull_delays_seconds: tuple[int, ...]
    confirm_worker_poll_seconds: int
    close_capture_window_minutes: int
    close_worker_poll_seconds: int
    close_polling_schedule: tuple[ClosePollRule, ...]


@dataclass(frozen=True)
class SignalConfig:
    edge_threshold_pct: float


@dataclass(frozen=True)
class SanityConfig:
    price_decimal_min: float
    price_decimal_max: float


@dataclass(frozen=True)
class TimeConfig:
    storage_timezone: str
    display_timezone: str


@dataclass(frozen=True)
class ReportingConfig:
    daily_run_time_local: str
    feasibility_min_graded_unique: int


@dataclass(frozen=True)
class DryrunConfig:
    scenario_path: str


@dataclass(frozen=True)
class Config:
    """Fully resolved, immutable configuration.

    ``config_snapshot`` is the canonical JSON of the (secret-free) file config and
    ``config_hash`` is its SHA-256; both are stored verbatim by T4 into ``audit_runs``.
    ``api_key`` comes from the environment and is deliberately excluded from the snapshot
    and hash.
    """

    run: RunConfig
    api: ApiConfig
    target: TargetConfig
    sharp_source: SharpSourceConfig
    soft_books: tuple[str, ...]
    timing: TimingConfig
    signal: SignalConfig
    sanity: SanityConfig
    time: TimeConfig
    reporting: ReportingConfig
    dryrun: DryrunConfig | None
    logging_level: str
    api_key: str | None
    config_hash: str
    config_snapshot: str

    def with_edge_threshold_pct(self, new_value: float) -> Config:
        """Return a new resolved config with a different ``edge_threshold_pct``.

        This is the *only* sanctioned path to change the threshold, and it refuses when
        ``run.threshold_locked`` is true (the live audit locks it for the whole window).
        On success it fully re-resolves so ``config_hash``/``config_snapshot`` stay
        consistent with the new value.
        """
        if self.run.threshold_locked:
            raise ThresholdLockedError(
                "edge_threshold_pct is locked (threshold_locked=true); "
                f"refusing mid-run change to {new_value}"
            )
        mapping = cast("dict[str, object]", json.loads(self.config_snapshot))
        new_signal = dict(_as_mapping(mapping["signal"], "signal"))
        new_signal["edge_threshold_pct"] = new_value
        new_mapping = dict(mapping)
        new_mapping["signal"] = new_signal
        return _build_config(new_mapping, self.api_key)


# --- typed coercion: bare values ----------------------------------------------------


def _as_mapping(value: object, where: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise InvalidConfigError(f"{where} must be a mapping, got {type(value).__name__}")
    for key in value:
        if not isinstance(key, str):
            raise InvalidConfigError(f"{where} has a non-string key: {key!r}")
    return cast("dict[str, object]", value)


def _as_str(value: object, where: str) -> str:
    if not isinstance(value, str):
        raise InvalidConfigError(f"{where} must be a string, got {type(value).__name__}")
    return value


def _as_bool(value: object, where: str) -> bool:
    if not isinstance(value, bool):
        raise InvalidConfigError(f"{where} must be a boolean, got {type(value).__name__}")
    return value


def _as_int(value: object, where: str) -> int:
    # bool is a subclass of int; reject it so `true` never silently becomes 1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidConfigError(f"{where} must be an integer, got {type(value).__name__}")
    return value


def _as_float(value: object, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidConfigError(f"{where} must be a number, got {type(value).__name__}")
    return float(value)


# --- typed coercion: require-then-coerce getters (DRY the parsers) ------------------


def _require(section: Mapping[str, object], key: str, where: str) -> object:
    if key not in section:
        raise MissingConfigKeyError(f"missing required key '{key}' in {where}")
    return section[key]


def _get_str(section: Mapping[str, object], key: str, where: str) -> str:
    return _as_str(_require(section, key, where), f"{where}.{key}")


def _get_bool(section: Mapping[str, object], key: str, where: str) -> bool:
    return _as_bool(_require(section, key, where), f"{where}.{key}")


def _get_int(section: Mapping[str, object], key: str, where: str) -> int:
    return _as_int(_require(section, key, where), f"{where}.{key}")


def _get_float(section: Mapping[str, object], key: str, where: str) -> float:
    return _as_float(_require(section, key, where), f"{where}.{key}")


def _get_str_tuple(section: Mapping[str, object], key: str, where: str) -> tuple[str, ...]:
    value = _require(section, key, where)
    label = f"{where}.{key}"
    if not isinstance(value, list):
        raise InvalidConfigError(f"{label} must be a list, got {type(value).__name__}")
    return tuple(_as_str(item, f"{label}[{i}]") for i, item in enumerate(value))


def _get_int_tuple(section: Mapping[str, object], key: str, where: str) -> tuple[int, ...]:
    value = _require(section, key, where)
    label = f"{where}.{key}"
    if not isinstance(value, list):
        raise InvalidConfigError(f"{label} must be a list, got {type(value).__name__}")
    return tuple(_as_int(item, f"{label}[{i}]") for i, item in enumerate(value))


def _section(file_cfg: Mapping[str, object], name: str) -> Mapping[str, object]:
    return _as_mapping(_require(file_cfg, name, "config"), name)


# --- section parsers ----------------------------------------------------------------


def _parse_run(m: Mapping[str, object]) -> RunConfig:
    return RunConfig(
        run_label=_get_str(m, "run_label", "run"),
        dry_run=_get_bool(m, "dry_run", "run"),
        threshold_locked=_get_bool(m, "threshold_locked", "run"),
        db_path=_get_str(m, "db_path", "run"),
    )


def _parse_api(m: Mapping[str, object]) -> ApiConfig:
    return ApiConfig(
        base_url=_get_str(m, "base_url", "api"),
        region=_get_str(m, "region", "api"),
        request_timeout_s=_get_int(m, "request_timeout_s", "api"),
        max_retries=_get_int(m, "max_retries", "api"),
    )


def _parse_target(m: Mapping[str, object]) -> TargetConfig:
    target = TargetConfig(
        sport_keys=_get_str_tuple(m, "sport_keys", "target"),
        market_key=_get_str(m, "market_key", "target"),
        allowed_two_way_only=_get_bool(m, "allowed_two_way_only", "target"),
        excluded_sports=_get_str_tuple(m, "excluded_sports", "target"),
    )
    overlap = sorted(set(target.sport_keys) & set(target.excluded_sports))
    if overlap:
        raise SportOverlapError(
            f"sport(s) in both target.sport_keys and target.excluded_sports: {overlap}"
        )
    return target


def _parse_sharp(m: Mapping[str, object]) -> SharpSourceConfig:
    return SharpSourceConfig(
        sharp_book_primary=_get_str(m, "sharp_book_primary", "sharp_source"),
        sharp_book_fallback=_get_str(m, "sharp_book_fallback", "sharp_source"),
        sharp_disagree_tolerance_prob=_get_float(
            m, "sharp_disagree_tolerance_prob", "sharp_source"
        ),
    )


def _parse_freshness(value: object) -> FreshnessWindow:
    m = _as_mapping(value, "timing.freshness_window_seconds")
    return FreshnessWindow(
        sharp=_get_int(m, "sharp", "timing.freshness_window_seconds"),
        soft=_get_int(m, "soft", "timing.freshness_window_seconds"),
    )


def _parse_close_schedule(value: object) -> tuple[ClosePollRule, ...]:
    where = "timing.close_polling_schedule"
    if not isinstance(value, list):
        raise InvalidConfigError(f"{where} must be a list, got {type(value).__name__}")
    rules: list[ClosePollRule] = []
    for i, item in enumerate(value):
        wi = f"{where}[{i}]"
        m = _as_mapping(item, wi)
        rules.append(
            ClosePollRule(
                from_min=_get_int(m, "from_min", wi),
                to_min=_get_int(m, "to_min", wi),
                interval_s=_get_int(m, "interval_s", wi),
            )
        )
    return tuple(rules)


def _parse_timing(m: Mapping[str, object]) -> TimingConfig:
    freshness = _require(m, "freshness_window_seconds", "timing")
    schedule = _require(m, "close_polling_schedule", "timing")
    return TimingConfig(
        pull_interval_seconds=_get_int(m, "pull_interval_seconds", "timing"),
        freshness_window_seconds=_parse_freshness(freshness),
        confirm_pull_delays_seconds=_get_int_tuple(m, "confirm_pull_delays_seconds", "timing"),
        confirm_worker_poll_seconds=_get_int(m, "confirm_worker_poll_seconds", "timing"),
        close_capture_window_minutes=_get_int(m, "close_capture_window_minutes", "timing"),
        close_worker_poll_seconds=_get_int(m, "close_worker_poll_seconds", "timing"),
        close_polling_schedule=_parse_close_schedule(schedule),
    )


def _parse_signal(m: Mapping[str, object]) -> SignalConfig:
    return SignalConfig(edge_threshold_pct=_get_float(m, "edge_threshold_pct", "signal"))


def _parse_sanity(m: Mapping[str, object]) -> SanityConfig:
    return SanityConfig(
        price_decimal_min=_get_float(m, "price_decimal_min", "sanity"),
        price_decimal_max=_get_float(m, "price_decimal_max", "sanity"),
    )


def _parse_time(m: Mapping[str, object]) -> TimeConfig:
    storage_tz = _get_str(m, "storage_timezone", "time")
    if storage_tz != "UTC":
        # base §4 / build-plan §10: all DB writes are UTC; a non-UTC store is a hard stop.
        raise InvalidConfigError(f"time.storage_timezone must be 'UTC', got {storage_tz!r}")
    return TimeConfig(
        storage_timezone=storage_tz,
        display_timezone=_get_str(m, "display_timezone", "time"),
    )


def _parse_reporting(m: Mapping[str, object]) -> ReportingConfig:
    return ReportingConfig(
        daily_run_time_local=_get_str(m, "daily_run_time_local", "reporting"),
        feasibility_min_graded_unique=_get_int(m, "feasibility_min_graded_unique", "reporting"),
    )


def _parse_dryrun(file_cfg: Mapping[str, object]) -> DryrunConfig | None:
    if "dryrun" not in file_cfg:  # optional: only the fixture path (T15) needs it
        return None
    m = _as_mapping(file_cfg["dryrun"], "dryrun")
    return DryrunConfig(scenario_path=_get_str(m, "scenario_path", "dryrun"))


# --- assembly + entry point ---------------------------------------------------------


def _build_config(file_cfg: Mapping[str, object], api_key: str | None) -> Config:
    """Validate + parse a (secret-free) config mapping into a resolved :class:`Config`.

    The snapshot/hash are computed over ``file_cfg`` exactly, so identical files always
    yield the same ``config_hash`` (P3 resume idempotency).
    """
    logging_section = _section(file_cfg, "logging")
    snapshot = canonical_json(file_cfg)
    return Config(
        run=_parse_run(_section(file_cfg, "run")),
        api=_parse_api(_section(file_cfg, "api")),
        target=_parse_target(_section(file_cfg, "target")),
        sharp_source=_parse_sharp(_section(file_cfg, "sharp_source")),
        soft_books=_get_str_tuple(file_cfg, "soft_books", "config"),
        timing=_parse_timing(_section(file_cfg, "timing")),
        signal=_parse_signal(_section(file_cfg, "signal")),
        sanity=_parse_sanity(_section(file_cfg, "sanity")),
        time=_parse_time(_section(file_cfg, "time")),
        reporting=_parse_reporting(_section(file_cfg, "reporting")),
        dryrun=_parse_dryrun(file_cfg),
        logging_level=_get_str(logging_section, "level", "logging"),
        api_key=api_key,
        config_hash=sha256_hex(snapshot),
        config_snapshot=snapshot,
    )


# Secrets are env-only. Primary defense: refuse any secret-like *key name* in the file
# (case/separator-insensitive, so api_key / apiKey / ODDS_API_KEY / client_secret all
# trip). Secondary defense: if the env key is set, refuse the literal secret *value*
# appearing anywhere in the file.
_SECRET_KEY_SUBSTRINGS = ("apikey", "secret", "token")
_MIN_SECRET_SCAN_LEN = 8


def _looks_like_secret_key(key: str) -> bool:
    normalized = key.lower().replace("_", "").replace("-", "")
    return any(sub in normalized for sub in _SECRET_KEY_SUBSTRINGS)


def _reject_secret_value(value: str, api_key: str | None) -> None:
    if api_key is not None and len(api_key) >= _MIN_SECRET_SCAN_LEN and api_key in value:
        raise ApiKeyInFileError(
            "the configured API key value appears in config.yaml; "
            "it must live only in env ODDS_API_KEY"
        )


def _reject_secrets_in_mapping(mapping: Mapping[str, object], api_key: str | None) -> None:
    for key, value in mapping.items():
        if isinstance(key, str) and _looks_like_secret_key(key):
            raise ApiKeyInFileError(
                f"secret-like key '{key}' must not appear in config.yaml; "
                "provide the API key via env ODDS_API_KEY"
            )
        _reject_secrets_in_file(value, api_key)


def _reject_secrets_in_file(obj: object, api_key: str | None) -> None:
    """Recursively refuse secret-like keys and the literal env secret value (env-only)."""
    if isinstance(obj, dict):
        _reject_secrets_in_mapping(obj, api_key)
    elif isinstance(obj, list):
        for item in obj:
            _reject_secrets_in_file(item, api_key)
    elif isinstance(obj, str):
        _reject_secret_value(obj, api_key)


def load_config(path: str | Path, *, env: Mapping[str, str] | None = None) -> Config:
    """Load and resolve ``config.yaml``.

    The API key is read only from ``env['ODDS_API_KEY']`` (defaulting to the process
    environment), never from the file. Presence of the key is not enforced here — the
    inventory/api-client tickets require it before any live pull; a dry-run/test context
    legitimately has none.
    """
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config file {p}: {exc}") from exc
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {p}: {exc}") from exc

    file_cfg = _as_mapping(loaded, "config root")
    environ = os.environ if env is None else env
    api_key = environ.get("ODDS_API_KEY")
    _reject_secrets_in_file(file_cfg, api_key)
    return _build_config(file_cfg, api_key)


__all__ = [
    "ApiConfig",
    "ApiKeyInFileError",
    "ClosePollRule",
    "Config",
    "ConfigError",
    "DryrunConfig",
    "FreshnessWindow",
    "InvalidConfigError",
    "MissingConfigKeyError",
    "ReportingConfig",
    "RunConfig",
    "SanityConfig",
    "SharpSourceConfig",
    "SignalConfig",
    "SportOverlapError",
    "TargetConfig",
    "ThresholdLockedError",
    "TimeConfig",
    "TimingConfig",
    "load_config",
]
