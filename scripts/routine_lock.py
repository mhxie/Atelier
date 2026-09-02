#!/usr/bin/env python3
"""Coordination backends for local routines.

The preferred single-machine backend is ``owner``: a shared vault record names
one machine identity, and every other machine stands down before execution.
Ownership can be transferred explicitly with ``routine_owner.py claim``.
The eligible owner serializes acquisition with a local per-cycle file lock and
atomically reserves the synchronized claim before returning.

The optional multi-machine backend is a DynamoDB conditional-put lock. Each
machine's launchd fires the routine on schedule, but only one runs per cycle.

Lock primitive: DynamoDB `PutItem` conditioned on `attribute_not_exists(pk)`.
Server-side atomicity removes the filesystem race window. A running item has no
DynamoDB TTL and is never taken over automatically; crash recovery is an
explicit operator decision so uncertain external effects are not repeated.
`release` marks the item completed and gives that completed marker a seven-day
TTL, which exceeds the same-cycle re-fire window.

When coordination is set to ``none``, acquire still uses a machine-local atomic
claim reservation to prevent duplicate cycles on that machine, but it does not
coordinate separate machines. The ``owner`` backend is local and needs no AWS
account.

Usage:
    # Acquire (exits 0 = acquired, 1 = held by another, 2 = error)
    routine_lock.py acquire <routine> [--cycle <id>] [--ttl 3600]

    # Release (failure is completion uncertainty, not success)
    routine_lock.py release <routine> [--cycle <id>]

    # Query
    routine_lock.py status <routine> [--cycle <id>]

    # Operator-reviewed uncertain-effect recovery
    routine_lock.py recover <routine> --cycle <id> \
        --outcome completed|safe-to-retry --confirm-effects-reviewed

    # Report the configured backend without opening a network client
    routine_lock.py backend

    # One-time table setup
    routine_lock.py setup-table
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import platform
import re
import sys
import tempfile
import time
import tomllib
from datetime import date, datetime, timezone
from pathlib import Path

import sys as _s
_s.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import retry_transient, tier_segments  # noqa: E402
from routine_owner import OwnershipError, coordination_backend, ownership_status

TABLE_NAME = "atelier-routine-locks"
TTL_DEFAULT = 3600  # 1 hour
COMPLETED_TTL = 7 * 86400  # 7 days: keep a completed marker past any re-fire window
AWS_REGION = os.environ.get("ATELIER_AWS_REGION", "us-west-2")
SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _coordination_mode() -> str:
    """Read coordination mode from env or routine_watch.toml."""
    try:
        return coordination_backend()
    except OwnershipError as exc:
        print(f"ERROR: coordination config failed: {exc}", file=sys.stderr)
        return "error"


def _get_client():
    """Get a boto3 DynamoDB client. Returns None if unavailable."""
    try:
        import boto3
    except ImportError:
        print(
            "ERROR: boto3 not installed. Run this script via `uv run` "
            "(boto3 is a project dependency; `uv sync` installs it).",
            file=sys.stderr,
        )
        return None

    try:
        return boto3.client("dynamodb", region_name=AWS_REGION)
    except Exception as exc:
        print(f"ERROR: failed to create DynamoDB client: {exc}", file=sys.stderr)
        return None


def _cycle_id(explicit: str | None) -> str:
    """Default cycle ID is today's date."""
    if explicit:
        return explicit
    return date.today().isoformat()


def _hostname() -> str:
    return platform.node() or "unknown"


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(raw_temp)
    try:
        with open(descriptor, "w", encoding="utf-8", closefd=True) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _claim_path(routine: str, cycle: str) -> Path:
    raw_ov = os.environ.get("OV", "")
    if not raw_ov:
        raise ValueError("OV is not set")
    return (
        Path(raw_ov).expanduser()
        / tier_segments().get("meta", "_meta")
        / "routine_runs"
        / routine
        / f"{cycle}.toml"
    )


