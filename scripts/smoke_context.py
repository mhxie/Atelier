"""smoke_context.py: bounded context projection smoke check.

Split out of harness_smoke.py; harness_smoke.py re-exports every name so callers and tests are unchanged.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from datetime import date, timedelta
from pathlib import Path

from smoke_common import (  # noqa: E402
    PYTHON,
    ROOT,
    expect,
    run,
)


def check_context_bundle() -> None:
    with tempfile.TemporaryDirectory(prefix="atelier-context-") as temp_dir:
        root = Path(temp_dir)
        vault = root / "vault"
        for relative in (
            "profile",
            "sessions",
            "reflections",
            "daily-notes/2099/01",
            "research",
        ):
            (vault / relative).mkdir(parents=True, exist_ok=True)

        (vault / "profile" / "identity.md").write_text(
            "Last built: 2099-01-03\n\n## Identity\nstable identity\n",
            encoding="utf-8",
        )
        (vault / "profile" / "directions.md").write_text(
            "Last built: 2099-01-03\n\n## Direction\nactive direction\n",
            encoding="utf-8",
        )
        (vault / "sessions" / "2099-01-03-reflection.md").write_text(
            "## Continuity\ncarry this\n\n"
            "## Anomalies\nnotice this\n\n"
            "## Full Text\nmust not preload\n",
            encoding="utf-8",
        )
        (vault / "sessions" / "2099-01-02-reading.md").write_text(
            "## Reading Capsule\n"
            "checkpoint: initial-analysis\n"
            "source: durable reading source\n"
            "status: discussion-open\n",
            encoding="utf-8",
        )
        (vault / "reflections" / "2099-01").mkdir(parents=True, exist_ok=True)
        (vault / "reflections" / "2099-01" / "2099-01-02-reflection.md").write_text(
            "## Theme\nbody must stay out of the heading projection\n\n"
            "## Next Action\ndo one bounded thing\n",
            encoding="utf-8",
        )
        daily = vault / "daily-notes" / "2099" / "01" / "2099-01-03.md"
        daily.write_text("## Today\nexplicit daily context\n", encoding="utf-8")
        (vault / "research" / "source.md").write_text(
            "## Alpha\nalpha only\n\n## Beta\nbeta only\n",
            encoding="utf-8",
        )

        capture_stdout = run(
            [
                "scripts/context_bundle.py",
                "--intent",
                "capture",
                "--vault",
                str(vault),
                "--effective-date",
                "2099-01-03",
                "--format",
                "json",
            ]
        )
        capture = json.loads(capture_stdout)
        expect(
            not any(row["component"] == "profile" for row in capture["excerpts"]),
            "empty profile_reads unexpectedly loaded profile content",
        )
        expect(
            not any(row["component"] == "daily" for row in capture["excerpts"]),
            "daily context must remain opt-in",
        )
        expect(
            capture["budget"]["output_bytes"] == len(capture_stdout.encode("utf-8")),
            "context bundle JSON byte accounting drift",
        )
        expect(
            capture["budget"]["limit_bytes"] == 8192,
            "capture route did not use its registry context budget",
        )

        reflection_stdout = run(
            [
                "scripts/context_bundle.py",
                "--intent",
                "reflection",
                "--vault",
                str(vault),
                "--effective-date",
                "2099-01-03",
                "--component",
                "profile",
                "--component",
                "session",
                "--component",
                "reflections",
                "--component",
                "daily",
                "--component",
                "sources",
                "--source",
                "research/source.md#Beta",
                "--byte-budget",
                "8192",
                "--format",
                "json",
            ]
        )
        reflection = json.loads(reflection_stdout)
        excerpts = reflection["excerpts"]
        expect(
            {row["section"] for row in excerpts if row["component"] == "session"}
            == {"Continuity", "Anomalies"},
            "session projection leaked non-continuity sections",
        )
        expect(
            any(row["source"] == str(daily.relative_to(vault)) for row in excerpts),
            "explicit daily component did not resolve effective-date note",
        )
        expect(
            any(
                row["source"] == "research/source.md"
                and row["section"] == "Beta"
                and "beta only" in row["content"]
                and "alpha only" not in row["content"]
                for row in excerpts
            ),
            "explicit source section projection drift",
        )
        expect(
            "body must stay out" not in reflection_stdout,
            "reflection projection loaded a full low-priority section body",
        )
        expect(
            len(reflection_stdout.encode("utf-8")) <= 8192,
            "context bundle exceeded its selected byte budget",
        )

        reading = json.loads(
            run(
                [
                    "scripts/context_bundle.py",
                    "--intent",
                    "reading",
                    "--vault",
                    str(vault),
                    "--effective-date",
                    "2099-01-03",
                    "--format",
                    "json",
                ]
            )
        )
        expect(
            any(
                row["section"] == "Reading Capsule"
                and row["source"] == "sessions/2099-01-02-reading.md"
                and "discussion-open" in row["content"]
                for row in reading["excerpts"]
            ),
            "reading route did not receive the latest reading capsule",
        )
        expect(
            reading["budget"]["limit_bytes"] == 32768,
            "reading route did not use its registry context budget",
        )
        talk = json.loads(
            run(
                [
                    "scripts/context_bundle.py",
                    "--intent",
                    "talk",
                    "--vault",
                    str(vault),
                    "--effective-date",
                    "2099-01-03",
                    "--format",
                    "json",
                ]
            )
        )
        expect(
            any(
                row["section"] == "Reading Capsule"
                and row["source"] == "sessions/2099-01-02-reading.md"
                for row in talk["excerpts"]
            ),
            "talk route did not receive the latest reading capsule",
        )
        expect(
            not any(
                row["section"] == "Reading Capsule" for row in reflection["excerpts"]
            ),
            "non-reading route leaked a reading capsule",
        )
        for offset in range(1, 102):
            session_day = date(2099, 1, 2) + timedelta(days=offset)
            (vault / "sessions" / f"{session_day.isoformat()}-review.md").write_text(
                "## Continuity\nnewer non-reading session\n",
                encoding="utf-8",
            )
        late_reading = json.loads(
            run(
                [
                    "scripts/context_bundle.py",
                    "--intent",
                    "reading",
                    "--vault",
                    str(vault),
                    "--effective-date",
                    "2099-05-01",
                    "--format",
                    "json",
                ]
            )
        )
        expect(
            any(
                row["section"] == "Reading Capsule"
                and row["source"] == "sessions/2099-01-02-reading.md"
                for row in late_reading["excerpts"]
            ),
            "reading recovery stopped after 100 newer non-reading session logs",
        )

        # Declared profile files land whole at real-world sizes. The old 4-8 KB
        # budgets and 3.5 KB per-file cap truncated identity + directions on
        # every goal route; this guards the re-provisioned ceilings.
        (vault / "profile" / "identity.md").write_text(
            "Last built: 2099-01-03\n\n## Identity\n" + ("stable identity line\n" * 340),
            encoding="utf-8",
        )
        (vault / "profile" / "directions.md").write_text(
            "Last built: 2099-01-03\n\n## Current era\n" + ("active direction line\n" * 360),
            encoding="utf-8",
        )
        weekly_stdout = run(
            [
                "scripts/context_bundle.py",
                "--intent",
                "weekly",
                "--vault",
                str(vault),
                "--effective-date",
                "2099-01-03",
                "--format",
                "json",
            ]
        )
        weekly = json.loads(weekly_stdout)
        profile_rows = [row for row in weekly["excerpts"] if row["component"] == "profile"]
        expect(
            {row["source"] for row in profile_rows} == {"profile/identity.md", "profile/directions.md"}
            and not any(row["truncated"] for row in profile_rows),
            "weekly route truncated or dropped a declared profile file at a normal size",
        )
        expect(
            weekly["budget"]["limit_bytes"] == 32768
            and len(weekly_stdout.encode("utf-8")) <= 32768,
            "weekly route did not honor its 32 KB catalog ceiling",
        )

        outside = root / "outside.md"
        outside.write_text("must not read\n", encoding="utf-8")
        escaped = subprocess.run(
            [
                PYTHON,
                "scripts/context_bundle.py",
                "--intent",
                "reflection",
                "--vault",
                str(vault),
                "--component",
                "sources",
                "--source",
                str(outside),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        expect(
            escaped.returncode != 0 and "escapes the vault" in escaped.stderr,
            "context bundle accepted a source outside the selected vault",
        )
