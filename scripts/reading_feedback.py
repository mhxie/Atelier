"""Typed reading evidence and offline selection evaluation; storage is decisions.py.

Agent proposals describe exposure, human ratings describe usefulness. Neither
is a claim-validity label. Reducers reject malformed or misattributed evidence.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

CLASS = "reading/item"
POLICY_CLASS = "reading/policy"
CLASSES = {CLASS, POLICY_CLASS}
ACTORS = {
    "proposed": {"agent"},
    "shown": {"agent", "observed"},
    "approved": {"human"},
    "declined": {"human"},
    "deferred": {"human"},
    "consumed": {"human", "observed"},
    "useful": {"human"},
    "not-useful": {"human"},
}
ACTIONS = {"deep-read", "digest", "archive"}
ITEM_FIELDS = ("title", "summary", "category", "url", "author", "reading_time", "word_count", "tags")


def fingerprint(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def policy_snapshot(files: list[Path], context: list[Path], model: str) -> dict:
    if not files or not model.strip():
        raise ValueError("policy needs source files and the actual model identity")
    sources = {str(p): p.read_text(encoding="utf-8") for p in files}
    contexts = {str(p): p.read_text(encoding="utf-8") for p in context}
    payload = {
        "model": model,
        "files": {p: hashlib.sha256(text.encode()).hexdigest() for p, text in sources.items()},
        "context": {p: hashlib.sha256(text.encode()).hexdigest() for p, text in contexts.items()},
        "sources": sources, "context_sources": contexts,
    }
    return {"id": fingerprint(payload), **payload}


def subject(episode: str, item: str) -> str:
    return fingerprint([episode, item])


def validate_policy(policy: Any) -> None:
    if not isinstance(policy, dict) or not isinstance(policy.get("model"), str) or not policy["model"].strip():
        raise ValueError("policy needs the actual model identity")
    if set(policy) != {"id", "model", "files", "context", "sources", "context_sources"}:
        raise ValueError("unsupported policy snapshot fields")
    for key in ("files", "context"):
        values = policy.get(key)
        if not isinstance(values, dict) or (key == "files" and not values):
            raise ValueError("policy needs source and context hash maps")
        if any(not isinstance(k, str) or not isinstance(v, str) or not v for k, v in values.items()):
            raise ValueError("invalid policy file hash")
        content = policy.get("sources" if key == "files" else "context_sources")
        if not isinstance(content, dict) or set(content) != set(values):
            raise ValueError("policy must preserve its source and context text")
        if any(not isinstance(text, str) or hashlib.sha256(text.encode()).hexdigest() != values[path]
               for path, text in content.items()):
            raise ValueError("policy content does not match its hash")
    if policy.get("id") != fingerprint({k: v for k, v in policy.items() if k != "id"}):
        raise ValueError("policy snapshot identity mismatch")


def validate_policy_record(row: dict) -> None:
    features = row.get("features")
    if (row.get("class") != POLICY_CLASS or row.get("by") != "rule"
            or row.get("verdict") != "captured" or not isinstance(features, dict)
            or set(features) != {"schema", "policy"}
            or type(features.get("schema")) is not int or features["schema"] != 1):
        raise ValueError("invalid reading policy record")
    validate_policy(features.get("policy"))
    if row.get("subject") != features["policy"]["id"]:
        raise ValueError("reading policy record identity mismatch")


def policy_index(rows: list[dict]) -> tuple[dict[str, dict], list[str]]:
    """Resolve stored snapshots and legacy inline snapshots without rewriting either."""
    policies, errors, invalid_ids = {}, [], set()
    for row in rows:
        features = row.get("features")
        if row.get("class") == POLICY_CLASS:
            policy_id = row.get("subject")
            try:
                validate_policy_record(row)
                policy = features["policy"]
            except (ValueError, TypeError) as exc:
                errors.append(str(exc))
                if isinstance(policy_id, str):
                    invalid_ids.add(policy_id)
                continue
        elif (row.get("class") == CLASS and row.get("verdict") == "proposed"
              and isinstance(features, dict) and "policy" in features):
            try:
                validate(row)
            except (ValueError, TypeError):
                continue  # The episode reader reports invalid legacy events.
            policy = features["policy"]
            policy_id = policy["id"]
        else:
            continue
        if policy_id in policies and policies[policy_id] != policy:
            errors.append("conflicting policy snapshot")
            invalid_ids.add(policy_id)
        policies[policy_id] = policy
    return {key: value for key, value in policies.items() if key not in invalid_ids}, errors


def validate(row: dict, policies: dict[str, dict] | None = None, *, resolve_policy: bool = True) -> None:
    """Validate at both the append boundary and the evidence-read boundary."""
    if row.get("class") != CLASS:
        raise ValueError("not a reading event")
    event = row.get("verdict")
    if event not in ACTORS or row.get("by") not in ACTORS[event]:
        raise ValueError("reading event has invalid provenance")
    if not isinstance(row.get("reason"), str) or not row["reason"].strip():
        raise ValueError("reading event needs an attributable reason")
    f = row.get("features")
    if not isinstance(f, dict) or type(f.get("schema")) is not int or f["schema"] != 1:
        raise ValueError("reading event needs schema 1 features")
    allowed = {"schema", "episode_id", "item_id", "policy_id", "event_id", "evidence_ref"}
    if event == "proposed":
        allowed.update(("action", "item", "policy"))
    if set(f) - allowed:
        raise ValueError("unsupported reading event features")
    for key in ("episode_id", "item_id", "policy_id", "event_id", "evidence_ref"):
        if not isinstance(f.get(key), str) or not f[key].strip():
            raise ValueError(f"reading event needs {key}")
    if row.get("subject") != subject(f["episode_id"], f["item_id"]):
        raise ValueError("reading subject does not match episode/item")
    if event == "proposed":
        if f.get("action") not in ACTIONS:
            raise ValueError("proposal needs a valid action")
        item = f.get("item")
        if not isinstance(item, dict) or not item.get("title"):
            raise ValueError("proposal needs item metadata including title")
        for k, v in item.items():
            if k not in ITEM_FIELDS:
                raise ValueError("unsupported item metadata")
            if k in {"reading_time", "word_count"}:
                if type(v) not in (int, float) or not 0 <= v < float("inf"):
                    raise ValueError("item duration/count must be a finite nonnegative number")
            elif k == "tags":
                if not isinstance(v, list) or any(not isinstance(tag, str) for tag in v):
                    raise ValueError("item tags must be a list of strings")
            elif not isinstance(v, str):
                raise ValueError("item text metadata must be strings")
        if "policy" in f or resolve_policy:
            policy = f.get("policy") if "policy" in f else (policies or {}).get(f["policy_id"])
            validate_policy(policy)
            if f["policy_id"] != policy["id"]:
                raise ValueError("proposal policy identity mismatch")


def event_identity(row: dict) -> dict:
    """Retries ignore append time and inline-vs-referenced snapshot storage."""
    return {**{k: v for k, v in row.items() if k not in {"ts", "features"}},
            "features": {k: v for k, v in row["features"].items() if k != "policy"}}


def episodes(rows: list[dict]) -> dict:
    policies, policy_errors = policy_index(rows)
    groups: dict[str, list[dict]] = {}
    seen: dict[str, dict] = {}
    invalid: list[str] = list(policy_errors)
    poisoned: set[str] = set()
    for row in rows:
        if row.get("class") != CLASS:
            continue
        try:
            validate(row, policies)
            if row["verdict"] == "proposed" and row["features"]["policy_id"] not in policies:
                raise ValueError("missing or conflicting policy snapshot")
        except (ValueError, TypeError) as exc:
            invalid.append(str(exc))
            continue
        eid = row["features"]["event_id"]
        if eid in seen:
            # Append retries may have a different timestamp, but not new content.
            previous = event_identity(seen[eid])
            current = event_identity(row)
            if previous != current:
                poisoned.update((row["subject"], seen[eid]["subject"]))
                invalid.append("conflicting event identity")
            continue
        seen[eid] = row
        groups.setdefault(row["subject"], []).append(row)
    result = []
    for key, events in groups.items():
        proposals = [r for r in events if r["verdict"] == "proposed"]
        policy_ids = {r["features"]["policy_id"] for r in events}
        proposal_shapes = {fingerprint(r["features"]["item"]) + r["features"]["action"] for r in proposals}
        if key in poisoned or len(policy_ids) != 1 or len(proposal_shapes) > 1:
            invalid.append("conflicting episode attribution")
            continue
        # Ledger append order is the event order. A later explicit rating corrects
        # an earlier one; approval or deferral cannot erase that rating.
        ratings = [r for r in events if r["verdict"] in {"useful", "not-useful"}]
        proposal = proposals[0] if proposals else None
        result.append({
            "id": key, "episode_id": events[0]["features"]["episode_id"],
            "item_id": events[0]["features"]["item_id"], "policy_id": next(iter(policy_ids)),
            "proposal": proposal, "events": events, "rating": ratings[-1] if ratings else None,
            "attributed": proposal is not None,
        })
    return {"episodes": result, "invalid_count": len(invalid), "errors": invalid[:10]}


def evidence(rows: list[dict], limit: int = 50, item: str | None = None) -> dict:
    if limit < 1:
        raise ValueError("evidence limit must be positive")
    reduced = episodes(rows)
    positions = {}
    for i, row in enumerate(rows):
        features = row.get("features")
        if isinstance(features, dict) and isinstance(features.get("event_id"), str):
            positions.setdefault(features["event_id"], i)
    explicit, consumption = [], []
    for ep in reduced["episodes"]:
        if item is not None and ep["item_id"] != item:
            continue
        context = {k: ep[k] for k in ("episode_id", "item_id", "policy_id", "attributed")}
        if ep["proposal"]:
            context["item"] = ep["proposal"]["features"]["item"]
            context["proposed_action"] = ep["proposal"]["features"]["action"]
        for event in ep["events"]:
            if event["verdict"] in {"proposed", "shown"}:
                continue
            if event["verdict"] in {"useful", "not-useful"} and event is not ep["rating"]:
                continue
            output = {**context, "event": event["verdict"], "by": event["by"],
                      "reason": event["reason"], "evidence_ref": event["features"]["evidence_ref"]}
            (consumption if event["verdict"] == "consumed" else explicit).append(
                (positions[event["features"]["event_id"]], output))
    return {"explicit_feedback": [r for _, r in sorted(explicit)[-limit:]],
            "consumption": [r for _, r in sorted(consumption)[-limit:]],
            "omitted": max(0, len(explicit) - limit) + max(0, len(consumption) - limit),
            "invalid_count": reduced["invalid_count"], "errors": reduced["errors"]}


def attribution(rows: list[dict], item: str, limit: int = 10) -> dict:
    if limit < 1:
        raise ValueError("episode limit must be positive")
    reduced = episodes(rows)
    matches = [{"episode_id": ep["episode_id"], "item_id": ep["item_id"],
                "policy_id": ep["policy_id"], "action": ep["proposal"]["features"]["action"],
                "events": sorted({r["verdict"] for r in ep["events"]})}
               for ep in reduced["episodes"] if ep["item_id"] == item and ep["attributed"]]
    return {"episodes": matches[-limit:], "total": len(matches),
            "omitted": max(0, len(matches) - limit), "invalid_count": reduced["invalid_count"],
            "interpretation": "attribution only; selection is not preference evidence"}


def outcomes(rows: list[dict]) -> dict:
    reduced = episodes(rows)
    policies: dict[str, dict] = {}
    for ep in reduced["episodes"]:
        if not ep["attributed"]:
            continue
        counts = policies.setdefault(ep["policy_id"], {
            "proposed": 0, "shown": 0, "approved": 0, "consumed": 0, "rated": 0, "useful": 0,
            "by_action": {},
        })
        kinds = {r["verdict"] for r in ep["events"]}
        for key in ("proposed", "shown", "approved", "consumed"):
            counts[key] += int(key in kinds)
        counts["rated"] += int(ep["rating"] is not None)
        counts["useful"] += int(ep["rating"] is not None and ep["rating"]["verdict"] == "useful")
        action = counts["by_action"].setdefault(ep["proposal"]["features"]["action"],
                                                {"proposed": 0, "rated": 0, "useful": 0})
        action["proposed"] += 1
        action["rated"] += int(ep["rating"] is not None)
        action["useful"] += int(ep["rating"] is not None and ep["rating"]["verdict"] == "useful")
    for counts in policies.values():
        counts["unknown"] = counts["proposed"] - counts["rated"]
        counts["feedback_coverage"] = counts["rated"] / counts["proposed"]
        counts["candidate_useful_rate"] = counts["useful"] / counts["rated"] if counts["rated"] else None
        for action in counts["by_action"].values():
            action["unknown"] = action["proposed"] - action["rated"]
            action["useful_rate"] = action["useful"] / action["rated"] if action["rated"] else None
    return {"interpretation": "candidate usefulness by proposed action; different pools are not a causal comparison",
            "policies": policies, "invalid_count": reduced["invalid_count"], "errors": reduced["errors"]}


def dataset(rows: list[dict]) -> tuple[dict, dict]:
    """Freeze rated episodes. Labels and previous selection reasons stay private."""
    reduced = episodes(rows)
    cases, labels, attribution = [], [], {}
    policies, _ = policy_index(rows)
    for ep in reduced["episodes"]:
        if ep["attributed"] and ep["rating"]:
            cases.append({"id": ep["id"], "item_id": ep["item_id"],
                          **ep["proposal"]["features"]["item"]})
            labels.append({"id": ep["id"], "useful": ep["rating"]["verdict"] == "useful"})
            attribution[ep["id"]] = ep["policy_id"]
    # Dataset identity includes labels: a correction invalidates old predictions.
    version = fingerprint([cases, labels])
    common = {"schema": 1, "case_set_id": version, "invalid_count": reduced["invalid_count"]}
    return {**common, "cases": cases}, {
        **common, "labels": labels, "attribution": attribution,
        "policies": {key: policies[key] for key in sorted(set(attribution.values()))},
    }


def compare(labels: dict, baseline: dict, candidate: dict) -> dict:
    """Score blind selections on an identical frozen set of human-rated episodes."""
    if labels.get("invalid_count", 0):
        raise ValueError("repair invalid reading evidence before evaluation")
    if not isinstance(labels.get("case_set_id"), str) or not labels["case_set_id"]:
        raise ValueError("labels need a case set identity")
    gold = labels.get("labels")
    if not isinstance(gold, list) or not gold:
        raise ValueError("no explicit usefulness labels; improvement is unknown")
    expected: dict[str, bool] = {}
    for row in gold:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or type(row.get("useful")) is not bool:
            raise ValueError("invalid reading label")
        if row["id"] in expected:
            raise ValueError("duplicate reading label")
        expected[row["id"]] = row["useful"]
    results = []
    for run in (baseline, candidate):
        validate_policy(run.get("policy"))
        if run.get("policy_id") != run["policy"]["id"]:
            raise ValueError("prediction policy identity mismatch")
        if run.get("case_set_id") != labels.get("case_set_id") or not run.get("policy_id"):
            raise ValueError("prediction needs matching case set and policy identity")
        predictions = run.get("predictions")
        if not isinstance(predictions, list):
            raise ValueError("predictions must be a list")
        selected: set[str] = set()
        seen: set[str] = set()
        for row in predictions:
            if not isinstance(row, dict) or row.get("id") not in expected or row["id"] in seen or type(row.get("selected")) is not bool:
                raise ValueError("invalid, duplicate, or unknown prediction")
            seen.add(row["id"])
            if row["selected"]:
                selected.add(row["id"])
        if seen != set(expected):
            raise ValueError("predictions must cover every case exactly once")
        useful = {key for key, value in expected.items() if value}
        hits = len(selected & useful)
        results.append({
            "policy_id": run["policy_id"], "cases": len(expected), "selected": len(selected),
            "policy": run["policy"],
            "useful_selected": hits, "noise_selected": len(selected - useful),
            "useful_missed": len(useful - selected),
            "precision": hits / len(selected) if selected else None,
            "recall": hits / len(useful) if useful else None,
        })
    if baseline["policy_id"] == candidate["policy_id"]:
        raise ValueError("baseline and candidate must identify different policies")
    return {"case_set_id": labels["case_set_id"], "baseline": results[0], "candidate": results[1],
            "decision": "review_required", "interpretation": "offline selection against explicit feedback; verify prospectively"}