def _claim_status(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        claim = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot read claim: {exc}") from exc
    status_value = claim.get("status")
    if not isinstance(status_value, str) or not status_value:
        raise ValueError("claim has no valid status")
    return status_value


def _cycle_mutex_path(claim_path: Path) -> Path:
    return claim_path.with_name(f".{claim_path.stem}.acquire.lock")


def _reserve_local_cycle(
    routine: str,
    cycle: str,
    generation: int,
) -> tuple[bool, str | None]:
    """Atomically reserve one cycle in a machine-local coordination mode."""
    claim_path = _claim_path(routine, cycle)
    claim_path.parent.mkdir(parents=True, exist_ok=True)
    mutex_path = _cycle_mutex_path(claim_path)
    with mutex_path.open("a+b") as mutex:
        retry_transient(
            lambda: fcntl.flock(mutex.fileno(), fcntl.LOCK_EX),
            what=f"lock {mutex_path.name}",
        )
        existing_status = _claim_status(claim_path)
        if existing_status not in (None, "deferred", "retry-approved"):
            return False, existing_status
        reserved_at = datetime.now(timezone.utc).isoformat()
        reservation = (
            f"routine = {json.dumps(routine)}\n"
            f"cycle_id = {json.dumps(cycle)}\n"
            f"machine = {json.dumps(_hostname())}\n"
            f"owner_generation = {generation}\n"
            f"claimed_at = {json.dumps(reserved_at)}\n"
            'status = "running"\n'
            'reservation = "owner-acquire"\n'
        )
        _atomic_write(claim_path, reservation)
        return True, existing_status


def _recover_local_claim(routine: str, cycle: str, outcome: str) -> Path:
    """Record an operator-reviewed recovery under the local cycle mutex."""
    claim_path = _claim_path(routine, cycle)
    claim_path.parent.mkdir(parents=True, exist_ok=True)
    mutex_path = _cycle_mutex_path(claim_path)
    with mutex_path.open("a+b") as mutex:
        retry_transient(
            lambda: fcntl.flock(mutex.fileno(), fcntl.LOCK_EX),
            what=f"lock {mutex_path.name}",
        )
        try:
            content = claim_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            content = (
                f"routine = {json.dumps(routine)}\n"
                f"cycle_id = {json.dumps(cycle)}\n"
                f"machine = {json.dumps(_hostname())}\n"
            )
            current_status = None
            claim_data: dict[str, object] = {}
        except OSError as exc:
            raise ValueError(f"cannot read claim: {exc}") from exc
        else:
            try:
                claim_data = tomllib.loads(content)
            except tomllib.TOMLDecodeError as exc:
                raise ValueError(f"cannot read claim: {exc}") from exc
            status_value = claim_data.get("status")
            if not isinstance(status_value, str) or not status_value:
                raise ValueError("claim has no valid status")
            current_status = status_value

        recoverable = {"running", "completion-uncertain", "failed"}
        replacement_status = (
            "completed" if outcome == "completed" else "retry-approved"
        )
        if (
            current_status == replacement_status
            and claim_data.get("recovery_outcome") == outcome
            and claim_data.get("effects_reviewed") is True
        ):
            return claim_path
        if current_status is not None and current_status not in recoverable:
            raise ValueError(
                "claim is not running, completion-uncertain, or failed"
            )

        if current_status is None:
            updated = content + f'status = "{replacement_status}"\n'
        else:
            updated, count = re.subn(
                r'(?m)^status = "(?:running|completion-uncertain|failed)"$',
                f'status = "{replacement_status}"',
                content,
                count=1,
            )
            if count != 1:
                raise ValueError("claim status could not be updated")
            if not updated.endswith("\n"):
                updated += "\n"
        updated += (
            f"recovered_at = {json.dumps(datetime.now(timezone.utc).isoformat())}\n"
            f"recovery_outcome = {json.dumps(outcome)}\n"
            "effects_reviewed = true\n"
        )
        _atomic_write(claim_path, updated)
    return claim_path


def acquire(routine: str, cycle: str | None, ttl: int) -> int:
    """Attempt to acquire the lock. Returns 0=acquired, 1=held, 2=error."""
    cycle_id = _cycle_id(cycle)
    if not SAFE_COMPONENT.fullmatch(routine) or not SAFE_COMPONENT.fullmatch(cycle_id):
        print("ERROR: unsafe routine or cycle identifier", file=sys.stderr)
        return 2
    mode = _coordination_mode()
    if mode == "none":
        try:
            reserved, existing_status = _reserve_local_cycle(
                routine,
                cycle_id,
                0,
            )
        except (OSError, ValueError) as exc:
            print(f"ERROR: local cycle reservation failed: {exc}", file=sys.stderr)
            return 2
        if not reserved:
            print(json.dumps({
                "acquired": False,
                "coordination": "none",
                "status": existing_status,
                "cycle": cycle_id,
            }))
            return 1
        print(json.dumps({
            "acquired": True,
            "coordination": "none",
            "cycle": cycle_id,
            "claim_reserved": True,
        }))
        return 0
    if mode == "owner":
        try:
            owner = ownership_status()
        except OwnershipError as exc:
            print(f"ERROR: owner check failed: {exc}", file=sys.stderr)
            return 2
        if owner["eligible"]:
            generation = owner.get("generation")
            if not isinstance(generation, int):
                print("ERROR: owner record has no valid generation", file=sys.stderr)
                return 2
            try:
                reserved, existing_status = _reserve_local_cycle(
                    routine,
                    cycle_id,
                    generation,
                )
            except (OSError, ValueError) as exc:
                print(f"ERROR: owner cycle reservation failed: {exc}", file=sys.stderr)
                return 2
            if not reserved:
                print(json.dumps({
                    "acquired": False,
                    "coordination": "owner",
                    "status": existing_status,
                    "cycle": cycle_id,
                }))
                return 1
            print(json.dumps({
                "acquired": True,
                "coordination": "owner",
                "machine": owner.get("machine_label"),
                "generation": generation,
                "cycle": cycle_id,
                "claim_reserved": True,
            }))
            return 0
        print(json.dumps({
            "acquired": False,
            "coordination": "owner",
            "held_by": owner.get("owner_label", "unknown"),
            "reason": owner.get("reason"),
            "cycle": cycle_id,
        }))
        return 1
    if mode != "dynamodb":
        print(f"ERROR: unsupported coordination backend: {mode}", file=sys.stderr)
        return 2

    client = _get_client()
    if not client:
        return 2

    pk = f"{routine}#{cycle_id}"
    now = int(time.time())
    hostname = _hostname()

    try:
        client.put_item(
            TableName=TABLE_NAME,
            Item={
                "pk": {"S": pk},
                "routine": {"S": routine},
                "cycle_id": {"S": cycle_id},
                "machine": {"S": hostname},
                "claimed_at": {"S": datetime.now(timezone.utc).isoformat()},
                "status": {"S": "running"},
                "lease_expires_at": {"N": str(now + ttl)},
            },
            ConditionExpression="attribute_not_exists(pk)",
        )
        print(json.dumps({
            "acquired": True,
            "coordination": "dynamodb",
            "machine": hostname,
            "cycle": cycle_id,
        }))
        return 0

    except client.exceptions.ConditionalCheckFailedException:
        try:
            resp = client.get_item(
                TableName=TABLE_NAME,
                Key={"pk": {"S": pk}},
            )
            item = resp.get("Item", {})
            holder = item.get("machine", {}).get("S", "unknown")
            status = item.get("status", {}).get("S", "unknown")
        except Exception as exc:
            print(f"ERROR: DynamoDB contention lookup failed: {exc}", file=sys.stderr)
            return 2

        if status == "retry-approved":
            try:
                client.update_item(
                    TableName=TABLE_NAME,
                    Key={"pk": {"S": pk}},
                    UpdateExpression=(
                        "SET #s = :running, machine = :machine, "
                        "claimed_at = :at, lease_expires_at = :lease "
                        "REMOVE #ttl, completed_at, recovery_started_at, "
                        "recovery_approved_at"
                    ),
                    ConditionExpression="#s = :retry",
                    ExpressionAttributeNames={"#s": "status", "#ttl": "ttl"},
                    ExpressionAttributeValues={
                        ":retry": {"S": "retry-approved"},
                        ":running": {"S": "running"},
                        ":machine": {"S": hostname},
                        ":at": {"S": datetime.now(timezone.utc).isoformat()},
                        ":lease": {"N": str(now + ttl)},
                    },
                )
            except client.exceptions.ConditionalCheckFailedException:
                try:
                    resp = client.get_item(
                        TableName=TABLE_NAME,
                        Key={"pk": {"S": pk}},
                    )
                    item = resp.get("Item", {})
                    holder = item.get("machine", {}).get("S", "unknown")
                    status = item.get("status", {}).get("S", "unknown")
                except Exception as exc:
                    print(
                        f"ERROR: DynamoDB retry contention lookup failed: {exc}",
                        file=sys.stderr,
                    )
                    return 2
            except Exception as exc:
                print(f"ERROR: DynamoDB retry acquire failed: {exc}", file=sys.stderr)
                return 2
            else:
                print(json.dumps({
                    "acquired": True,
                    "coordination": "dynamodb",
                    "machine": hostname,
                    "cycle": cycle_id,
                    "retry_authorized": True,
                }))
                return 0

        print(json.dumps({
            "acquired": False,
            "coordination": "dynamodb",
            "held_by": holder,
            "status": status,
            "cycle": cycle_id,
        }))
        return 1

    except Exception as exc:
        print(f"ERROR: DynamoDB put failed: {exc}", file=sys.stderr)
        return 2


def release(routine: str, cycle: str | None) -> int:
    """Release the lock (update status to completed). Returns 0=ok, 2=error."""
    mode = _coordination_mode()
    if mode == "none":
        print(json.dumps({
            "released": True,
            "coordination": "none",
            "cycle": _cycle_id(cycle),
        }))
        return 0
    if mode == "owner":
        try:
            owner = ownership_status()
        except OwnershipError as exc:
            print(f"ERROR: owner check failed: {exc}", file=sys.stderr)
            return 2
        print(json.dumps({
            "released": bool(owner["eligible"]),
            "coordination": "owner",
            "cycle": _cycle_id(cycle),
        }))
        return 0
    if mode != "dynamodb":
        print(f"ERROR: unsupported coordination backend: {mode}", file=sys.stderr)
        return 2

    client = _get_client()
    if not client:
        return 2

    cycle_id = _cycle_id(cycle)
    pk = f"{routine}#{cycle_id}"
    hostname = _hostname()

    try:
        client.update_item(
            TableName=TABLE_NAME,
            Key={"pk": {"S": pk}},
            # Extend ttl on completion so DynamoDB's background TTL GC cannot
            # delete a finished marker and let a late same-cycle re-fire
            # (attribute_not_exists(pk) becomes true) rerun the cycle.
            UpdateExpression="SET #s = :s, completed_at = :t, #ttl = :newttl",
            ConditionExpression="machine = :m",
            ExpressionAttributeNames={"#s": "status", "#ttl": "ttl"},
            ExpressionAttributeValues={
                ":s": {"S": "completed"},
                ":t": {"S": datetime.now(timezone.utc).isoformat()},
                ":m": {"S": hostname},
                ":newttl": {"N": str(int(time.time()) + COMPLETED_TTL)},
            },
        )
        print(json.dumps({"released": True, "coordination": "dynamodb", "cycle": cycle_id}))
        return 0

    except client.exceptions.ConditionalCheckFailedException:
        print(json.dumps({
            "released": False,
            "coordination": "dynamodb",
            "reason": "not_owner",
            "cycle": cycle_id,
        }))
        return 0  # not an error; another machine owns it

    except Exception as exc:
        print(f"ERROR: DynamoDB update failed: {exc}", file=sys.stderr)
        return 2


def status(routine: str, cycle: str | None) -> int:
    """Query lock status. Returns 0 always."""
    mode = _coordination_mode()
    if mode == "none":
        print(json.dumps({"coordination": "none", "routine": routine}))
        return 0
    if mode == "owner":
        try:
            owner = ownership_status()
        except OwnershipError as exc:
            print(f"ERROR: owner check failed: {exc}", file=sys.stderr)
            return 2
        print(json.dumps({
            "coordination": "owner",
            "routine": routine,
            "eligible": owner["eligible"],
            "owner_label": owner.get("owner_label"),
            "machine_label": owner.get("machine_label"),
            "generation": owner.get("generation"),
        }))
        return 0
    if mode != "dynamodb":
        print(f"ERROR: unsupported coordination backend: {mode}", file=sys.stderr)
        return 2

    client = _get_client()
    if not client:
        return 2

    cycle_id = _cycle_id(cycle)
    pk = f"{routine}#{cycle_id}"
    try:
        resp = client.get_item(
            TableName=TABLE_NAME,
            Key={"pk": {"S": pk}},
        )
        item = resp.get("Item")
        if not item:
            print(json.dumps({"coordination": "dynamodb", "exists": False, "cycle": cycle_id}))
        else:
            out = {
                "coordination": "dynamodb",
                "exists": True,
                "cycle": cycle_id,
                "machine": item.get("machine", {}).get("S"),
                "status": item.get("status", {}).get("S"),
                "claimed_at": item.get("claimed_at", {}).get("S"),
                "completed_at": item.get("completed_at", {}).get("S"),
                "lease_expires_at": item.get("lease_expires_at", {}).get("N"),
            }
            print(json.dumps({key: value for key, value in out.items() if value is not None}))
        return 0
    except Exception as exc:
        print(f"ERROR: DynamoDB get failed: {exc}", file=sys.stderr)
        return 2


def recover(routine: str, cycle: str, outcome: str, confirmed: bool) -> int:
    """Resolve an uncertain cycle only after an operator reviews its effects."""
    if not confirmed:
        print("ERROR: --confirm-effects-reviewed is required", file=sys.stderr)
        return 2
    if not SAFE_COMPONENT.fullmatch(routine) or not SAFE_COMPONENT.fullmatch(cycle):
        print("ERROR: unsafe routine or cycle identifier", file=sys.stderr)
        return 2
    mode = _coordination_mode()
    if mode in {"none", "owner"}:
        try:
            claim_path = _recover_local_claim(routine, cycle, outcome)
        except ValueError as exc:
            print(f"ERROR: local claim recovery failed: {exc}", file=sys.stderr)
            return 2
        print(json.dumps({
            "recovered": True,
            "coordination": mode,
            "outcome": outcome,
            "claim": str(claim_path),
        }))
        return 0
    if mode != "dynamodb":
        print(f"ERROR: recovery is unavailable for coordination={mode}", file=sys.stderr)
        return 2

    client = _get_client()
    if not client:
        return 2
    pk = f"{routine}#{cycle}"
    try:
        if outcome == "completed":
            try:
                client.update_item(
                    TableName=TABLE_NAME,
                    Key={"pk": {"S": pk}},
                    UpdateExpression="SET #s = :completed, completed_at = :at, #ttl = :ttl",
                    ConditionExpression="#s = :running OR #s = :recovering",
                    ExpressionAttributeNames={"#s": "status", "#ttl": "ttl"},
                    ExpressionAttributeValues={
                        ":running": {"S": "running"},
                        ":recovering": {"S": "recovery-in-progress"},
                        ":completed": {"S": "completed"},
                        ":at": {"S": datetime.now(timezone.utc).isoformat()},
                        ":ttl": {"N": str(int(time.time()) + COMPLETED_TTL)},
                    },
                )
            except client.exceptions.ConditionalCheckFailedException:
                # A retry after the remote update succeeded but the local
                # claim write failed may safely finish local reconciliation.
                response = client.get_item(
                    TableName=TABLE_NAME,
                    Key={"pk": {"S": pk}},
                )
                remote_status = (
                    response.get("Item", {}).get("status", {}).get("S")
                )
                if remote_status != "completed":
                    raise
            claim_path = _recover_local_claim(routine, cycle, outcome)
        else:
            # Two-phase safe retry: first fence the remote item, then make the
            # local claim retryable, and only then publish central retry
            # authorization. Acquire atomically consumes that authorization.
            # A crash at either intermediate step remains fail closed and the
            # same recovery command can resume it.
            client.update_item(
                TableName=TABLE_NAME,
                Key={"pk": {"S": pk}},
                UpdateExpression="SET #s = :recovering, recovery_started_at = :at",
                ConditionExpression="#s = :running OR #s = :recovering",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":running": {"S": "running"},
                    ":recovering": {"S": "recovery-in-progress"},
                    ":at": {"S": datetime.now(timezone.utc).isoformat()},
                },
            )
            claim_path = _recover_local_claim(routine, cycle, outcome)
            client.update_item(
                TableName=TABLE_NAME,
                Key={"pk": {"S": pk}},
                UpdateExpression="SET #s = :retry, recovery_approved_at = :at",
                ConditionExpression="#s = :recovering OR #s = :retry",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":recovering": {"S": "recovery-in-progress"},
                    ":retry": {"S": "retry-approved"},
                    ":at": {"S": datetime.now(timezone.utc).isoformat()},
                },
            )
        print(json.dumps({
            "recovered": True,
            "coordination": "dynamodb",
            "outcome": outcome,
            "cycle": cycle,
            "claim": str(claim_path),
        }))
        return 0
    except client.exceptions.ConditionalCheckFailedException:
        print("ERROR: lock is absent or no longer running", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"ERROR: DynamoDB recovery failed: {exc}", file=sys.stderr)
        return 2


