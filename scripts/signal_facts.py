#!/usr/bin/env python3
"""Durable signal-fact ledger and bounded analysis bundles.

The ledger is private data under $OV. This helper is provider-neutral,
stdlib-only, and deterministic. It never fetches the network. Source-specific
adapters produce declarative JSON records and pass them to ``ingest``.
"""

from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import urlparse

from _paths import fmt, parse_iso_date, vault_root


SCHEMA_VERSION = 1
DEFAULT_CONFIG_RELATIVE = "_meta/signal_facts.toml"
DEFAULT_MAX_BYTES = 96 * 1024

PERIOD_BASES = frozenset({"quarter", "ytd", "fy", "ttm", "point_in_time"})
OBSERVATION_KINDS = frozenset({"reported"})
VERIFICATION_STATES = frozenset(
    {"primary-deterministic", "primary-extracted", "candidate"}
)
PRIMARY_VERIFICATIONS = frozenset({"primary-deterministic", "primary-extracted"})
SOURCE_TYPES = frozenset(
    {"regulatory-filing", "investor-relations", "primary-other", "secondary"}
)
PRIMARY_SOURCE_TYPES = frozenset(
    {"regulatory-filing", "investor-relations", "primary-other"}
)
ENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}$")

FORMULA_CANONICAL_FCF = "canonical_cash_capex_fcf_v1"
FORMULA_LEASE_ADJUSTED_FCF = "lease_adjusted_fcf_v1"

OCF_KEY = ("operating_cash_flow", "gaap_operating_cash_flow")
CAPEX_GROSS_KEY = ("cash_ppe_purchases", "gross_cash_ppe_purchases")
CAPEX_NET_KEY = ("cash_ppe_purchases_net", "net_cash_ppe_purchases_v1")
CAPEX_OFFSET_KEY = (
    "cash_ppe_proceeds_and_incentives",
    "explicit_cash_offsets",
)
LEASE_PRINCIPAL_KEY = (
    "finance_lease_principal",
    "cash_finance_lease_principal",
)
POSITIVE_MAGNITUDE_KEYS = frozenset(
    {
        CAPEX_GROSS_KEY,
        CAPEX_NET_KEY,
        CAPEX_OFFSET_KEY,
        LEASE_PRINCIPAL_KEY,
    }
)
CANONICAL_FCF_KEY = ("free_cash_flow", FORMULA_CANONICAL_FCF)
LEASE_ADJUSTED_FCF_KEY = ("free_cash_flow", FORMULA_LEASE_ADJUSTED_FCF)

VERIFICATION_RANK = {
    "primary-deterministic": 0,
    "primary-extracted": 1,
    "candidate": 2,
}


class SignalFactsError(ValueError):
    """User-facing input, configuration, or ledger error."""


@dataclass(frozen=True, slots=True)
class SignalSpec:
    signal_id: str
    kind: str
    metric_id: str
    definition_id: str
    period_basis: str
    threshold: Decimal
    required_count: int
    scope: str = "consolidated"


@dataclass(frozen=True, slots=True)
class MetricSpec:
    metric_id: str
    definition_id: str
    period_basis: str
    scope: str
    required: bool
    entities: tuple[str, ...]

    @property
    def key(self) -> tuple[str, str]:
        return (self.metric_id, self.definition_id)

    def applies_to(self, entity_id: str) -> bool:
        return not self.entities or entity_id in self.entities


@dataclass(frozen=True, slots=True)
class Profile:
    name: str
    entities: tuple[str, ...]
    latest_periods: int
    max_age_days: int
    metrics: tuple[MetricSpec, ...]
    signals: tuple[SignalSpec, ...]


@dataclass(frozen=True, slots=True)
class Config:
    path: Path
    ledger_dir: Path
    cache_dir: Path
    profiles: dict[str, Profile]


@dataclass(frozen=True, slots=True)
class LedgerLoad:
    records: tuple[dict[str, Any], ...]
    invalid: tuple[dict[str, str], ...]


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def parse_date(value: Any, field: str) -> date:
    if not isinstance(value, str):
        raise SignalFactsError(f"{field} must be an ISO date string")
    parsed = parse_iso_date(value) if len(value.strip()) == 10 else None
    if parsed is None:
        raise SignalFactsError(f"{field} is not a valid ISO date: {value!r}")
    return parsed


