#!/usr/bin/env python3
"""Autoevo's structured proposal/result contract and derived human report."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

from _git import hash_objects, tree_blobs
from _paths import atomic_write, retry_transient, tier_segments, vault_root

VERSION = 1
CATEGORIES = ("redundant", "time-stale-A", "time-stale-B", "contradicted", "low-signal")
BLOCKING_STATES = ("half-applied", "malformed")
NOTE_KINDS = ("redundant-high", "low-signal-high", "stale-banner", "wiki-review")
SHA256 = re.compile(r"[0-9a-f]{64}")


class VerificationError(RuntimeError):
    """Missing, malformed, or unproven domain evidence."""


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def relative(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value or any(ord(c) < 32 for c in value):
        raise VerificationError("expected a safe relative path")
    path = Path(value)
    if path.is_absolute() or any(part in {"", ".", "..", ".git"} for part in value.split("/")):
        raise VerificationError(f"unsafe relative path: {value!r}")
    return value


def record_path(vault: Path, cycle: str) -> Path:
    from datetime import date

    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", cycle) or date.fromisoformat(cycle).isoformat() != cycle:
        raise VerificationError("cycle must be a calendar date")
    path = vault.resolve() / relative(tier_segments().get("meta", "_meta")) / "routine_receipts" / "autoevo-nightly" / f"{cycle}.json"
    _regular_record(path)
    return path


def _regular_record(path: Path) -> None:
    if path.resolve() != path.absolute() or path.is_symlink():
        raise VerificationError("Autoevo result path contains a symlink")
    if path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
        raise VerificationError("Autoevo result must be a regular single-link file")


def read_record(path: Path) -> dict:
    _regular_record(path)
    try:
        value = json.loads(retry_transient(lambda: path.read_text(encoding="utf-8"), what="autoevo result read"))
    except (OSError, ValueError) as exc:
        raise VerificationError(f"cannot read Autoevo result: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != VERSION:
        raise VerificationError("unsupported Autoevo result")
    return value


def write_record(path: Path, record: dict) -> None:
    _regular_record(path)
    atomic_write(path, json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


def proposal_schema(plan: dict) -> dict:
    """One schema validates the candidate file before it acquires write authority."""
    strings = {"type": "array", "items": {"type": "string"}}
    finding = {
        "type": "object",
        "required": ["category", "candidate", "confidence", "evidence", "proposed_action"],
        "properties": {
            "category": {"enum": list(CATEGORIES)}, "candidate": {"type": "string"},
            "confidence": {"enum": ["high", "medium", "low"]},
            "evidence": {"type": "string"}, "proposed_action": {"type": "string"},
            "peers": strings, "scores": {"type": "array", "items": {"type": "number"}},
            "mode": {"type": "string"}, "conditions_met": {"type": "integer"},
            "claim": {"type": "string"}, "contradicting_peer": {"type": "string"},
            "contradiction_signal": {"type": "string"},
            "curator": {"type": "object"}, "probe": {"type": "object"},
        },
        "additionalProperties": False,
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object", "additionalProperties": False,
        "required": ["schema_version", "cycle_id", "sweeps", "judgments", "notes", "errors"],
        "properties": {
            "schema_version": {"const": VERSION}, "cycle_id": {"const": plan["cycle_id"]},
            "sweeps": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["scope", "outcome", "mode", "completion_status", "remaining_work", "gaps", "findings", "notes"],
                "properties": {
                    "scope": {"enum": [row["scope"] for row in plan["dispatches"]]},
                    "outcome": {"enum": ["envelope_returned", "forgetter_no_envelope"]},
                    "mode": {"enum": ["full", "partial", "absent"]},
                    "completion_status": {"enum": ["complete", "partial", "aborted"]},
                    "remaining_work": {"type": "string"}, "gaps": {"type": "string"},
                    "findings": {"type": "array", "items": finding}, "notes": strings,
                },
            }},
            "judgments": {"type": "object", "additionalProperties": {
                "type": "object", "additionalProperties": False,
                "required": ["bundle_sha256", "judgment"],
                "properties": {"bundle_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                               "judgment": {"type": "object"}},
            }},
            "notes": strings, "errors": strings,
            "wiki_reviews": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["path", "claim", "verdict", "reason"],
                "properties": {"path": {"type": "string"}, "claim": {"type": "integer"},
                               "verdict": {"enum": ["verified", "flagged", "inconclusive"]}, "reason": {"type": "string"}},
            }},
        },
    }


def validate_proposal(value: object, plan: dict) -> dict:
    from jsonschema import Draft202012Validator

    errors = sorted(Draft202012Validator(proposal_schema(plan)).iter_errors(value), key=lambda error: str(error.path))
    if errors:
        raise VerificationError(f"proposal schema: {errors[0].message}")
    expected = [row["scope"] for row in plan["dispatches"]]
    actual = [row["scope"] for row in value["sweeps"]]
    if sorted(actual) != sorted(expected):
        raise VerificationError("proposal omitted or repeated a planned sweep")
    staged = [(row["path"], row["claim"]) for row in plan.get("wiki_review", {}).get("claims", [])]
    reviewed = [(row["path"], row["claim"]) for row in value.get("wiki_reviews", [])]
    if len(set(reviewed)) != len(reviewed) or not set(reviewed) <= set(staged):
        raise VerificationError("wiki review repeated or invented a claim")
    for sweep in value["sweeps"]:
        if sweep["outcome"] == "envelope_returned":
            if (sweep["mode"], sweep["completion_status"]) not in {("full", "complete"), ("partial", "partial")}:
                raise VerificationError("returned sweep has inconsistent completion metadata")
            if sweep["completion_status"] == "complete" and sweep["remaining_work"].strip():
                raise VerificationError("complete sweep reports unfinished work")
        elif sweep["findings"] or sweep["completion_status"] != "aborted":
            raise VerificationError("missing envelope cannot supply accepted findings")
        for row in sweep["findings"]:
            relative(row["candidate"])
            for peer in row.get("peers", []):
                relative(peer)
    return value


def coverage_errors(proposal: dict, plan: dict) -> list[str]:
    issues = list(proposal["errors"])
    returned = sum(row["outcome"] == "envelope_returned" for row in proposal["sweeps"])
    if returned < 3:
        issues.append(f"only {returned} returned sweeps; expected at least 3")
    issues.extend(f"forgetter_no_envelope: {row['scope']}" for row in proposal["sweeps"] if row["outcome"] != "envelope_returned")
    issues.extend(plan["quarantine_skipped"])
    return issues


def sweep_report(sweep: dict) -> str:
    return f"## Decay sweep: {sweep['scope']}\n\n```json\n{json.dumps(sweep, ensure_ascii=False, sort_keys=True, indent=2)}\n```\n"


def _operation_line(op: dict) -> str:
    if "state" not in op:  # commit-era receipts re-render exactly as they were published
        return f"- {op['kind']}: {op.get('commit', {}).get('sha', 'not committed')}"
    return f"- {op['kind']} {op['state']}: {', '.join(sorted(op['paths']))}"


def render_report(record: dict) -> str:
    proposal = record["proposal"]
    lines = [f"## Autoevo Run: {record['cycle_id']}", "", f"Run ID: {record['run_id']}", "", "### Sweep coverage"]
    if "triage" in record:
        lines = [f"# Autoevo: {record['cycle_id']}", "", *lines]
    lines += [f"- {row['scope']}: {row['outcome']} ({row['mode']})" for row in proposal["sweeps"]]
    lines += ["", "### Operations"]
    lines += [_operation_line(op) for op in record["operations"] if op["kind"] != "audit"] or ["- (none)"]
    for heading, values in (
        ("Pending", record.get("pending", [])),
        ("Lint", [json.dumps(record.get("lint", {}), ensure_ascii=False, sort_keys=True)]),
        ("Notes", [*record["plan"]["notes"], *proposal["notes"], *record.get("notes", [])]),
        ("Errors", record["errors"]),
    ):
        lines += ["", f"### {heading}", *[f"- {item}" for item in values]] if values else ["", f"### {heading}", "- (none)"]
    staged = {(row["path"], row["claim"]): row for row in record["plan"].get("wiki_review", {}).get("claims", [])}
    for row in record.get("wiki_reviews", []):
        claim, reason = staged[(row["path"], row["claim"])], next(
            r["reason"] for r in proposal["wiki_reviews"] if (r["path"], r["claim"]) == (row["path"], row["claim"]))
        lines += ["", f"### Wiki review {row['verdict']}: [[{claim['title']}#^c{row['claim']}]]", f"- Reason: {reason}",
                  *(f"- {label}: {' '.join((claim[key] or '(unavailable)').split())[:300]}" for label, key in (("Before", "previous"), ("After", "current")))]
    for entry in record.get("triage", []):
        lines += ["", f"### Review {entry['category']}: {entry['id']}",
                  f"- Sources: {', '.join(entry['peers'])}",
                  f"- Proposed action: {entry['proposed_action']}",
                  f"- Evidence: {entry['evidence_summary']}"]
        if entry.get("default_action"):
            lines += [f"- Veto before {entry['default_at']}: {entry['default_action']}"]
    return "\n".join(lines) + "\n"


def needs_report(record: dict) -> bool:
    """Publish applied note changes or fresh human decisions, not routine metadata."""
    return bool(record.get("pending") or record.get("lint", {}).get("new_errors")
                or record.get("status") == "needs_review") or any(
        op["kind"] in NOTE_KINDS and op.get("state", "applied") in {"applied", "applying"}
        for op in record["operations"])


def operation_paths(operation: dict) -> dict[str, tuple[str | None, str | None]]:
    """(before_sha256, after_sha256) per path; commit-era receipts stored the after text."""
    if "paths" in operation:
        pairs = {rel: (row["before_sha256"], row["after_sha256"]) for rel, row in operation["paths"].items()}
    else:
        pairs = {rel: (row["before_sha256"], None if row["after"] is None else hashlib.sha256(row["after"].encode()).hexdigest())
                 for rel, row in operation["changes"].items()}
    if not pairs or any(pair == (None, None) or any(value is not None and not SHA256.fullmatch(value) for value in pair)
                        for pair in pairs.values()):
        raise VerificationError("operation paths are malformed")
    return {relative(rel): pair for rel, pair in pairs.items()}


def _current(vault: Path, rel: str) -> str | None:
    path = vault / rel
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def operation_state(vault: Path, operation: object) -> str:
    """Classify receipted bytes; interrupted writes block only after a change."""
    try:
        paths = operation_paths(operation)
        if not isinstance(operation["kind"], str) or not isinstance(operation["candidate_id"], str):
            raise TypeError("operation identity")
    except (AttributeError, KeyError, TypeError, VerificationError):
        return "malformed"
    if "commit" not in operation and operation.get("publication_started") is False:
        return "skipped"  # a commit-era refusal before any write
    current = {rel: _current(vault, rel) for rel in paths}
    if all(current[rel] == after for rel, (_, after) in paths.items()):
        head = tree_blobs(vault, *paths)
        written = [rel for rel, (_, after) in paths.items() if after is not None]
        try:
            committed = hash_objects(vault, written) == [head.get(rel) for rel in written]
        except RuntimeError:  # a path changed while hashing; it cannot be proven committed
            committed = False
        deleted_at_head = head.keys() - set(written)
        return "committed" if committed and not deleted_at_head else "pending-commit"
    restored = all(current[rel] == before for rel, (before, _) in paths.items())
    if operation.get("state") == "applied" or "commit" in operation:
        return "reverted" if restored else "superseded"
    return "skipped" if restored else "half-applied"


def verify_operations(vault: Path, record: dict) -> dict[str, str]:
    """Content state per operation; only half-applied or malformed operations block."""
    states = {}
    for index, operation in enumerate(record["operations"]):
        state = operation_state(vault, operation)
        if state in BLOCKING_STATES:
            raise VerificationError(f"{state} Autoevo operation {index} in {record.get('cycle_id')} needs review")
        states[operation["candidate_id"]] = state
    return states


def verify_cycle(*, vault: Path, cycle: str) -> dict:
    path = record_path(vault, cycle)
    record = read_record(path)
    if record.get("cycle_id") != cycle or record.get("status") != "complete":
        raise VerificationError("cycle has no complete domain result; inspect Prefect and the result record")
    proposal = validate_proposal(record.get("proposal"), record["plan"])
    if record.get("errors") or coverage_errors(proposal, record["plan"]):
        raise VerificationError("cycle has incomplete coverage or errors")
    lint = record.get("lint", {})
    if any(type(lint.get("counts", {}).get(key)) is not int or lint["counts"][key] < 0 for key in ("error", "warn", "info")):
        raise VerificationError("cycle has no deterministic lint result")
    if lint.get("new_errors"):
        raise VerificationError("cycle introduced lint errors")
    operations = record.get("operations", [])
    states = verify_operations(vault, record)
    if record["output_file"] == path.relative_to(vault).as_posix():
        if needs_report(record) or record["reports"] or any(op["kind"] == "audit" for op in operations):
            raise VerificationError("actionable result cannot omit its review note")
    else:
        if not operations or operations[-1].get("kind") != "audit":
            raise VerificationError("cycle has no final audit write")
        audit = operation_paths(operations[-1])
        expected_reports = {record["output_file"]: render_report(record)}
        for sweep in proposal["sweeps"]:
            if sweep["scope"] in record["reports"]:
                expected_reports[record["reports"][sweep["scope"]]] = sweep_report(sweep)
        if any(audit.get(relative(name), (None, None))[1] != hashlib.sha256(body.encode()).hexdigest()
               for name, body in expected_reports.items()):
            raise VerificationError("written report does not represent the structured result")
    return {"verified": True, "sweeps_completed": len(proposal["sweeps"]),
            "operations": states, "record_file": str(path)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycle", required=True)
    parser.add_argument("--vault", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = verify_cycle(vault=(args.vault or vault_root()).resolve(), cycle=args.cycle)
    except (VerificationError, OSError, KeyError, TypeError, ValueError) as exc:
        print(json.dumps({"verified": False, "error": str(exc)}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
