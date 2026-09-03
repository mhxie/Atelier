"""smoke_vault.py: dining audit, tracking refresh, and vault job runner smoke checks.

Split out of harness_smoke.py; harness_smoke.py re-exports every name so callers and tests are unchanged.
"""

from __future__ import annotations

import plistlib
import tempfile
from pathlib import Path
import dining_audit

from smoke_common import (  # noqa: E402
    ROOT,
    expect,
)


def check_dining_audit() -> None:
    with tempfile.TemporaryDirectory(prefix="atelier-dining-audit-") as temp_dir:
        vault = Path(temp_dir)
        profile = vault / "profile" / "diet.md"
        profile.parent.mkdir(parents=True)
        mapped = {
            "Regional dining catalog": "travel/regional-catalog.md",
            "Meal-history tracker": "travel/meal-history.md",
            "Credit-perks catalog": "travel/credit-eligibility.md",
            "Benefits tracker": "finance/benefits-tracker.md",
            "Prepaid-balance tracker": "finance/prepaid-balances.md",
        }
        for relative in mapped.values():
            target = vault / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("# Fixture\n", encoding="utf-8")
        profile.write_text(
            """# Personal Diet Policy

## Catalog files

| Role | Path | Write owner |
|---|---|---|
| Regional dining catalog | `travel/regional-catalog.md` | fixture |
| Meal-history tracker | `travel/meal-history.md` | fixture |
| Credit-perks catalog | `travel/credit-eligibility.md` | fixture |
| Benefits tracker | `finance/benefits-tracker.md` | fixture |
| Prepaid-balance tracker | `finance/prepaid-balances.md` | fixture |

## Full health-flag taxonomy

- `flag-a` - fixture
- `flag-b` - fixture
""",
            encoding="utf-8",
        )
        dining_log = vault / mapped["Meal-history tracker"]
        dining_log.write_text(
            """# Meal History Fixture

## Visits

| Date | Restaurant | City | 类型 | ⭐ | 评分 | 再去 | 健康 | 人数 | 总额 | 人均 | Platform | Credit | 必点·备注 |
|---|---|---|---|---|---|---|---|---:|---:|---:|---|---|---|
| 2025-12-31 | JPY | Tokyo | Test | — | 7 | Y | flag-a | 2 | ¥23,925 | ¥11,962.50 | W | — | okay |
| 2026-01-01 | A | X | Test | — | 8 | Y | flag-a | 2 | $20.00 | $10.00 | W | — | good |
| 2026-01-02 | B | X | Test | — | 7 | Maybe | flag-b | 3 | ~$30.00 | ~$10.00 | W | — | okay |

## Derived views
""",
            encoding="utf-8",
        )
        valid = dining_audit.audit(vault, 2)
        expect(valid["ok"] is True, f"valid dining fixture failed: {valid}")
        expect(valid["stats"]["rows"] == 3, "dining row count drift")
        expect(
            len(valid["recent"]) == 2
            and valid["per_person_trend"]["known"] == 2
            and valid["per_person_trend"]["direction"] == "unknown",
            f"dining recent view overclaimed a sparse trend: {valid}",
        )

        dining_log.write_text(
            dining_log.read_text(encoding="utf-8")
            .replace(
                "| 2026-01-02 | B |",
                "| 2025-12-31 | B |",
            )
            .replace(
                "| ~$30.00 | ~$10.00 |",
                "| ~$30.00 | ~$12.00 |",
            )
            .replace(
                "| 2026-01-01 | A |",
                "| 2026-01-01 | TBD |",
            ),
            encoding="utf-8",
        )
        (vault / mapped["Regional dining catalog"]).write_text(
            "[broken](missing.md)\n[remote](readwise:fixture)\n",
            encoding="utf-8",
        )
        (vault / mapped["Credit-perks catalog"]).write_text(
            "## Cycle Tracking\n",
            encoding="utf-8",
        )
        invalid = dining_audit.audit(vault)
        error_codes = {finding["code"] for finding in invalid["errors"]}
        expect(invalid["ok"] is False, "invalid dining fixture passed")
        expect("date_order" in error_codes, "dining audit missed event-date drift")
        expect(
            "per_person_mismatch" in error_codes,
            "dining audit missed per-person arithmetic drift",
        )
        expect(
            "restaurant_pending" in error_codes,
            "dining audit accepted a placeholder restaurant",
        )
        expect(
            "local_link_broken" in error_codes,
            "dining audit missed a broken mapped-catalog link",
        )
        expect(
            "live_state_in_eligibility_catalog" in error_codes,
            "dining audit accepted live state in the eligibility catalog",
        )

def check_tracking_refresh_routine() -> None:
    runner = (ROOT / "scripts" / "tracking_refresh_runner.sh").read_text(
        encoding="utf-8"
    )
    for fragment in (
        'routine_owner.py" check --json',
        "find_python.sh",
        "command_timeout.py",
        "/usr/bin/caffeinate",
        'refresh_tracking.py" --json',
    ):
        expect(
            fragment in runner,
            f"tracking refresh runner missing contract fragment: {fragment}",
        )
    expect(
        "codex" not in runner.lower(),
        "deterministic tracking refresh invokes Codex",
    )

    plist_path = ROOT / "scripts" / "launchd" / "com.atelier.tracking-refresh.plist"
    plist = plistlib.loads(plist_path.read_bytes())
    expect(
        plist.get("Label") == "com.atelier.tracking-refresh"
        and plist.get("RunAtLoad") is True
        and plist.get("StartCalendarInterval")
        == [
            {"Hour": 5, "Minute": 30},
            {"Hour": 17, "Minute": 30},
        ],
        "tracking refresh launchd schedule drift",
    )
    arguments = plist.get("ProgramArguments", [])
    expect(
        any("tracking_refresh_runner.sh" in str(value) for value in arguments),
        "tracking refresh plist does not invoke its deterministic runner",
    )

def check_vault_job_runner() -> None:
    runner = (ROOT / "scripts" / "vault_job_runner.sh").read_text(encoding="utf-8")
    for fragment in (
        'routine_owner.py" check --json',
        "find_python.sh",
        "command_timeout.py",
        "/usr/bin/caffeinate",
        "ATELIER_VAULT_JOB_TIMEOUT_SECONDS",
        "uv run --quiet",
        # A vault-relative path only: absolute paths and parent traversal are
        # refused before ownership is even checked.
        "/*|*..*)",
    ):
        expect(
            fragment in runner,
            f"vault job runner missing contract fragment: {fragment}",
        )
    expect(
        "codex" not in runner.lower() and "claude" not in runner.lower(),
        "deterministic vault job runner invokes a model runtime",
    )
