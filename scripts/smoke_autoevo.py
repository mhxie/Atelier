"""smoke_autoevo.py: the autoevo nightly contract smoke check.

Split out of harness_smoke.py; harness_smoke.py re-exports every name so callers and tests are unchanged.
"""

from __future__ import annotations

import json
import os
import plistlib
import subprocess
import tempfile
import tomllib
from datetime import date, datetime, timedelta
from pathlib import Path
import cues
import autoevo_preflight
import autoevo_quarantine
import autoevo_verify
import command_timeout
import routine_claim
import routine_lock
import routine_result

import _paths
from smoke_common import (  # noqa: E402
    ROOT,
    SmokeFailure,
    expect,
)


def check_autoevo_reliability() -> None:
    plist_path = ROOT / "scripts" / "launchd" / "com.atelier.autoevo-nightly.plist"
    plist = plistlib.loads(plist_path.read_bytes())
    expect(
        plist.get("RunAtLoad") is True, "autoevo does not catch up on login or reload"
    )
    intervals = plist.get("StartCalendarInterval")
    expect(
        isinstance(intervals, dict)
        and intervals.get("Minute") == 0
        and "Hour" not in intervals,
        "autoevo must check deferred or missed cycles at the top of every hour",
    )

    class SleepingProcess:
        args = ["fixture"]

        def poll(self) -> None:
            return None

    clock = [100.0]

    def jump_after_sleep(_: float) -> None:
        clock[0] = 1000.0

    try:
        command_timeout.wait_until_deadline(
            SleepingProcess(), 110.0, now=lambda: clock[0], sleep=jump_after_sleep
        )
    except subprocess.TimeoutExpired:
        pass
    else:
        raise SmokeFailure("command timeout ignored a wall-clock jump across sleep")

    captured_semantic_env: dict[str, str] = {}
    original_preflight_run = autoevo_preflight._run

    def capture_semantic_run(
        command: list[str],
        *,
        cwd: Path,
        timeout: float = 30,
        env: dict[str, str] | None = None,
    ) -> autoevo_preflight.CommandResult:
        del command, cwd, timeout
        captured_semantic_env.update(env or {})
        return autoevo_preflight.CommandResult(0, "[]", "real mode: fixture")

    autoevo_preflight._run = capture_semantic_run
    try:
        semantic_readiness = autoevo_preflight._default_semantic_probe()
    finally:
        autoevo_preflight._run = original_preflight_run
    expect(
        semantic_readiness["ready"] is True
        and captured_semantic_env.get("HF_HUB_OFFLINE") == "1"
        and captured_semantic_env.get("TRANSFORMERS_OFFLINE") == "1",
        "autoevo semantic readiness probe can still attempt a model download",
    )

    with tempfile.TemporaryDirectory(prefix="atelier-autoevo-quarantine-") as temp_dir:
        temp = Path(temp_dir)
        state = temp / "autoevo_quarantine.toml"
        outcomes = temp / "outcomes.json"
        count_file = temp / "count.txt"
        state.write_text(
            "[[quarantine]]\n"
            'scope = "/expired"\n'
            'first_failed = "2098-12-01"\n'
            "consecutive_failures = 3\n"
            'reason = "forgetter_no_envelope"\n'
            'expires_at = "2099-01-02"\n',
            encoding="utf-8",
        )
        outcomes.write_text(
            json.dumps({"/expired": "forgetter_no_envelope"}),
            encoding="utf-8",
        )
        crossed = autoevo_quarantine.update_state(
            outcomes_path=outcomes,
            state_path=state,
            count_path=count_file,
            today=date(2099, 1, 2),
        )
        restarted = tomllib.loads(state.read_text(encoding="utf-8"))["quarantine"][0]
        expect(
            crossed == 0
            and count_file.read_text(encoding="utf-8").strip() == "0"
            and restarted["consecutive_failures"] == 1
            and restarted["first_failed"] == "2099-01-02"
            and restarted["expires_at"] == "2099-02-01",
            "post-expiry quarantine failure did not restart at one",
        )

        state.write_text(
            "[[quarantine]]\n"
            'scope = "/active"\n'
            'first_failed = "2099-01-01"\n'
            "consecutive_failures = 2\n"
            'reason = "forgetter_no_envelope"\n'
            'expires_at = "2099-02-01"\n',
            encoding="utf-8",
        )
        outcomes.write_text(
            json.dumps({"/active": "forgetter_no_envelope"}),
            encoding="utf-8",
        )
        crossed = autoevo_quarantine.update_state(
            outcomes_path=outcomes,
            state_path=state,
            count_path=count_file,
            today=date(2099, 1, 2),
        )
        active = tomllib.loads(state.read_text(encoding="utf-8"))["quarantine"][0]
        expect(
            crossed == 1 and active["consecutive_failures"] == 3,
            "quarantine threshold transition count drift",
        )

        boundary_state = temp / "boundary-quarantine.toml"
        boundary_state.write_text(
            "[[quarantine]]\n"
            'scope = "/boundary"\n'
            'first_failed = "2098-12-01"\n'
            "consecutive_failures = 3\n"
            'reason = "forgetter_no_envelope"\n'
            'expires_at = "2099-01-02"\n',
            encoding="utf-8",
        )
        expect(
            autoevo_quarantine.active_scopes(
                state_path=boundary_state,
                today=date(2099, 1, 1),
            )
            == ["/boundary"]
            and autoevo_quarantine.active_scopes(
                state_path=boundary_state,
                today=date(2099, 1, 2),
            )
            == [],
            "quarantine expiry does not follow the selected routine cycle date",
        )

        missing_outcomes = temp / "missing-outcomes.json"
        try:
            autoevo_quarantine.update_state(
                outcomes_path=missing_outcomes,
                state_path=state,
                count_path=count_file,
                today=date(2099, 1, 2),
            )
        except autoevo_quarantine.QuarantineError:
            pass
        else:
            raise SmokeFailure("quarantine update accepted a missing outcomes sidecar")

        malformed_state = temp / "malformed-quarantine.toml"
        malformed_state.write_text(
            "[[quarantine]]\n"
            'scope = "/malformed"\n'
            'first_failed = "not-a-date"\n'
            "consecutive_failures = 1\n"
            'reason = "forgetter_no_envelope"\n'
            'expires_at = "also-not-a-date"\n',
            encoding="utf-8",
        )
        try:
            autoevo_quarantine.update_state(
                outcomes_path=outcomes,
                state_path=malformed_state,
                count_path=count_file,
                today=date(2099, 1, 2),
            )
        except autoevo_quarantine.QuarantineError:
            pass
        else:
            raise SmokeFailure("quarantine update accepted malformed ISO dates")

        state_before_failed_write = state.read_text(encoding="utf-8")
        write_order: list[Path] = []
        original_quarantine_write = autoevo_quarantine._atomic_write

        def fail_authoritative_state_write(path: Path, text: str) -> None:
            write_order.append(path)
            if path == state:
                raise OSError("fixture state write failure")
            original_quarantine_write(path, text)

        autoevo_quarantine._atomic_write = fail_authoritative_state_write
        try:
            try:
                autoevo_quarantine.update_state(
                    outcomes_path=outcomes,
                    state_path=state,
                    count_path=count_file,
                    today=date(2099, 1, 2),
                )
            except OSError:
                pass
            else:
                raise SmokeFailure(
                    "quarantine update hid an authoritative write failure"
                )
        finally:
            autoevo_quarantine._atomic_write = original_quarantine_write
        expect(
            write_order == [count_file, state]
            and state.read_text(encoding="utf-8") == state_before_failed_write,
            "quarantine partial write advanced authoritative state before count evidence",
        )

        cleanup_target = temp / "cleanup-state.toml"
        cleanup_temporary = cleanup_target.with_name(
            f".{cleanup_target.name}.{os.getpid()}.tmp"
        )
        original_replace = autoevo_quarantine.os.replace

        def fail_replace(source: Path, destination: Path) -> None:
            del source, destination
            raise OSError("fixture replace failure")

        autoevo_quarantine.os.replace = fail_replace
        try:
            try:
                autoevo_quarantine._atomic_write(cleanup_target, "state\n")
            except OSError:
                pass
            else:
                raise SmokeFailure("quarantine atomic write hid a replace failure")
        finally:
            autoevo_quarantine.os.replace = original_replace
        expect(
            not cleanup_temporary.exists(),
            "quarantine atomic write left a hidden temporary file after failure",
        )

        audit = temp / "audit.md"
        skipped = temp / "quarantine-skipped.txt"
        audit.write_text(
            "## Autoevo Run: 2099-01-01 05:00\n\n"
            "### Skipped (reason)\n"
            "- older-run-entry\n\n"
            "### Errors\n"
            "- (none)\n\n"
            "## Autoevo Run: 2099-01-02 05:00\n\n"
            "### Skipped (reason)\n"
            "- (none)\n\n"
            "### Errors\n"
            "- (none)\n",
            encoding="utf-8",
        )
        skipped.write_text(
            "scope_quarantined: scope=/active (research-tier rotation)\n",
            encoding="utf-8",
        )
        inserted = autoevo_quarantine.insert_skipped(
            audit_path=audit,
            skipped_path=skipped,
        )
        after_first_insert = audit.read_text(encoding="utf-8")
        inserted_again = autoevo_quarantine.insert_skipped(
            audit_path=audit,
            skipped_path=skipped,
        )
        after_second_insert = audit.read_text(encoding="utf-8")
        latest_run = autoevo_verify._latest_run(after_second_insert)
        skipped_section = autoevo_verify._section(latest_run, "Skipped")
        errors_section = autoevo_verify._section(latest_run, "Errors")
        expect(
            inserted
            and not inserted_again
            and after_first_insert == after_second_insert
            and after_second_insert.count(
                "scope_quarantined: scope=/active (research-tier rotation)"
            )
            == 1
            and "older-run-entry"
            in after_second_insert.split("## Autoevo Run: 2099-01-02", maxsplit=1)[0]
            and "scope_quarantined: scope=/active" in skipped_section
            and "scope_quarantined" not in errors_section,
            "quarantine skip evidence was misplaced, duplicated, or rewrote history",
        )

    with tempfile.TemporaryDirectory(prefix="atelier-autoevo-preflight-") as temp_dir:
        vault = Path(temp_dir) / "vault"
        vault.mkdir()
        for segment in (
            "cache",
            "agent-findings",
            "wip",
            "research",
            "reflections",
            "_meta",
        ):
            (vault / segment).mkdir()

        def git(*args: str) -> subprocess.CompletedProcess[str]:
            result = subprocess.run(
                ["git", *args],
                cwd=vault,
                capture_output=True,
                text=True,
            )
            expect(
                result.returncode == 0,
                f"autoevo fixture git {' '.join(args)} failed: {result.stderr}",
            )
            return result

        git("init", "-q")
        git("config", "user.name", "Atelier Smoke")
        git("config", "user.email", "smoke@example.invalid")
        git("config", "commit.gpgsign", "false")
        (vault / ".gitignore").write_text("cache/\n_meta/\n", encoding="utf-8")
        note = vault / "wip" / "note.md"
        note.write_text("base\n", encoding="utf-8")
        git("add", ".")
        git("commit", "-q", "-m", "base")

        original_ov = os.environ.get("OV")
        os.environ["OV"] = str(vault)
        _paths.vault_root.cache_clear()
        _paths._registry.cache_clear()
        try:

            def privacy_probe() -> dict[str, object]:
                return {"hit_count": 0}

            def semantic_probe() -> dict[str, object]:
                return {
                    "ready": True,
                    "mode": "real",
                    "duration_seconds": 0.01,
                }

            session_lock = vault / "cache" / "atelier-session-lock"
            original_run = autoevo_preflight._run
            status_commands: list[list[str]] = []

            def capture_status(command: list[str], **kwargs: object):
                status_commands.append(command)
                return original_run(command, **kwargs)

            autoevo_preflight._run = capture_status
            clean = autoevo_preflight.inspect_preflight(
                vault=vault,
                lock_path=session_lock,
                privacy_probe=privacy_probe,
                semantic_probe=semantic_probe,
            )
            autoevo_preflight._run = original_run
            expect(clean["ready"] is True, f"clean autoevo fixture blocked: {clean}")
            expect(
                any(
                    command[1:3] == ["--no-optional-locks", "status"]
                    for command in status_commands
                ),
                "autoevo status probe may create an optional Git index lock",
            )

            raw_index = git("rev-parse", "--git-path", "index").stdout.strip()
            index_path = Path(raw_index)
            if not index_path.is_absolute():
                index_path = vault / index_path
            index_path.unlink()
            missing = autoevo_preflight.inspect_preflight(
                vault=vault,
                lock_path=session_lock,
                privacy_probe=privacy_probe,
                semantic_probe=semantic_probe,
            )
            expect(
                missing["gate"] == "git_index_missing",
                "autoevo misclassified a missing index as an ordinary dirty tree",
            )
            expect(
                missing["health"]["worktree_entries"] is None,
                "autoevo ran git status after detecting a missing index",
            )
            for invalid_run_date, invalid_cycle in (
                ("not-a-date", "2099-01-02"),
                ("2099-01-03", "2099-01-02"),
            ):
                try:
                    autoevo_preflight.record_blocker(
                        missing,
                        run_date=invalid_run_date,
                        run_ts="smoke-invalid-run-identity",
                        cycle=invalid_cycle,
                    )
                except autoevo_preflight.PreflightError:
                    pass
                else:
                    raise SmokeFailure(
                        "autoevo preflight accepted a noncanonical run identity"
                    )
            expect(
                not (
                    vault / "agent-findings" / "autoevo-applied-not-a-date.md"
                ).exists()
                and not (
                    vault / "agent-findings" / "autoevo-applied-2099-01-03.md"
                ).exists(),
                "invalid preflight identity created an audit artifact",
            )
            recorded = autoevo_preflight.record_blocker(
                missing,
                run_date="2099-01-02",
                run_ts="smoke-missing-index",
                cycle="2099-01-02",
            )
            expect(
                recorded["audit_commit"] == "deferred",
                "missing-index audit should remain checksum-owned until Git recovers",
            )
            git("read-tree", "HEAD")
            recovery = autoevo_preflight.recover_owned_audit()
            expect(
                recovery["status"] == "committed",
                f"managed audit did not recover after index repair: {recovery}",
            )
            expect(
                git("status", "--porcelain").stdout == "",
                "managed audit recovery left the clean fixture dirty",
            )

            raw_lock = git("rev-parse", "--git-path", "index.lock").stdout.strip()
            index_lock = Path(raw_lock)
            if not index_lock.is_absolute():
                index_lock = vault / index_lock
            index_lock.touch()
            locked = autoevo_preflight.inspect_preflight(
                vault=vault,
                lock_path=session_lock,
                privacy_probe=privacy_probe,
                semantic_probe=semantic_probe,
            )
            expect(
                locked["gate"] == "git_index_lock_present",
                "autoevo did not diagnose a Git index lock precisely",
            )
            first_locked_record = autoevo_preflight.record_blocker(
                locked,
                run_date="2099-01-03",
                run_ts="smoke-locked-first",
                cycle="2099-01-03",
            )
            repeated_locked_record = autoevo_preflight.record_blocker(
                locked,
                run_date="2099-01-03",
                run_ts="smoke-locked-repeat",
                cycle="2099-01-03",
            )
            expect(
                first_locked_record["audit_commit"] == "deferred"
                and repeated_locked_record["audit_commit"] == "reused",
                "unchanged index-lock blocker audit was not reusable on retry",
            )
            index_lock.unlink()
            recovery = autoevo_preflight.recover_owned_audit()
            expect(
                recovery["status"] == "committed",
                f"index-lock blocker audit did not recover: {recovery}",
            )

            note.write_text("dirty\n", encoding="utf-8")
            dirty = autoevo_preflight.inspect_preflight(
                vault=vault,
                lock_path=session_lock,
                now=1_000,
                privacy_probe=privacy_probe,
                semantic_probe=semantic_probe,
            )
            # A dirty note the user is editing no longer stops the sweep; it
            # becomes untouchable for the run and is refused at commit time.
            # (2026-08-29: the scope gate blocked every run after a work day.)
            expect(
                dirty["ready"] is True
                and dirty["health"]["worktree_entries"] == 1
                and dirty["health"]["worktree_entries_in_scope"] == 1
                and dirty["health"]["protected_paths"],
                f"in-scope content dirt must protect, not block: {dirty.get('gate')}",
            )
            # Autoevo's own queue state is different: dirty there means the
            # queue condition is unknown, so the run must not start.
            # Production tracks `_meta/autoevo_*.toml`; this fixture ignores
            # `_meta/`, so force-track it or the state gate can never fire here.
            state_file = vault / "_meta" / "autoevo_pending.toml"
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text("# base\n", encoding="utf-8")
            git("add", "-f", "--", "_meta/autoevo_pending.toml")
            git("commit", "-q", "-m", "track autoevo state")
            state_file.write_text("# smoke\n", encoding="utf-8")
            state_dirty = autoevo_preflight.inspect_preflight(
                vault=vault,
                lock_path=session_lock,
                now=1_000,
                privacy_probe=privacy_probe,
                semantic_probe=semantic_probe,
            )
            expect(
                state_dirty["gate"] == "dirty_autoevo_state",
                f"dirty autoevo state must block: {state_dirty.get('gate')}",
            )
            state_file.write_text("# base\n", encoding="utf-8")
            # Dirt outside the sweep scopes must not block: the bot stages
            # explicit paths, so user edits elsewhere cannot leak into its
            # commits. (2026-08-22: a vault-wide gate blocked 73 of 103 runs.)
            (vault / "personal").mkdir(exist_ok=True)
            stray = vault / "personal" / "stray.md"
            stray.write_text("user edit in progress\n", encoding="utf-8")
            note.write_text("base\n", encoding="utf-8")
            out_of_scope = autoevo_preflight.inspect_preflight(
                vault=vault,
                lock_path=session_lock,
                now=1_000,
                privacy_probe=privacy_probe,
                semantic_probe=semantic_probe,
            )
            expect(
                out_of_scope["ready"] is True
                and out_of_scope["health"]["worktree_entries"] == 1
                and out_of_scope["health"]["worktree_entries_in_scope"] == 0,
                f"out-of-scope dirt must not block autoevo: {out_of_scope.get('gate')}",
            )
            stray.unlink()
            note.write_text("base\n", encoding="utf-8")
            state_file.write_text("# dirty\n", encoding="utf-8")
            dirty = autoevo_preflight.inspect_preflight(
                vault=vault,
                lock_path=session_lock,
                now=1_000,
                privacy_probe=privacy_probe,
                semantic_probe=semantic_probe,
            )
            expect(
                dirty["retry_after_epoch"]
                == 1_000 + autoevo_preflight.GENERIC_RETRY_DELAY_SECONDS,
                "non-session autoevo blocker did not retry on the next hourly check",
            )
            first_dirty_record = autoevo_preflight.record_blocker(
                dirty,
                run_date="2099-01-02",
                run_ts="smoke-dirty-first",
                cycle="2099-01-02",
            )
            expect(
                first_dirty_record["audit_commit"] == "committed",
                "first dirty-tree blocker audit was not committed path-locally",
            )
            blocker_audit = vault / "agent-findings" / "autoevo-applied-2099-01-02.md"
            blocker_text = blocker_audit.read_text(encoding="utf-8")
            blocker_head = git("rev-parse", "HEAD").stdout.strip()
            repeated_dirty_record = autoevo_preflight.record_blocker(
                dirty,
                run_date="2099-01-02",
                run_ts="smoke-dirty-repeat",
                cycle="2099-01-02",
            )
            expect(
                repeated_dirty_record["audit_commit"] == "reused"
                and blocker_audit.read_text(encoding="utf-8") == blocker_text
                and git("rev-parse", "HEAD").stdout.strip() == blocker_head,
                "identical deferred blocker produced a duplicate audit commit",
            )
            with blocker_audit.open("a", encoding="utf-8") as handle:
                handle.write("\nuser-owned audit edit\n")
            user_edited_audit = blocker_audit.read_text(encoding="utf-8")
            dirty_audit_record = autoevo_preflight.record_blocker(
                dirty,
                run_date="2099-01-02",
                run_ts="smoke-dirty-audit",
                cycle="2099-01-02",
            )
            committed_audit = git(
                "show",
                f"HEAD:{blocker_audit.relative_to(vault).as_posix()}",
            ).stdout
            expect(
                dirty_audit_record["audit_commit"] == "deferred"
                and "audit path already had uncommitted changes"
                in dirty_audit_record["audit_commit_detail"]
                and blocker_audit.read_text(encoding="utf-8") == user_edited_audit
                and "user-owned audit edit" not in committed_audit
                and git("rev-parse", "HEAD").stdout.strip() == blocker_head,
                "blocked autoevo run absorbed a pre-existing user audit edit",
            )
            blocker_audit.write_text(blocker_text, encoding="utf-8")
            note.write_text("base\n", encoding="utf-8")
            state_file.write_text("# base\n", encoding="utf-8")

            session_lock.touch()
            active = autoevo_preflight.inspect_preflight(
                vault=vault,
                lock_path=session_lock,
                now=session_lock.stat().st_mtime,
                privacy_probe=privacy_probe,
                semantic_probe=semantic_probe,
            )
            expect(
                active["gate"] == "session_active",
                "autoevo session safety lock classification drift",
            )
            expect(
                active["retry_after_epoch"]
                == int(session_lock.stat().st_mtime)
                + autoevo_preflight.SESSION_LOCK_TTL_SECONDS
                + 1,
                "session-active retry does not align with lock expiry",
            )
            session_lock.unlink()
            semantic_blocked = autoevo_preflight.inspect_preflight(
                vault=vault,
                lock_path=session_lock,
                privacy_probe=privacy_probe,
                semantic_probe=lambda: {
                    "ready": False,
                    "mode": "real",
                    "duration_seconds": 0.02,
                    "detail": "fixture semantic failure",
                },
            )
            expect(
                semantic_blocked["gate"] == "semantic_unavailable"
                and semantic_blocked["health"]["semantic_ready"] is False,
                "autoevo did not fail closed on unavailable semantic search",
            )

            audit = vault / "agent-findings" / "autoevo-applied-2099-01-02.md"
            with audit.open("a", encoding="utf-8") as handle:
                handle.write(
                    "\n## Autoevo Run: 2099-01-02 11:00\n\n"
                    "### Skipped (reason)\n"
                    "- (none)\n\n"
                    "### Errors\n"
                    "- (none)\n"
                )
            cue, debug = cues.check_autoevo_ran(
                vault,
                date(2099, 1, 2),
                now=datetime(2099, 1, 2, 12, 0),
            )
            expect(
                cue is None,
                f"successful same-day retry did not supersede an earlier skip: {debug}",
            )

            deferred_claim = (
                vault
                / "_meta"
                / "routine_runs"
                / "autoevo-nightly"
                / "retry-fixture.toml"
            )
            deferred_claim.parent.mkdir(parents=True, exist_ok=True)
            deferred_claim.write_text(
                'routine = "autoevo-nightly"\n'
                'cycle_id = "retry-fixture"\n'
                'status = "deferred"\n',
                encoding="utf-8",
            )
            reserved, previous = routine_lock._reserve_local_cycle(
                "autoevo-nightly", "retry-fixture", 1
            )
            expect(
                reserved and previous == "deferred",
                "deferred autoevo claim was not safely reacquired",
            )
            deferred_claim.write_text(
                'routine = "autoevo-nightly"\n'
                'cycle_id = "retry-fixture"\n'
                'status = "deferred"\n'
                "retry_after_epoch = 200\n",
                encoding="utf-8",
            )
            waiting = routine_claim.schedule_decision(
                "autoevo-nightly", "retry-fixture", now_epoch=199
            )
            due = routine_claim.schedule_decision(
                "autoevo-nightly", "retry-fixture", now_epoch=200
            )
            expect(
                waiting["action"] == "skip"
                and waiting["reason"] == "deferred-retry-not-due",
                "deferred autoevo retry ignored its cooldown",
            )
            expect(
                due["action"] == "run" and due["reason"] == "deferred-retry-due",
                "deferred autoevo retry did not reopen when due",
            )

            local_zone = datetime.now().astimezone().tzinfo
            completed_previous = (
                vault / "_meta" / "routine_runs" / "autoevo-nightly" / "2026-07-25.toml"
            )
            completed_previous.write_text(
                'routine = "autoevo-nightly"\n'
                'cycle_id = "2026-07-25"\n'
                'status = "completed"\n',
                encoding="utf-8",
            )
            before_primary = routine_claim.select_scheduled_cycle(
                "autoevo-nightly",
                now=datetime(2026, 7, 26, 4, 30, tzinfo=local_zone),
            )
            expect(
                before_primary["action"] == "skip"
                and before_primary["cycle_id"] == "2026-07-25"
                and before_primary["reason"]
                == "previous-cycle-completed-before-primary",
                "pre-05:00 RunAtLoad duplicated a completed previous cycle",
            )
            completed_previous.write_text(
                'routine = "autoevo-nightly"\n'
                'cycle_id = "2026-07-25"\n'
                'status = "deferred"\n'
                "retry_after_epoch = 0\n",
                encoding="utf-8",
            )
            unresolved_previous = routine_claim.select_scheduled_cycle(
                "autoevo-nightly",
                now=datetime(2026, 7, 26, 4, 30, tzinfo=local_zone),
            )
            expect(
                unresolved_previous["action"] == "run"
                and unresolved_previous["cycle_id"] == "2026-07-25"
                and unresolved_previous["reason"] == "previous-cycle-unresolved",
                "pre-05:00 wake did not target the unresolved previous cycle",
            )
            after_primary = routine_claim.select_scheduled_cycle(
                "autoevo-nightly",
                now=datetime(2026, 7, 26, 5, 0, tzinfo=local_zone),
            )
            expect(
                after_primary["action"] == "run"
                and after_primary["cycle_id"] == "2026-07-26"
                and after_primary["reason"] == "primary-or-missed-current-cycle",
                "05:00 or later wake did not target the current missed cycle",
            )
            completed_previous.unlink()
            missing_previous = routine_claim.select_scheduled_cycle(
                "autoevo-nightly",
                now=datetime(2026, 7, 26, 4, 30, tzinfo=local_zone),
            )
            selected_cycle = routine_claim.validate_cycle_id(
                str(missing_previous["cycle_id"])
            )
            selected_audit = f"agent-findings/autoevo-applied-{selected_cycle}.md"
            selected_output = vault / selected_audit
            selected_output.write_text(
                "selected-cycle audit fixture\n",
                encoding="utf-8",
            )
            (vault / "_meta" / "routine_watch.toml").write_text(
                "[[routine]]\n"
                'name = "autoevo-nightly"\n'
                'execution = "local"\n'
                'output_dir = "agent-findings"\n'
                'file_pattern = "autoevo-applied-*.md"\n',
                encoding="utf-8",
            )
            selected_result = vault / "cache" / "selected-cycle-result.json"
            selected_result.write_text(
                json.dumps(
                    {
                        "routine": "autoevo-nightly",
                        "outcome": "delivered",
                        "output_file": selected_audit,
                        "summary": "selected-cycle fixture",
                        "skipped_inputs": [],
                    }
                ),
                encoding="utf-8",
            )
            selected_claimed_at = (
                datetime.now().astimezone() - timedelta(seconds=1)
            ).isoformat()
            selected_attestation = routine_result.verify_result(
                "autoevo-nightly",
                selected_cycle,
                selected_claimed_at,
                selected_result,
            )
            selected_verified_output = autoevo_verify._output_path(
                vault.resolve(),
                selected_attestation["output_file"],
                selected_cycle,
            )
            expect(
                missing_previous["action"] == "run"
                and selected_cycle == "2026-07-25"
                and Path(selected_audit).name == f"autoevo-applied-{selected_cycle}.md"
                and Path(selected_audit).parent.as_posix() == "agent-findings"
                and selected_attestation["cycle_id"] == selected_cycle
                and selected_verified_output == selected_output.resolve()
                and missing_previous["reason"] == "missed-previous-cycle",
                "pre-05:00 cycle diverged across selection, result, or verifier",
            )
        finally:
            if original_ov is None:
                os.environ.pop("OV", None)
            else:
                os.environ["OV"] = original_ov
            _paths.vault_root.cache_clear()
            _paths._registry.cache_clear()

    with tempfile.TemporaryDirectory(prefix="atelier-autoevo-verify-") as temp_dir:
        vault = Path(temp_dir) / "vault"
        audit_dir = vault / "agent-findings"
        cache_dir = vault / "cache"
        claim_dir = vault / "_meta" / "routine_runs" / "autoevo-nightly"
        audit_dir.mkdir(parents=True)
        cache_dir.mkdir(parents=True)
        claim_dir.mkdir(parents=True)

        def verify_git(*args: str) -> subprocess.CompletedProcess[str]:
            result = subprocess.run(
                ["git", *args],
                cwd=vault,
                capture_output=True,
                text=True,
            )
            expect(
                result.returncode == 0,
                f"autoevo verifier git {' '.join(args)} failed: {result.stderr}",
            )
            return result

        verify_git("init", "-q")
        verify_git("config", "user.name", "Atelier Smoke")
        verify_git("config", "user.email", "smoke@example.invalid")
        verify_git("config", "commit.gpgsign", "false")
        (vault / ".gitignore").write_text("_meta/\ncache/\n", encoding="utf-8")
        verify_git("add", ".gitignore")
        verify_git("commit", "-q", "-m", "fixture base")
        audit = audit_dir / "autoevo-applied-2099-01-03.md"
        decay_reports = [
            audit_dir / "decay-20990103-070000-research.md",
            audit_dir / "decay-20990103-070000-wip.md",
            audit_dir / "decay-20990103-070000-reflections.md",
        ]
        for report in decay_reports:
            report.write_text("fixture decay report\n", encoding="utf-8")
        audit.write_text(
            """## Autoevo Run: 2099-01-03 07:00

Run ID: 20990103-070000

### Sweep coverage (3)
- /fixture/research: envelope_returned
- /fixture/wip: envelope_returned
- /fixture/reflections: envelope_returned

### Sweep reports (3)
- agent-findings/decay-20990103-070000-research.md
- agent-findings/decay-20990103-070000-wip.md
- agent-findings/decay-20990103-070000-reflections.md

### Auto-applied (0)
- (none)

### Logged to pending queue (0)
- (none)

### Contradicted rhetorical dismissals (0)
- (none)

### Lint
- ERROR: 0, WARN: 0, INFO: 0

### Notes
- forgetter_partial: scope=/fixture/research, candidates_evaluated=15, reason=max_candidates

### Skipped (reason)
- (none)

### Errors
- (none)
""",
            encoding="utf-8",
        )
        quarantine = vault / "_meta" / "autoevo_quarantine.toml"
        quarantine.write_text(
            "[[quarantine]]\n"
            "scope = '/fixture/research'\n"
            "first_failed = '2098-12-01'\n"
            "consecutive_failures = 3\n"
            "reason = 'forgetter_no_envelope'\n"
            "expires_at = '2099-02-01'\n",
            encoding="utf-8",
        )
        output_paths = [
            audit.relative_to(vault).as_posix(),
            *(report.relative_to(vault).as_posix() for report in decay_reports),
            quarantine.relative_to(vault).as_posix(),
        ]
        verify_git(
            "add",
            audit.relative_to(vault).as_posix(),
            *(report.relative_to(vault).as_posix() for report in decay_reports),
        )
        verify_git("add", "-f", quarantine.relative_to(vault).as_posix())
        verify_git(
            "commit",
            "-q",
            "--only",
            "-m",
            "[autoevo:audit] smoke",
            "--",
            *output_paths,
        )
        expect(
            verify_git("status", "--porcelain").stdout == "",
            "exact autoevo evidence commit left the fixture dirty",
        )
        audit_commit = verify_git("rev-parse", "HEAD").stdout.strip()
        wrapper_log = cache_dir / "autoevo-runner-2099-01-03.log.smoke1"
        claim = claim_dir / "2099-01-03.toml"
        claim.write_text(
            f"""routine = "autoevo-nightly"
cycle_id = "2099-01-03"
claimed_at = "2099-01-03T07:00:00-08:00"
status = "completed"
completed_at = "2099-01-03T07:05:00-08:00"
duration_seconds = 300
outcome = "delivered"
output_file = "agent-findings/autoevo-applied-2099-01-03.md"
event_log = "cache/autoevo-runner-2099-01-03.log.smoke1"
verification = "passed"
verified_at = "2099-01-03T07:05:01-08:00"
verified_sweeps = 3
verification_commit = "{audit_commit}"
""",
            encoding="utf-8",
        )
        (cache_dir / "autoevo-20990103-070000-outcomes.json").write_text(
            json.dumps(
                {
                    "/fixture/research": "envelope_returned",
                    "/fixture/wip": "envelope_returned",
                    "/fixture/reflections": "envelope_returned",
                }
            ),
            encoding="utf-8",
        )
        (cache_dir / "autoevo-20990103-070000-lint.json").write_text(
            json.dumps(
                {
                    "counts": {"error": 0, "warn": 0, "info": 0},
                    "findings": [],
                }
            ),
            encoding="utf-8",
        )
        wrapper_log.write_text(
            """[2099-01-03T07:00:00-08:00] claimed: /fixture/2099-01-03.toml
[2099-01-03T07:00:01-08:00] deterministic autoevo preflight passed
[2099-01-03T07:00:02-08:00] starting: runtime=codex command=/autoevo-nightly
[2099-01-03T07:04:59-08:00] delivery validated: outcome=delivered output=agent-findings/autoevo-applied-2099-01-03.md
[2099-01-03T07:05:00-08:00] finished: status=completed duration=300s
[2099-01-03T07:05:00-08:00] lock release: {"released": true}
[2099-01-03T07:05:01-08:00] post-run verification passed: sweeps=3 commit=fixture
[2099-01-03T07:05:02-08:00] done: claim updated, lock released
""",
            encoding="utf-8",
        )
        verified = autoevo_verify.verify_cycle(
            "2099-01-03",
            vault=vault,
            wrapper_log=wrapper_log,
        )
        expect(
            verified["verified"] is True and verified["sweeps_completed"] == 3,
            "autoevo verifier rejected complete production evidence",
        )
        mismatched_audit = audit_dir / "autoevo-applied-2099-01-04.md"
        mismatched_audit.write_text("wrong cycle\n", encoding="utf-8")
        try:
            autoevo_verify._output_path(
                vault,
                "agent-findings/autoevo-applied-2099-01-04.md",
                "2099-01-03",
            )
        except autoevo_verify.VerificationError:
            pass
        else:
            raise SmokeFailure(
                "autoevo verifier accepted an audit path from another cycle"
            )
        mismatched_audit.unlink()
        completed_claim = claim.read_text(encoding="utf-8")
        claim.write_text(
            completed_claim.replace(
                'status = "completed"', 'status = "completion-uncertain"'
            ).replace('verification = "passed"', 'verification = "pending"'),
            encoding="utf-8",
        )
        try:
            autoevo_verify.verify_cycle(
                "2099-01-03",
                vault=vault,
                wrapper_log=wrapper_log,
            )
        except autoevo_verify.VerificationError:
            pass
        else:
            raise SmokeFailure("autoevo verifier accepted a pending external claim")
        pending = autoevo_verify.verify_cycle(
            "2099-01-03",
            vault=vault,
            wrapper_log=wrapper_log,
            allow_pending_claim=True,
        )
        expect(
            pending["verified"] is True,
            "autoevo verifier rejected its internal pending claim",
        )
        claim.write_text(completed_claim, encoding="utf-8")
        claim.write_text(
            claim.read_text(encoding="utf-8").replace(
                'outcome = "delivered"', 'outcome = "noop"'
            ),
            encoding="utf-8",
        )
        try:
            autoevo_verify.verify_cycle(
                "2099-01-03",
                vault=vault,
                wrapper_log=wrapper_log,
            )
        except autoevo_verify.VerificationError:
            pass
        else:
            raise SmokeFailure("autoevo verifier accepted a preflight noop")

        claim.write_text(completed_claim, encoding="utf-8")
        decay_reports[0].write_text(
            "fixture decay report changed after audit commit\n",
            encoding="utf-8",
        )
        verify_git("add", decay_reports[0].relative_to(vault).as_posix())
        verify_git("commit", "-q", "-m", "drift one decay report")
        try:
            autoevo_verify.verify_cycle(
                "2099-01-03",
                vault=vault,
                wrapper_log=wrapper_log,
            )
        except autoevo_verify.VerificationError:
            pass
        else:
            raise SmokeFailure(
                "autoevo verifier accepted a report outside the audit commit"
            )