def parse_datetime(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise SignalFactsError(f"{field} must be an RFC3339 timestamp")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise SignalFactsError(f"{field} is not a valid timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise SignalFactsError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def rfc3339(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_as_of(value: str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        parsed_date = parse_date(value, "--as-of")
        return datetime.combine(parsed_date, time.max, timezone.utc)
    return parse_datetime(value, "--as-of")


def require_string(
    mapping: dict[str, Any],
    field: str,
    *,
    identifier: bool = False,
) -> str:
    value = mapping.get(field)
    if not isinstance(value, str) or not value.strip():
        raise SignalFactsError(f"{field} must be a non-empty string")
    value = value.strip()
    if identifier and not IDENTIFIER_RE.fullmatch(value):
        raise SignalFactsError(f"{field} contains unsupported characters: {value!r}")
    return value


def require_mapping(mapping: dict[str, Any], field: str) -> dict[str, Any]:
    value = mapping.get(field)
    if not isinstance(value, dict):
        raise SignalFactsError(f"{field} must be an object")
    return value


def require_list(mapping: dict[str, Any], field: str) -> list[Any]:
    value = mapping.get(field)
    if not isinstance(value, list):
        raise SignalFactsError(f"{field} must be a list")
    return value


def decimal_value(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise SignalFactsError(f"{field} must be numeric")
    try:
        parsed = Decimal(str(value))
    except Exception as exc:
        raise SignalFactsError(f"{field} must be numeric") from exc
    if not parsed.is_finite():
        raise SignalFactsError(f"{field} must be finite")
    return parsed


def json_number(value: Decimal) -> int | float:
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def resolve_under_vault(root: Path, raw: Any, field: str) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise SignalFactsError(f"{field} must be a non-empty vault-relative path")
    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        raise SignalFactsError(f"{field} must be relative to $OV")
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise SignalFactsError(f"{field} escapes $OV: {raw!r}") from exc
    return resolved


def load_config(path: Path | None = None) -> Config:
    root = vault_root()
    config_path = path or root / DEFAULT_CONFIG_RELATIVE
    config_path = config_path.expanduser().resolve()
    try:
        config_path.relative_to(root)
    except ValueError as exc:
        raise SignalFactsError("signal-facts config must live under $OV") from exc
    try:
        with config_path.open("rb") as handle:
            raw = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise SignalFactsError(f"config missing: {fmt(config_path)}") from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SignalFactsError(f"cannot read config {fmt(config_path)}: {exc}") from exc

    if raw.get("schema_version") != SCHEMA_VERSION:
        raise SignalFactsError(
            f"{fmt(config_path)} schema_version must be {SCHEMA_VERSION}"
        )
    ledger_dir = resolve_under_vault(root, raw.get("ledger_dir"), "ledger_dir")
    cache_dir = resolve_under_vault(root, raw.get("cache_dir"), "cache_dir")
    raw_profiles = raw.get("profiles")
    if not isinstance(raw_profiles, dict) or not raw_profiles:
        raise SignalFactsError("config must declare at least one [profiles.<name>]")

    profiles: dict[str, Profile] = {}
    for name, row in raw_profiles.items():
        if not isinstance(name, str) or not ENTITY_RE.fullmatch(name):
            raise SignalFactsError(f"invalid profile name: {name!r}")
        if not isinstance(row, dict):
            raise SignalFactsError(f"profiles.{name} must be a table")
        entities_raw = row.get("entities")
        if not isinstance(entities_raw, list) or not entities_raw:
            raise SignalFactsError(f"profiles.{name}.entities must be non-empty")
        entities: list[str] = []
        for entity in entities_raw:
            if not isinstance(entity, str) or not ENTITY_RE.fullmatch(entity):
                raise SignalFactsError(
                    f"profiles.{name}.entities contains invalid id: {entity!r}"
                )
            if entity not in entities:
                entities.append(entity)

        latest_periods = row.get("latest_periods", 2)
        max_age_days = row.get("max_age_days", 125)
        if (
            isinstance(latest_periods, bool)
            or not isinstance(latest_periods, int)
            or not 1 <= latest_periods <= 8
        ):
            raise SignalFactsError(
                f"profiles.{name}.latest_periods must be an integer from 1 to 8"
            )
        if (
            isinstance(max_age_days, bool)
            or not isinstance(max_age_days, int)
            or not 1 <= max_age_days <= 730
        ):
            raise SignalFactsError(
                f"profiles.{name}.max_age_days must be an integer from 1 to 730"
            )

        metrics: list[MetricSpec] = []
        metrics_raw = row.get("metrics", [])
        if not isinstance(metrics_raw, list):
            raise SignalFactsError(f"profiles.{name}.metrics must be a list")
        for index, metric in enumerate(metrics_raw):
            prefix = f"profiles.{name}.metrics[{index}]"
            if not isinstance(metric, dict):
                raise SignalFactsError(f"{prefix} must be a table")
            period_basis = metric.get("period_basis", "quarter")
            if not isinstance(period_basis, str) or period_basis not in PERIOD_BASES:
                raise SignalFactsError(
                    f"{prefix}.period_basis unsupported: {period_basis!r}"
                )
            required = metric.get("required", False)
            if not isinstance(required, bool):
                raise SignalFactsError(f"{prefix}.required must be a boolean")
            metric_entities_raw = metric.get("entities", [])
            if not isinstance(metric_entities_raw, list) or not all(
                isinstance(entity, str) and entity in entities
                for entity in metric_entities_raw
            ):
                raise SignalFactsError(
                    f"{prefix}.entities must contain configured entity ids"
                )
            metrics.append(
                MetricSpec(
                    metric_id=require_string(metric, "metric_id", identifier=True),
                    definition_id=require_string(
                        metric, "definition_id", identifier=True
                    ),
                    period_basis=period_basis,
                    scope=require_string(metric, "scope")
                    if "scope" in metric
                    else "consolidated",
                    required=required,
                    entities=tuple(dict.fromkeys(metric_entities_raw)),
                )
            )

        signals: list[SignalSpec] = []
        signals_raw = row.get("signals", [])
        if not isinstance(signals_raw, list):
            raise SignalFactsError(f"profiles.{name}.signals must be a list")
        for index, signal in enumerate(signals_raw):
            prefix = f"profiles.{name}.signals[{index}]"
            if not isinstance(signal, dict):
                raise SignalFactsError(f"{prefix} must be a table")
            kind = require_string(signal, "kind", identifier=True)
            if kind != "distinct_entities_below":
                raise SignalFactsError(
                    f"{prefix}.kind unsupported: {kind!r}; "
                    "expected distinct_entities_below"
                )
            period_basis = require_string(signal, "period_basis", identifier=True)
            if period_basis not in PERIOD_BASES:
                raise SignalFactsError(
                    f"{prefix}.period_basis unsupported: {period_basis!r}"
                )
            required_count = signal.get("required_count")
            if (
                isinstance(required_count, bool)
                or not isinstance(required_count, int)
                or not 1 <= required_count <= len(entities)
            ):
                raise SignalFactsError(
                    f"{prefix}.required_count must be from 1 to entity count"
                )
            signals.append(
                SignalSpec(
                    signal_id=require_string(signal, "signal_id", identifier=True),
                    kind=kind,
                    metric_id=require_string(signal, "metric_id", identifier=True),
                    definition_id=require_string(
                        signal, "definition_id", identifier=True
                    ),
                    period_basis=period_basis,
                    threshold=decimal_value(signal.get("threshold"), "threshold"),
                    required_count=required_count,
                    scope=require_string(signal, "scope")
                    if "scope" in signal
                    else "consolidated",
                )
            )

        profiles[name] = Profile(
            name=name,
            entities=tuple(entities),
            latest_periods=latest_periods,
            max_age_days=max_age_days,
            metrics=tuple(metrics),
            signals=tuple(signals),
        )

    return Config(
        path=config_path,
        ledger_dir=ledger_dir,
        cache_dir=cache_dir,
        profiles=profiles,
    )


def validate_url(value: str, field: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SignalFactsError(f"{field} must be an http(s) URL")


def normalized_observation(
    raw: Any,
    *,
    index: int,
    period_end: date,
) -> dict[str, Any]:
    prefix = f"observations[{index}]"
    if not isinstance(raw, dict):
        raise SignalFactsError(f"{prefix} must be an object")
    metric_id = require_string(raw, "metric_id", identifier=True)
    definition_id = require_string(raw, "definition_id", identifier=True)
    value = decimal_value(raw.get("value"), f"{prefix}.value")
    unit = require_string(raw, "unit", identifier=True)
    scale = decimal_value(raw.get("scale", 1), f"{prefix}.scale")
    if scale <= 0:
        raise SignalFactsError(f"{prefix}.scale must be positive")
    period_basis = require_string(raw, "period_basis", identifier=True)
    if period_basis not in PERIOD_BASES:
        raise SignalFactsError(f"{prefix}.period_basis unsupported: {period_basis!r}")
    kind = require_string(raw, "kind", identifier=True)
    if kind not in OBSERVATION_KINDS:
        raise SignalFactsError(f"{prefix}.kind unsupported: {kind!r}")
    verification = require_string(raw, "verification", identifier=True)
    if verification not in VERIFICATION_STATES:
        raise SignalFactsError(f"{prefix}.verification unsupported: {verification!r}")
    scope = require_string(raw, "scope")

    if (metric_id, definition_id) in POSITIVE_MAGNITUDE_KEYS and value <= 0:
        raise SignalFactsError(
            f"{prefix}.value must be a positive magnitude for "
            f"{metric_id}/{definition_id}"
        )

    observation_date: str | None = None
    if period_basis == "point_in_time":
        observation_date = raw.get("observation_date", period_end.isoformat())
        if not isinstance(observation_date, str) or not observation_date.strip():
            raise SignalFactsError(
                f"{prefix}.observation_date must be an ISO date string"
            )
        parse_date(observation_date, f"{prefix}.observation_date")
    elif "observation_date" in raw:
        raise SignalFactsError(
            f"{prefix}.observation_date is only allowed for point_in_time"
        )

    normalized: dict[str, Any] = {
        "metric_id": metric_id,
        "value": json_number(value),
        "unit": unit,
        "scale": json_number(scale),
        "period_basis": period_basis,
        "scope": scope,
        "kind": kind,
        "definition_id": definition_id,
        "verification": verification,
    }
    if observation_date is not None:
        normalized["observation_date"] = observation_date
    if "note" in raw:
        normalized["note"] = require_string(raw, "note")

    if raw.get("derivation") is not None:
        raise SignalFactsError(
            f"{prefix}.derivation is generated by the read model, not ingested"
        )
    return normalized


def normalize_record(raw: Any, *, require_record_id: bool) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise SignalFactsError("record must be a JSON object")
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise SignalFactsError(f"schema_version must be {SCHEMA_VERSION}")

    entity_raw = require_mapping(raw, "entity")
    entity_id = require_string(entity_raw, "id", identifier=True)
    if not ENTITY_RE.fullmatch(entity_id):
        raise SignalFactsError(f"entity.id is invalid: {entity_id!r}")
    entity: dict[str, Any] = {"id": entity_id}
    if "ticker" in entity_raw:
        ticker = require_string(entity_raw, "ticker", identifier=True)
        if not ENTITY_RE.fullmatch(ticker):
            raise SignalFactsError(f"entity.ticker is invalid: {ticker!r}")
        entity["ticker"] = ticker
    if "fiscal_year_end" in entity_raw:
        fiscal_year_end = require_string(entity_raw, "fiscal_year_end")
        if not re.fullmatch(r"\d{2}-\d{2}", fiscal_year_end):
            raise SignalFactsError("entity.fiscal_year_end must be MM-DD")
        entity["fiscal_year_end"] = fiscal_year_end

    event_raw = require_mapping(raw, "event")
    period_start_text = require_string(event_raw, "period_start")
    period_end_text = require_string(event_raw, "period_end")
    period_start = parse_date(period_start_text, "event.period_start")
    period_end = parse_date(period_end_text, "event.period_end")
    if period_start > period_end:
        raise SignalFactsError("event.period_start must not exceed period_end")
    reported_at = require_string(event_raw, "reported_at")
    reported_datetime = parse_datetime(reported_at, "event.reported_at")
    if reported_datetime.date() < period_end:
        raise SignalFactsError("event.reported_at cannot precede event.period_end")
    event_kind = require_string(event_raw, "kind", identifier=True)
    if event_kind != "earnings":
        raise SignalFactsError(
            f"event.kind unsupported in the current ledger: {event_kind!r}"
        )
    event = {
        "kind": event_kind,
        "fiscal_period": require_string(event_raw, "fiscal_period"),
        "period_start": period_start_text,
        "period_end": period_end_text,
        "reported_at": rfc3339(reported_datetime),
    }

    source_raw = require_mapping(raw, "source")
    source_type = require_string(source_raw, "type", identifier=True)
    if source_type not in SOURCE_TYPES:
        raise SignalFactsError(f"source.type unsupported: {source_type!r}")
    source_url = require_string(source_raw, "url")
    validate_url(source_url, "source.url")
    available_at = source_raw.get("available_at")
    available_datetime = reported_datetime
    if available_at is None:
        if not require_record_id:
            raise SignalFactsError("source.available_at is required for new records")
    else:
        available_datetime = parse_datetime(
            require_string(source_raw, "available_at"), "source.available_at"
        )
        if available_datetime < reported_datetime:
            raise SignalFactsError(
                "source.available_at cannot precede event.reported_at"
            )
    retrieved_at = require_string(source_raw, "retrieved_at")
    retrieved_datetime = parse_datetime(retrieved_at, "source.retrieved_at")
    if retrieved_datetime < available_datetime:
        raise SignalFactsError("source.retrieved_at cannot precede source.available_at")
    source = {
        "type": source_type,
        "url": source_url,
        "accession_or_document_id": require_string(
            source_raw, "accession_or_document_id", identifier=True
        ),
        "retrieved_at": rfc3339(retrieved_datetime),
    }
    if available_at is not None:
        source["available_at"] = rfc3339(available_datetime)

    observations_raw = require_list(raw, "observations")
    if not observations_raw:
        raise SignalFactsError("observations must be non-empty")
    observations = [
        normalized_observation(
            item,
            index=index,
            period_end=period_end,
        )
        for index, item in enumerate(observations_raw)
    ]
    if source_type not in PRIMARY_SOURCE_TYPES:
        primary_claims = [
            observation
            for observation in observations
            if observation["verification"] in PRIMARY_VERIFICATIONS
        ]
        if primary_claims:
            raise SignalFactsError(
                "secondary sources cannot use a primary verification state"
            )
    observation_keys: set[tuple[str, str, str, str, str]] = set()
    for observation in observations:
        key = (
            observation["metric_id"],
            observation["definition_id"],
            observation["period_basis"],
            observation["scope"],
            observation.get("observation_date", ""),
        )
        if key in observation_keys:
            raise SignalFactsError(
                "record contains duplicate observation key: " + "|".join(key)
            )
        observation_keys.add(key)

    supersedes_raw = raw.get("supersedes", [])
    if not isinstance(supersedes_raw, list) or not all(
        isinstance(item, str) and IDENTIFIER_RE.fullmatch(item)
        for item in supersedes_raw
    ):
        raise SignalFactsError("supersedes must be a list of record ids")

    normalized = {
        "schema_version": SCHEMA_VERSION,
        "entity": entity,
        "event": event,
        "source": source,
        "observations": observations,
        "supersedes": list(dict.fromkeys(supersedes_raw)),
    }
    computed_id = compute_record_id(normalized)
    supplied_id = raw.get("record_id")
    if supplied_id is not None:
        if not isinstance(supplied_id, str) or supplied_id != computed_id:
            raise SignalFactsError(f"record_id mismatch: expected {computed_id!r}")
    elif require_record_id:
        raise SignalFactsError(f"record_id missing; expected {computed_id!r}")
    normalized["record_id"] = computed_id
    return normalized


def compute_record_id(record: dict[str, Any]) -> str:
    payload = copy.deepcopy(record)
    payload.pop("record_id", None)
    source = payload.get("source")
    if isinstance(source, dict):
        source.pop("retrieved_at", None)
    observations = payload.get("observations")
    if isinstance(observations, list):
        observations.sort(
            key=lambda item: (
                str(item.get("metric_id", "")),
                str(item.get("definition_id", "")),
                str(item.get("period_basis", "")),
                str(item.get("scope", "")),
                canonical_json(item),
            )
        )
    supersedes = payload.get("supersedes")
    if isinstance(supersedes, list):
        supersedes.sort()
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return f"sf1-{digest[:24]}"


def record_path(ledger_dir: Path, record: dict[str, Any]) -> Path:
    return (
        ledger_dir
        / "earnings"
        / record["entity"]["id"]
        / record["event"]["period_end"]
        / f"{record['record_id']}.json"
    )


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temp = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temp_path = Path(raw_temp)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def load_ledger(ledger_dir: Path) -> LedgerLoad:
    if not ledger_dir.exists():
        return LedgerLoad(records=(), invalid=())
    records: list[dict[str, Any]] = []
    invalid: list[dict[str, str]] = []
    for path in sorted(ledger_dir.rglob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            record = normalize_record(raw, require_record_id=True)
        except (OSError, UnicodeError, json.JSONDecodeError, SignalFactsError) as exc:
            invalid.append({"path": fmt(path), "error": str(exc)})
            continue
        expected_path = record_path(ledger_dir, record)
        if path.resolve() != expected_path.resolve():
            invalid.append(
                {
                    "path": fmt(path),
                    "error": f"non-canonical path; expected {fmt(expected_path)}",
                }
            )
            continue
        records.append(record)
    return LedgerLoad(records=tuple(records), invalid=tuple(invalid))


def source_identity(record: dict[str, Any]) -> tuple[str, str, str]:
    return (
        record["entity"]["id"],
        record["event"]["period_end"],
        record["source"]["accession_or_document_id"],
    )


@contextmanager
def ledger_write_lock(ledger_dir: Path) -> Iterable[None]:
    ledger_dir.mkdir(parents=True, exist_ok=True)
    lock_path = ledger_dir / ".ingest.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def ingest_record(config: Config, raw: Any) -> dict[str, Any]:
    record = normalize_record(raw, require_record_id=False)
    with ledger_write_lock(config.ledger_dir):
        return _ingest_record_locked(config, record)


def _ingest_record_locked(config: Config, record: dict[str, Any]) -> dict[str, Any]:
    loaded = load_ledger(config.ledger_dir)
    if loaded.invalid:
        raise SignalFactsError(
            f"ledger has {len(loaded.invalid)} invalid record(s); run validate"
        )

    existing_by_id = {item["record_id"]: item for item in loaded.records}
    record_id = record["record_id"]
    target = record_path(config.ledger_dir, record)
    if record_id in existing_by_id:
        return {
            "schema": 1,
            "status": "unchanged",
            "record_id": record_id,
            "path": fmt(target),
        }

    superseded_ids = {
        superseded
        for item in loaded.records
        for superseded in item.get("supersedes", [])
    }
    same_source_tips = [
        item
        for item in loaded.records
        if source_identity(item) == source_identity(record)
        and item["record_id"] not in superseded_ids
        and item["record_id"] not in record["supersedes"]
    ]
    if same_source_tips:
        prior_ids = ", ".join(sorted(item["record_id"] for item in same_source_tips))
        raise SignalFactsError(
            "source identity already exists with different content; "
            f"set supersedes to the prior record id(s): {prior_ids}"
        )

    for superseded in record["supersedes"]:
        if superseded not in existing_by_id:
            raise SignalFactsError(f"supersedes unknown record: {superseded}")
        if source_identity(existing_by_id[superseded]) != source_identity(record):
            raise SignalFactsError(
                "supersedes must reference the same entity, period, and source"
            )

    atomic_write(
        target,
        json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    return {
        "schema": 1,
        "status": "written",
        "record_id": record_id,
        "path": fmt(target),
    }


def observation_key(
    record: dict[str, Any], observation: dict[str, Any]
) -> tuple[str, str, str, str, str, str, str, str]:
    event = record["event"]
    return (
        record["entity"]["id"],
        event["period_start"],
        event["period_end"],
        observation["metric_id"],
        observation["definition_id"],
        observation["period_basis"],
        observation["scope"],
        observation.get("observation_date", ""),
    )


def base_value(observation: dict[str, Any]) -> Decimal:
    return decimal_value(observation["value"], "value") * decimal_value(
        observation.get("scale", 1), "scale"
    )


def fact_sort_key(fact: dict[str, Any]) -> tuple[str, str, str, str, str, str]:
    return (
        fact["metric_id"],
        fact["definition_id"],
        fact["period_basis"],
        fact["scope"],
        str(fact.get("observation_date", "")),
        str(fact.get("value", "")),
    )


def worst_verification(values: Iterable[str]) -> str:
    return max(values, key=lambda item: VERIFICATION_RANK[item])


def source_available_at(record: dict[str, Any]) -> datetime:
    """Return public availability, preserving legacy report-time behavior."""
    return parse_datetime(
        record["source"].get("available_at", record["event"]["reported_at"]),
        "source.available_at",
    )


def active_records(
    records: Sequence[dict[str, Any]], as_of: datetime
) -> list[dict[str, Any]]:
    visible = [record for record in records if source_available_at(record) <= as_of]
    superseded = {
        record_id for record in visible for record_id in record.get("supersedes", [])
    }
    return [record for record in visible if record["record_id"] not in superseded]


def resolve_facts(
    records: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    grouped: dict[
        tuple[str, str, str, str, str, str, str, str],
        list[tuple[dict[str, Any], dict[str, Any]]],
    ] = {}
    for record in records:
        for observation in record["observations"]:
            grouped.setdefault(observation_key(record, observation), []).append(
                (record, observation)
            )

    facts: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for key in sorted(grouped):
        rows = grouped[key]
        primary_rows = [
            row for row in rows if row[1]["verification"] in PRIMARY_VERIFICATIONS
        ]
        resolution_rows = primary_rows or rows
        values = {
            (
                base_value(observation),
                observation["unit"],
                observation["kind"],
            )
            for _, observation in resolution_rows
        }
        if len(values) != 1:
            conflicts.append(
                {
                    "blocking": True,
                    "reason": "verified_sources_disagree"
                    if primary_rows
                    else "candidate_sources_disagree",
                    "key": {
                        "entity_id": key[0],
                        "period_start": key[1],
                        "period_end": key[2],
                        "metric_id": key[3],
                        "definition_id": key[4],
                        "period_basis": key[5],
                        "scope": key[6],
                        **({"observation_date": key[7]} if key[7] else {}),
                    },
                    "observations": [
                        {
                            "record_id": record["record_id"],
                            "value": observation["value"],
                            "scale": observation["scale"],
                            "unit": observation["unit"],
                            "kind": observation["kind"],
                            "verification": observation["verification"],
                            "source_type": record["source"]["type"],
                            "source_url": record["source"]["url"],
                        }
                        for record, observation in resolution_rows
                    ],
                }
            )
            continue

        first_record, first_observation = resolution_rows[0]
        value, unit, kind = next(iter(values))
        candidate_disagreements = [
            (record, observation)
            for record, observation in rows
            if observation["verification"] not in PRIMARY_VERIFICATIONS
            and (
                base_value(observation),
                observation["unit"],
                observation["kind"],
            )
            != (value, unit, kind)
        ]
        if candidate_disagreements:
            conflicts.append(
                {
                    "blocking": False,
                    "reason": "candidate_disagrees_with_verified_fact",
                    "key": {
                        "entity_id": key[0],
                        "period_start": key[1],
                        "period_end": key[2],
                        "metric_id": key[3],
                        "definition_id": key[4],
                        "period_basis": key[5],
                        "scope": key[6],
                        **({"observation_date": key[7]} if key[7] else {}),
                    },
                    "observations": [
                        {
                            "record_id": record["record_id"],
                            "value": observation["value"],
                            "scale": observation["scale"],
                            "unit": observation["unit"],
                            "kind": observation["kind"],
                            "verification": observation["verification"],
                            "source_type": record["source"]["type"],
                            "source_url": record["source"]["url"],
                        }
                        for record, observation in candidate_disagreements
                    ],
                }
            )
        sources = sorted(
            [
                {
                    "record_id": record["record_id"],
                    "url": record["source"]["url"],
                    "document_id": record["source"]["accession_or_document_id"],
                    "available_at": rfc3339(source_available_at(record)),
                    "verification": observation["verification"],
                }
                for record, observation in resolution_rows
            ],
            key=lambda item: (item["record_id"], item["url"]),
        )
        fact: dict[str, Any] = {
            "entity_id": key[0],
            "period_start": key[1],
            "period_end": key[2],
            "fiscal_period": first_record["event"]["fiscal_period"],
            "reported_at": max(
                record["event"]["reported_at"] for record, _ in resolution_rows
            ),
            "available_at": rfc3339(
                max(source_available_at(record) for record, _ in resolution_rows)
            ),
            "metric_id": key[3],
            "definition_id": key[4],
            "period_basis": key[5],
            "scope": key[6],
            "value": json_number(value),
            "unit": unit,
            "scale": 1,
            "kind": kind,
            "verification": worst_verification(
                observation["verification"] for _, observation in resolution_rows
            ),
            "sources": sources,
        }
        if key[7]:
            fact["observation_date"] = key[7]
        if first_observation.get("note"):
            fact["note"] = first_observation["note"]
        if first_observation.get("derivation"):
            fact["derivation"] = first_observation["derivation"]
        facts.append(fact)
    return facts, conflicts


def find_fact(
    by_event: dict[tuple[str, str, str], list[dict[str, Any]]],
    event_key: tuple[str, str, str],
    metric_definition: tuple[str, str],
    *,
    period_basis: str = "quarter",
    scope: str = "consolidated",
) -> dict[str, Any] | None:
    metric_id, definition_id = metric_definition
    return next(
        (
            fact
            for fact in by_event.get(event_key, [])
            if fact["metric_id"] == metric_id
            and fact["definition_id"] == definition_id
            and fact["period_basis"] == period_basis
            and fact["scope"] == scope
        ),
        None,
    )


def derived_fact(
    *,
    base: dict[str, Any],
    metric_definition: tuple[str, str],
    value: Decimal,
    formula_id: str,
    operands: Sequence[dict[str, Any]],
    note: str | None = None,
) -> dict[str, Any]:
    metric_id, definition_id = metric_definition
    sources_by_record: dict[str, dict[str, Any]] = {}
    for operand in operands:
        for source in operand["sources"]:
            sources_by_record[source["record_id"]] = source
    fact: dict[str, Any] = {
        "entity_id": base["entity_id"],
        "period_start": base["period_start"],
        "period_end": base["period_end"],
        "fiscal_period": base["fiscal_period"],
        "reported_at": max(operand["reported_at"] for operand in operands),
        "available_at": max(operand["available_at"] for operand in operands),
        "metric_id": metric_id,
        "definition_id": definition_id,
        "period_basis": "quarter",
        "scope": "consolidated",
        "value": json_number(value),
        "unit": base["unit"],
        "scale": 1,
        "kind": "derived",
        "verification": worst_verification(
            operand["verification"] for operand in operands
        ),
        "sources": [sources_by_record[key] for key in sorted(sources_by_record)],
        "derivation": {
            "formula_id": formula_id,
            "operands": [
                {
                    "metric_id": operand["metric_id"],
                    "definition_id": operand["definition_id"],
                    "record_ids": [
                        source["record_id"] for source in operand["sources"]
                    ],
                }
                for operand in operands
            ],
        },
    }
    if note:
        fact["note"] = note
    return fact


def require_matching_units(formula_id: str, operands: Sequence[dict[str, Any]]) -> None:
    units = {operand["unit"] for operand in operands}
    if len(units) != 1:
        raise SignalFactsError(
            f"{formula_id} cannot combine mixed units: " + ", ".join(sorted(units))
        )


def derive_cash_flow_facts(facts: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    by_event: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for fact in facts:
        key = (fact["entity_id"], fact["period_start"], fact["period_end"])
        by_event.setdefault(key, []).append(fact)

    derived: list[dict[str, Any]] = []
    for event_key in sorted(by_event):
        ocf = find_fact(by_event, event_key, OCF_KEY)
        gross_capex = find_fact(by_event, event_key, CAPEX_GROSS_KEY)
        net_capex = find_fact(by_event, event_key, CAPEX_NET_KEY)
        offset = find_fact(by_event, event_key, CAPEX_OFFSET_KEY)
        if ocf is None or (gross_capex is None and net_capex is None):
            continue
        if gross_capex is not None:
            operands = [ocf, gross_capex]
            value = base_value(ocf) - base_value(gross_capex)
            note = "No explicit cash offset was included."
            if offset is not None:
                operands.append(offset)
                value += base_value(offset)
                note = "Explicitly disclosed proceeds or incentives were included."
        else:
            assert net_capex is not None
            operands = [ocf, net_capex]
            value = base_value(ocf) - base_value(net_capex)
            note = "Used issuer-disclosed cash PP&E purchases net of offsets."
        require_matching_units(FORMULA_CANONICAL_FCF, operands)
        canonical = derived_fact(
            base=ocf,
            metric_definition=CANONICAL_FCF_KEY,
            value=value,
            formula_id=FORMULA_CANONICAL_FCF,
            operands=operands,
            note=note,
        )
        derived.append(canonical)

        lease_principal = find_fact(by_event, event_key, LEASE_PRINCIPAL_KEY)
        if lease_principal is not None:
            lease_operands = [canonical, lease_principal]
            require_matching_units(FORMULA_LEASE_ADJUSTED_FCF, lease_operands)
            derived.append(
                derived_fact(
                    base=ocf,
                    metric_definition=LEASE_ADJUSTED_FCF_KEY,
                    value=value - base_value(lease_principal),
                    formula_id=FORMULA_LEASE_ADJUSTED_FCF,
                    operands=lease_operands,
                )
            )
    return derived


def record_events(
    records: Sequence[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for record in records:
        key = (
            record["entity"]["id"],
            record["event"]["period_start"],
            record["event"]["period_end"],
        )
        grouped.setdefault(key, []).append(record)

    by_entity: dict[str, list[dict[str, Any]]] = {}
    for key, rows in grouped.items():
        entity_id, period_start, period_end = key
        event = {
            "entity_id": entity_id,
            "period_start": period_start,
            "period_end": period_end,
            "fiscal_period": rows[0]["event"]["fiscal_period"],
            "reported_at": max(row["event"]["reported_at"] for row in rows),
            "available_at": rfc3339(max(source_available_at(row) for row in rows)),
            "sources": sorted(
                [
                    {
                        "record_id": row["record_id"],
                        "type": row["source"]["type"],
                        "url": row["source"]["url"],
                        "document_id": row["source"]["accession_or_document_id"],
                        "available_at": rfc3339(source_available_at(row)),
                    }
                    for row in rows
                ],
                key=lambda item: item["record_id"],
            ),
        }
        by_entity.setdefault(entity_id, []).append(event)
    for events in by_entity.values():
        events.sort(
            key=lambda item: (item["period_end"], item["reported_at"]),
            reverse=True,
        )
    return by_entity


def event_age_days(period_end: str, as_of: datetime) -> int:
    return (as_of.date() - parse_date(period_end, "period_end")).days


def evaluate_signal(
    spec: SignalSpec,
    *,
    profile: Profile,
    facts: Sequence[dict[str, Any]],
    events: dict[str, list[dict[str, Any]]],
    as_of: datetime,
) -> dict[str, Any]:
    readings: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    below: list[str] = []
    for entity_id in profile.entities:
        entity_events = events.get(entity_id, [])
        if not entity_events:
            gaps.append({"entity_id": entity_id, "reason": "no_records"})
            continue
        latest_event = entity_events[0]
        matching = [
            fact
            for fact in facts
            if fact["entity_id"] == entity_id
            and fact["period_start"] == latest_event["period_start"]
            and fact["period_end"] == latest_event["period_end"]
            and fact["metric_id"] == spec.metric_id
            and fact["definition_id"] == spec.definition_id
            and fact["period_basis"] == spec.period_basis
            and fact["scope"] == spec.scope
        ]
        if not matching:
            gaps.append(
                {
                    "entity_id": entity_id,
                    "period_end": latest_event["period_end"],
                    "reason": "metric_missing_for_latest_event",
                }
            )
            continue
        fact = matching[0]
        if fact["verification"] not in PRIMARY_VERIFICATIONS:
            gaps.append(
                {
                    "entity_id": entity_id,
                    "period_end": fact["period_end"],
                    "reason": "candidate_only",
                }
            )
            continue
        age_days = event_age_days(fact["period_end"], as_of)
        is_stale = age_days > profile.max_age_days
        reading = {
            "entity_id": entity_id,
            "period_end": fact["period_end"],
            "value": fact["value"],
            "unit": fact["unit"],
            "age_days": age_days,
            "stale": is_stale,
            "source_urls": sorted({source["url"] for source in fact["sources"]}),
        }
        readings.append(reading)
        if is_stale:
            gaps.append(
                {
                    "entity_id": entity_id,
                    "period_end": fact["period_end"],
                    "reason": "stale",
                    "age_days": age_days,
                }
            )
            continue
        if base_value(fact) < spec.threshold:
            below.append(entity_id)

    if len(below) >= spec.required_count:
        state = "lit"
    elif gaps:
        state = "unknown"
    else:
        state = "dark"
    return {
        "signal_id": spec.signal_id,
        "kind": spec.kind,
        "metric_id": spec.metric_id,
        "definition_id": spec.definition_id,
        "period_basis": spec.period_basis,
        "scope": spec.scope,
        "threshold": json_number(spec.threshold),
        "required_count": spec.required_count,
        "state": state,
        "reading": f"{len(below)}/{spec.required_count}",
        "matching_entities": below,
        "entity_readings": readings,
        "gaps": gaps,
    }


def ledger_fingerprint(records: Sequence[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for record_id in sorted(record["record_id"] for record in records):
        digest.update(record_id.encode("utf-8"))
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def build_bundle(
    config: Config,
    *,
    profile_name: str,
    as_of: datetime,
) -> dict[str, Any]:
    profile = config.profiles.get(profile_name)
    if profile is None:
        choices = ", ".join(sorted(config.profiles))
        raise SignalFactsError(
            f"unknown profile {profile_name!r}; expected one of: {choices}"
        )
    loaded = load_ledger(config.ledger_dir)
    visible = active_records(loaded.records, as_of)
    facts, conflicts = resolve_facts(visible)
    derived = derive_cash_flow_facts(facts)
    all_facts = sorted([*facts, *derived], key=fact_sort_key)
    events = record_events(visible)

    entities: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    for entity_id in profile.entities:
        entity_events = events.get(entity_id, [])
        if not entity_events:
            entities.append({"entity_id": entity_id, "events": []})
            gaps.append({"entity_id": entity_id, "reason": "no_records"})
            continue
        allowed_metrics = {
            metric.key for metric in profile.metrics if metric.applies_to(entity_id)
        }
        selected_events: list[dict[str, Any]] = []
        for event in entity_events[: profile.latest_periods]:
            event_facts = [
                fact
                for fact in all_facts
                if fact["entity_id"] == entity_id
                and fact["period_start"] == event["period_start"]
                and fact["period_end"] == event["period_end"]
            ]
            if profile.metrics:
                event_facts = [
                    fact
                    for fact in event_facts
                    if (fact["metric_id"], fact["definition_id"]) in allowed_metrics
                ]
            selected_events.append(
                {
                    **event,
                    "age_days": event_age_days(event["period_end"], as_of),
                    "stale": event_age_days(event["period_end"], as_of)
                    > profile.max_age_days,
                    "observations": sorted(event_facts, key=fact_sort_key),
                }
            )
        latest_event = selected_events[0]
        if latest_event["stale"]:
            gaps.append(
                {
                    "entity_id": entity_id,
                    "period_end": latest_event["period_end"],
                    "reason": "latest_event_stale",
                    "age_days": latest_event["age_days"],
                }
            )
        for metric in profile.metrics:
            if not metric.required or not metric.applies_to(entity_id):
                continue
            matching = [
                fact
                for fact in latest_event["observations"]
                if (fact["metric_id"], fact["definition_id"]) == metric.key
                and fact["period_basis"] == metric.period_basis
                and fact["scope"] == metric.scope
            ]
            if not matching:
                gaps.append(
                    {
                        "entity_id": entity_id,
                        "period_end": latest_event["period_end"],
                        "metric_id": metric.metric_id,
                        "definition_id": metric.definition_id,
                        "period_basis": metric.period_basis,
                        "scope": metric.scope,
                        "reason": "required_metric_missing_for_latest_event",
                    }
                )
            elif matching[0]["verification"] not in PRIMARY_VERIFICATIONS:
                gaps.append(
                    {
                        "entity_id": entity_id,
                        "period_end": latest_event["period_end"],
                        "metric_id": metric.metric_id,
                        "definition_id": metric.definition_id,
                        "period_basis": metric.period_basis,
                        "scope": metric.scope,
                        "reason": "required_metric_candidate_only",
                    }
                )
        entities.append({"entity_id": entity_id, "events": selected_events})

    signals = [
        evaluate_signal(
            spec,
            profile=profile,
            facts=all_facts,
            events=events,
            as_of=as_of,
        )
        for spec in profile.signals
    ]
    for signal in signals:
        gaps.extend({"signal_id": signal["signal_id"], **gap} for gap in signal["gaps"])
    for invalid in loaded.invalid:
        gaps.append({"reason": "invalid_record", **invalid})

    return {
        "schema": 1,
        "profile": profile_name,
        "as_of": rfc3339(as_of),
        "ledger": {
            "path": fmt(config.ledger_dir),
            "fingerprint": ledger_fingerprint(visible),
            "active_record_count": len(visible),
            "invalid_record_count": len(loaded.invalid),
        },
        "entities": entities,
        "signals": signals,
        "conflicts": conflicts,
        "gaps": gaps,
        "retrieval_required": bool(gaps or conflicts),
    }


def compact_bundle(bundle: dict[str, Any], max_bytes: int) -> dict[str, Any]:
    result = copy.deepcopy(bundle)
    result["omissions"] = []

    def encoded_size() -> int:
        json_size = len(
            (
                json.dumps(
                    result,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8")
        )
        markdown_size = len(render_markdown(result).encode("utf-8"))
        return max(json_size, markdown_size)

    if encoded_size() <= max_bytes:
        return result
    for entity in result["entities"]:
        events = entity.get("events", [])
        if len(events) > 1:
            omitted = len(events) - 1
            entity["events"] = events[:1]
            result["omissions"].append(
                {
                    "entity_id": entity["entity_id"],
                    "reason": "byte_budget",
                    "older_events_omitted": omitted,
                }
            )
    if encoded_size() <= max_bytes:
        return result

    signal_keys = {
        (signal["metric_id"], signal["definition_id"]) for signal in result["signals"]
    }
    for entity in result["entities"]:
        for event in entity.get("events", []):
            observations = event.get("observations", [])
            kept = [
                observation
                for observation in observations
                if (
                    observation["metric_id"],
                    observation["definition_id"],
                )
                in signal_keys
            ]
            if len(kept) < len(observations):
                event["observations"] = kept
                result["omissions"].append(
                    {
                        "entity_id": entity["entity_id"],
                        "period_end": event["period_end"],
                        "reason": "byte_budget",
                        "non_signal_observations_omitted": len(observations)
                        - len(kept),
                    }
                )
    if encoded_size() > max_bytes:
        raise SignalFactsError(
            f"bundle cannot fit {max_bytes} bytes after deterministic compaction"
        )
    return result


def render_markdown(bundle: dict[str, Any]) -> str:
    ledger = bundle["ledger"]
    lines = [
        "## Signal facts bundle",
        "",
        f"- Profile: `{bundle['profile']}`",
        f"- As of: `{bundle['as_of']}`",
        f"- Ledger fingerprint: `{ledger['fingerprint']}`",
        f"- Active records: {ledger['active_record_count']}",
        f"- Retrieval required: {'yes' if bundle['retrieval_required'] else 'no'}",
        "",
        "## Policy signals",
        "",
    ]
    if bundle["signals"]:
        lines.extend(
            [
                "| Signal | State | Reading | Definition | Basis | Scope |",
                "|---|---|---:|---|---|---|",
            ]
        )
        for signal in bundle["signals"]:
            lines.append(
                f"| {signal['signal_id']} | {signal['state']} | "
                f"{signal['reading']} | {signal['definition_id']} | "
                f"{signal['period_basis']} | {signal['scope']} |"
            )
    else:
        lines.append("No configured policy signals.")

    for entity in bundle["entities"]:
        lines.extend(["", f"## {entity['entity_id']}", ""])
        if not entity["events"]:
            lines.append("No records.")
            continue
        for event in entity["events"]:
            lines.extend(
                [
                    f"### {event['fiscal_period']} ending {event['period_end']}",
                    "",
                    f"Reported: `{event['reported_at']}`; "
                    f"age {event['age_days']} days; "
                    f"stale: {'yes' if event['stale'] else 'no'}.",
                    "",
                    "| Metric | Definition | Basis | Observation date | Value | Verification |",
                    "|---|---|---|---|---:|---|",
                ]
            )
            for observation in event["observations"]:
                lines.append(
                    f"| {observation['metric_id']} | "
                    f"{observation['definition_id']} | "
                    f"{observation['period_basis']} | "
                    f"{observation.get('observation_date', '')} | "
                    f"{observation['value']} {observation['unit']} | "
                    f"{observation['verification']} |"
                )
            derivations = [
                observation
                for observation in event["observations"]
                if observation.get("derivation")
            ]
            if derivations:
                lines.extend(["", "Derivations:"])
                for observation in derivations:
                    derivation = observation["derivation"]
                    operand_labels = [
                        (
                            f"{operand['metric_id']}/"
                            f"{operand['definition_id']} "
                            f"({', '.join(operand['record_ids'])})"
                        )
                        for operand in derivation["operands"]
                    ]
                    lines.append(
                        f"- `{observation['metric_id']}/"
                        f"{observation['definition_id']}` via "
                        f"`{derivation['formula_id']}` from "
                        + "; ".join(operand_labels)
                    )
            if event.get("sources"):
                lines.extend(["", "Sources:"])
                for source in event["sources"]:
                    lines.append(
                        f"- [{source['document_id']}]({source['url']}) "
                        f"(`{source['record_id']}`)"
                    )
    if bundle["conflicts"]:
        lines.extend(["", "## Conflicts", ""])
        for conflict in bundle["conflicts"]:
            key = conflict["key"]
            lines.append(
                f"- {key['entity_id']} {key['period_end']} "
                f"{key['metric_id']} / {key['definition_id']}"
            )
    if bundle["gaps"]:
        lines.extend(["", "## Retrieval gaps", ""])
        for gap in bundle["gaps"]:
            label = gap.get("entity_id", gap.get("path", "ledger"))
            lines.append(f"- `{label}`: {gap['reason']}")
    return "\n".join(lines).rstrip() + "\n"


def cache_bundle(config: Config, profile_name: str, content: str) -> Path:
    path = config.cache_dir / f"{profile_name}-latest.json"
    atomic_write(path, content)
    return path


def read_json_file(path: str) -> Any:
    candidate = Path(path).expanduser()
    try:
        return json.loads(candidate.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SignalFactsError(f"input file missing: {candidate}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SignalFactsError(f"cannot read JSON input {candidate}: {exc}") from exc


def command_ingest(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config) if args.config else None)
    raw = read_json_file(args.file)
    result = ingest_record(config, raw)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def command_validate(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config) if args.config else None)
    if args.file:
        records = []
        invalid = []
        for raw_path in args.file:
            try:
                normalized = normalize_record(
                    read_json_file(raw_path), require_record_id=args.require_record_id
                )
                records.append(
                    {
                        "path": str(Path(raw_path).expanduser()),
                        "record_id": normalized["record_id"],
                    }
                )
            except SignalFactsError as exc:
                invalid.append({"path": raw_path, "error": str(exc)})
    else:
        loaded = load_ledger(config.ledger_dir)
        records = [{"record_id": record["record_id"]} for record in loaded.records]
        invalid = list(loaded.invalid)
    result = {
        "schema": 1,
        "valid_count": len(records),
        "invalid_count": len(invalid),
        "valid": records,
        "invalid": invalid,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if invalid else 0


def command_bundle(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config) if args.config else None)
    if args.cache and args.as_of is not None:
        raise SignalFactsError(
            "--cache cannot be combined with --as-of; "
            "historical projections must not replace the latest cache"
        )
    as_of = parse_as_of(args.as_of)
    bundle = build_bundle(config, profile_name=args.profile, as_of=as_of)
    if args.cache:
        bundle["cache"] = {
            "path": fmt(config.cache_dir / f"{args.profile}-latest.json")
        }
    compact = compact_bundle(bundle, args.max_bytes)
    if args.format == "json":
        rendered = (
            json.dumps(compact, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        )
    else:
        rendered = render_markdown(compact)
    if len(rendered.encode("utf-8")) > args.max_bytes:
        raise SignalFactsError(f"rendered bundle exceeds --max-bytes={args.max_bytes}")
    if args.cache:
        json_content = (
            json.dumps(compact, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        )
        cache_bundle(config, args.profile, json_content)
    sys.stdout.write(rendered)
    return 0


def command_status(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config) if args.config else None)
    as_of = parse_as_of(args.as_of)
    bundle = build_bundle(config, profile_name=args.profile, as_of=as_of)
    result = {
        "schema": 1,
        "profile": args.profile,
        "as_of": bundle["as_of"],
        "ledger": bundle["ledger"],
        "signals": [
            {
                "signal_id": signal["signal_id"],
                "state": signal["state"],
                "reading": signal["reading"],
            }
            for signal in bundle["signals"]
        ],
        "conflict_count": len(bundle["conflicts"]),
        "gap_count": len(bundle["gaps"]),
        "retrieval_required": bundle["retrieval_required"],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if bundle["conflicts"] or bundle["gaps"] else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate, ingest, and query a private signal-fact ledger."
    )
    parser.add_argument(
        "--config",
        help="config path under $OV; defaults to $OV/_meta/signal_facts.toml",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest = subparsers.add_parser(
        "ingest", help="validate and atomically add one immutable record"
    )
    ingest.add_argument("--file", required=True, help="candidate JSON record")
    ingest.set_defaults(func=command_ingest)

    validate = subparsers.add_parser(
        "validate", help="validate candidate files or the configured ledger"
    )
    validate.add_argument("file", nargs="*", help="candidate JSON record")
    validate.add_argument(
        "--require-record-id",
        action="store_true",
        help="require candidate files to contain their computed record id",
    )
    validate.set_defaults(func=command_validate)

    bundle = subparsers.add_parser(
        "bundle", help="build a bounded analysis bundle from current facts"
    )
    bundle.add_argument("--profile", required=True)
    bundle.add_argument("--as-of", help="ISO date or RFC3339 timestamp")
    bundle.add_argument("--format", choices=("json", "markdown"), default="json")
    bundle.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    bundle.add_argument(
        "--cache",
        action="store_true",
        help="also write the JSON read model to the configured L1 cache",
    )
    bundle.set_defaults(func=command_bundle)

    status = subparsers.add_parser(
        "status", help="summarize gaps, conflicts, and policy states"
    )
    status.add_argument("--profile", required=True)
    status.add_argument("--as-of", help="ISO date or RFC3339 timestamp")
    status.set_defaults(func=command_status)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if hasattr(args, "max_bytes") and not 1024 <= args.max_bytes <= 1024 * 1024:
        parser.error("--max-bytes must be from 1024 to 1048576")
    try:
        return int(args.func(args))
    except SignalFactsError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