def setup_table() -> int:
    """Create the DynamoDB table (one-time setup)."""
    client = _get_client()
    if not client:
        return 2

    try:
        client.create_table(
            TableName=TABLE_NAME,
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
            ],
            BillingMode="PROVISIONED",
            ProvisionedThroughput={
                "ReadCapacityUnits": 1,
                "WriteCapacityUnits": 1,
            },
        )
        print(f"Table '{TABLE_NAME}' created. Waiting for ACTIVE status...")

        waiter = client.get_waiter("table_exists")
        waiter.wait(TableName=TABLE_NAME)

        client.update_time_to_live(
            TableName=TABLE_NAME,
            TimeToLiveSpecification={
                "Enabled": True,
                "AttributeName": "ttl",
            },
        )
        print("TTL enabled on 'ttl' column. Table ready.")
        return 0

    except client.exceptions.ResourceInUseException:
        print(f"Table '{TABLE_NAME}' already exists.")
        return 0

    except Exception as exc:
        print(f"ERROR: table creation failed: {exc}", file=sys.stderr)
        return 2


def main() -> int:
    parser = argparse.ArgumentParser(description="Coordinate scheduled local routines.")
    sub = parser.add_subparsers(dest="command")

    acq = sub.add_parser("acquire")
    acq.add_argument("routine")
    acq.add_argument("--cycle")
    acq.add_argument("--ttl", type=int, default=TTL_DEFAULT)

    rel = sub.add_parser("release")
    rel.add_argument("routine")
    rel.add_argument("--cycle")

    st = sub.add_parser("status")
    st.add_argument("routine")
    st.add_argument("--cycle")

    recovery = sub.add_parser("recover")
    recovery.add_argument("routine")
    recovery.add_argument("--cycle", required=True)
    recovery.add_argument("--outcome", choices=("completed", "safe-to-retry"), required=True)
    recovery.add_argument("--confirm-effects-reviewed", action="store_true")

    sub.add_parser("backend")
    sub.add_parser("setup-table")

    args = parser.parse_args()

    if args.command == "acquire":
        return acquire(args.routine, args.cycle, args.ttl)
    elif args.command == "release":
        return release(args.routine, args.cycle)
    elif args.command == "status":
        return status(args.routine, args.cycle)
    elif args.command == "recover":
        return recover(
            args.routine,
            args.cycle,
            args.outcome,
            args.confirm_effects_reviewed,
        )
    elif args.command == "backend":
        mode = _coordination_mode()
        if mode == "error":
            return 2
        print(json.dumps({"coordination": mode}))
        return 0
    elif args.command == "setup-table":
        return setup_table()
    else:
        parser.print_help()
        return 2


if __name__ == "__main__":
    sys.exit(main())
