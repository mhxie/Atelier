"""Autoevo command, read-only readiness, and evidence integration tests."""

from __future__ import annotations

import json
import os
import plistlib
import subprocess
import tempfile
import tomllib
import unittest
from functools import partial
from unittest import mock
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

import autoevo_preflight  # noqa: E402
import autoevo_quarantine  # noqa: E402

import _paths  # noqa: E402
from tests.support import (  # noqa: E402
    IntegrationFailure,
    expect,
)


def check_autoevo_readiness() -> None:
    paths = tomllib.loads((ROOT / "harness/paths.toml").read_text())["paths"]
    with mock.patch.object(_paths, "_registry", return_value=paths):
        _check_autoevo_readiness()


def _check_autoevo_readiness() -> None:
    plist_path = ROOT / "scripts" / "launchd" / "com.atelier.prefect-routines.plist"
    plist = plistlib.loads(plist_path.read_bytes())
    expect(
        plist.get("Label") == "com.atelier.prefect-routines"
        and plist.get("RunAtLoad") is True
        and plist.get("KeepAlive") is True
        and "StartCalendarInterval" not in plist
        and plist.get("ProgramArguments", [])[-1] == "serve",
        "autoevo is not owned by the long-running Prefect deployment service",
    )
    deployment_source = (ROOT / "scripts/routine_prefect.py").read_text(encoding="utf-8")
    expect(
        "adapter.load_specs" in deployment_source
        and "for expression in spec.schedule.cron" in deployment_source
        and "model_routine_flow.to_deployment" in deployment_source,
        "model schedules are not declared through validated Prefect deployments",
    )

    captured_semantic_command: list[str] = []
    def capture_semantic_run(
        command: list[str],
        *,
        cwd: Path,
        timeout: float = 30,
        env: dict[str, str] | None = None,
    ) -> autoevo_preflight.CommandResult:
        del cwd, timeout, env
        captured_semantic_command.extend(command)
        return autoevo_preflight.CommandResult(0, '{"backend":"qmd","ready":true,"totalDocuments":3}', "")

    with mock.patch.object(autoevo_preflight, "_run", capture_semantic_run):
        semantic_readiness = autoevo_preflight._default_semantic_probe()
    expect(
        semantic_readiness["ready"] is True
        and captured_semantic_command[-3:] == ["status", "--format", "json"]
        and "query" not in captured_semantic_command,
        "autoevo semantic readiness probe can still attempt a model download",
    )

    with tempfile.TemporaryDirectory(prefix="atelier-autoevo-quarantine-") as temp_dir:
        temp = Path(temp_dir)
        state = temp / "autoevo_quarantine.toml"
        state.write_text(
            "[[quarantine]]\n"
            'scope = "/expired"\n'
            'first_failed = "2098-12-01"\n'
            "consecutive_failures = 3\n"
            'reason = "forgetter_no_envelope"\n'
            'expires_at = "2099-01-02"\n',
            encoding="utf-8",
        )
        crossed = autoevo_quarantine.update_state(
            outcomes={"/expired": "forgetter_no_envelope"},
            state_path=state,
            today=date(2099, 1, 2),
        )
        restarted = tomllib.loads(state.read_text(encoding="utf-8"))["quarantine"][0]
        expect(
            crossed == 0
            and restarted["consecutive_failures"] == 1
            and restarted["first_failed"] == "2099-01-02"
            and restarted["expires_at"] == "2099-02-01"
            and {path.name for path in temp.iterdir()} == {state.name},
            "post-expiry quarantine failure did not restart at one",
        )

        active_scope = '/active/tab\tcrlf\r\nslash\\quote"snow雪\x01'
        state.write_text(
            "[[quarantine]]\n"
            f"scope = {json.dumps(active_scope, ensure_ascii=False)}\n"
            'first_failed = "2099-01-01"\n'
            "consecutive_failures = 2\n"
            'reason = "forgetter_no_envelope"\n'
            'expires_at = "2099-02-01"\n',
            encoding="utf-8",
        )
        crossed = autoevo_quarantine.update_state(
            outcomes={active_scope: "forgetter_no_envelope"},
            state_path=state,
            today=date(2099, 1, 2),
        )
        active = tomllib.loads(state.read_text(encoding="utf-8"))["quarantine"][0]
        expect(
            crossed == 1
            and active["consecutive_failures"] == 3
            and active["scope"] == active_scope,
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

        cleared = autoevo_quarantine.update_state(
            outcomes={active_scope: "envelope_returned"},
            state_path=state,
            today=date(2099, 1, 2),
        )
        expect(
            cleared == 0
            and autoevo_quarantine.active_scopes(state_path=state, today=date(2099, 1, 2)) == []
            and tomllib.loads(state.read_text(encoding="utf-8"))["quarantine"] == [],
            "successful dispatch did not clear its quarantine streak",
        )

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
                outcomes={"/malformed": "forgetter_no_envelope"},
                state_path=malformed_state,
                today=date(2099, 1, 2),
            )
        except autoevo_quarantine.QuarantineError:
            pass
        else:
            raise IntegrationFailure("quarantine update accepted malformed ISO dates")

        for malformed_outcomes in ([], {"": "forgetter_no_envelope"}, {"/x": "unknown"}):
            try:
                autoevo_quarantine.update_state(
                    outcomes=malformed_outcomes,  # type: ignore[arg-type]
                    state_path=state,
                    today=date(2099, 1, 2),
                )
            except autoevo_quarantine.QuarantineError:
                pass
            else:
                raise IntegrationFailure("quarantine update accepted malformed outcomes")

        state_before_failed_write = state.read_text(encoding="utf-8")
        with mock.patch.object(autoevo_quarantine, "_atomic_write", side_effect=OSError("fixture state write failure")):
            try:
                autoevo_quarantine.update_state(
                    outcomes={active_scope: "forgetter_no_envelope"},
                    state_path=state,
                    today=date(2099, 1, 2),
                )
            except OSError:
                pass
            else:
                raise IntegrationFailure(
                    "quarantine update hid an authoritative write failure"
                )
        expect(
            state.read_text(encoding="utf-8") == state_before_failed_write,
            "quarantine write failure changed authoritative state",
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
            inspect_preflight = partial(
                autoevo_preflight.inspect_preflight,
                vault=vault,
                lock_path=session_lock,
                privacy_probe=privacy_probe,
                semantic_probe=semantic_probe,
            )
            status_commands: list[list[str]] = []

            original_run = autoevo_preflight._run

            def capture_status(command: list[str], **kwargs: object):
                status_commands.append(command)
                return original_run(command, **kwargs)

            with mock.patch.object(autoevo_preflight, "_run", capture_status):
                clean = inspect_preflight()
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
            missing = inspect_preflight()
            expect(
                missing["gate"] == "git_index_missing",
                "autoevo misclassified a missing index as an ordinary dirty tree",
            )
            expect(
                missing["health"]["worktree_entries"] is None,
                "autoevo ran git status after detecting a missing index",
            )
            expect(not index_path.exists(), "preflight repaired a missing index")
            expect(
                not list((vault / "agent-findings").iterdir())
                and not list((vault / "cache").iterdir()),
                "blocked preflight wrote an audit or recovery state",
            )
            git("read-tree", "HEAD")

            raw_lock = git("rev-parse", "--git-path", "index.lock").stdout.strip()
            index_lock = Path(raw_lock)
            if not index_lock.is_absolute():
                index_lock = vault / index_lock
            index_lock.touch()
            locked = inspect_preflight()
            expect(
                locked["gate"] == "git_index_lock_present",
                "autoevo did not diagnose a Git index lock precisely",
            )
            expect(index_lock.exists(), "preflight removed the user's index lock")
            expect(
                not list((vault / "agent-findings").iterdir()),
                "index-lock preflight wrote an audit",
            )
            index_lock.unlink()

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
            state_dirty = inspect_preflight(now=1_000)
            expect(
                state_dirty["gate"] == "dirty_autoevo_state",
                f"dirty autoevo state must block: {state_dirty.get('gate')}",
            )
            state_file.write_text("# base\n", encoding="utf-8")
            state_file.write_text("# dirty\n", encoding="utf-8")
            dirty = inspect_preflight(now=1_000)
            expect(
                dirty["retry_after_epoch"]
                == 1_000 + autoevo_preflight.GENERIC_RETRY_DELAY_SECONDS,
                "non-session autoevo blocker did not retry on the next hourly check",
            )
            expect(
                state_file.read_text(encoding="utf-8") == "# dirty\n"
                and not list((vault / "agent-findings").iterdir()),
                "preflight absorbed dirty managed state or wrote an audit",
            )
            state_file.write_text("# base\n", encoding="utf-8")

            legacy = vault / "cache" / autoevo_preflight.LEGACY_OWNED_AUDIT_STATE
            legacy.write_text("{unparsed legacy state", encoding="utf-8")
            legacy_result = inspect_preflight()
            expect(
                legacy_result["gate"] == "legacy_audit_review_required"
                and legacy_result["retry_after_epoch"] is None
                and legacy.read_text(encoding="utf-8") == "{unparsed legacy state",
                "legacy owned-audit state was consumed or silently bypassed",
            )
            legacy.unlink()

            session_lock.touch()
            active = inspect_preflight(
                now=session_lock.stat().st_mtime,
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
            semantic_blocked = inspect_preflight(
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

        finally:
            if original_ov is None:
                os.environ.pop("OV", None)
            else:
                os.environ["OV"] = original_ov
            _paths.vault_root.cache_clear()
            _paths._registry.cache_clear()

class AutoevoIntegrationTest(unittest.TestCase):
    test_read_only_readiness = staticmethod(check_autoevo_readiness)
