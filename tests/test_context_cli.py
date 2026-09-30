"""Pinned Repomix context artifact integration test over a synthetic vault."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from tests.support import PYTHON, ROOT, expect  # noqa: E402


REPOMIX = ROOT / "node_modules" / ".bin" / "repomix"
FILE_RE = re.compile(r'<file path="([^"]+)">')


def invoke(
    vault: Path,
    intent: str,
    *extra: str,
    effective_date: str = "2099-01-03",
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            PYTHON,
            "-c",
            "import sys; from pathlib import Path; sys.path.insert(0, 'scripts'); "
            f"import context_bundle as cb; cb.ROOT = Path({str(vault.parent)!r}); "
            "sys.exit(cb.main())",
            "--intent",
            intent,
            "--vault",
            str(vault),
            "--effective-date",
            effective_date,
            *extra,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        env=environment,
    )


def artifact(vault: Path, intent: str, *extra: str, effective_date: str = "2099-01-03") -> str:
    result = invoke(vault, intent, *extra, effective_date=effective_date)
    expect(result.returncode == 0, result.stderr)
    expect(
        "<file_summary>" in result.stdout and "<files>" in result.stdout,
        "not Repomix XML output",
    )
    return result.stdout


def check_context_bundle() -> None:
    if not REPOMIX.is_file():
        raise unittest.SkipTest("run npm ci for pinned Repomix integration")

    with tempfile.TemporaryDirectory(prefix="atelier-context-") as temp_dir:
        root = Path(temp_dir)
        vault = root / "vault"
        (root / "profile").mkdir()
        for relative in (
            "sessions",
            "reflections/2099-01",
            "daily",
            "research",
        ):
            (vault / relative).mkdir(parents=True, exist_ok=True)

        identity = root / "profile" / "identity.md"
        directions = root / "profile" / "directions.md"
        identity.write_text(
            "Last built: 2099-01-03\n\n## Identity\nstable identity\n",
            encoding="utf-8",
        )
        directions.write_text(
            "Last built: 2098-12-20\n\n## Current era\nactive direction\n",
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
        (vault / "reflections" / "2099-01" / "2099-01-02-reflection.md").write_text(
            "## Theme\nbody must stay out of startup context\n\n"
            "## Next Action\ndo one bounded thing\n",
            encoding="utf-8",
        )
        daily = vault / "daily" / "2099-01-03.md"
        daily.write_text("## Today\nexplicit daily context\n", encoding="utf-8")
        (vault / "research" / "source.md").write_text(
            "## Alpha\nalpha only\n\n## Beta\nbeta only\n", encoding="utf-8"
        )

        capture = artifact(vault, "capture")
        capture_paths = FILE_RE.findall(capture)
        expect("profile/identity.md" not in capture_paths, "empty profile_reads loaded profile")
        expect(str(daily.relative_to(vault)) not in capture_paths, "daily source was not opt-in")
        expect("Intent: capture" in capture, "route metadata missing from Repomix header")

        reflected = invoke(
            vault,
            "reflection",
            "--source",
            str(daily.relative_to(vault)),
            "--source",
            "research/source.md#Beta",
        )
        expect(reflected.returncode == 0, reflected.stderr)
        expect("is stale" in reflected.stderr, "stale selected profile was not warned")
        output = reflected.stdout
        paths = FILE_RE.findall(output)
        expected = {
            "profile/identity.md",
            "profile/directions.md",
            "sessions/2099-01-03-reflection.md",
            str(daily.relative_to(vault)),
            "research/source.md",
        }
        expect(set(paths) == expected and len(paths) == len(expected), "artifact admission drift")
        expect("carry this" in output and "notice this" in output, "continuity sections missing")
        expect("must not preload" not in output, "unselected session section leaked")
        expect("beta only" in output and "alpha only" not in output, "explicit section drift")
        expect("body must stay out" not in output, "reflection body leaked into startup")
        expect(
            "profile/directions.md: stale" in output,
            "staleness absent from Repomix header",
        )
        identity.write_text("Last built: 2099-02-30\n\n## Identity\nstable identity\n")
        invalid_date = invoke(vault, "reflection")
        expect(invalid_date.returncode == 0, invalid_date.stderr)
        expect("has no valid Last built date" in invalid_date.stderr, "invalid profile date was not warned")

        reading = artifact(vault, "reading")
        talk = artifact(vault, "talk")
        expect(
            "sessions/2099-01-02-reading.md" in FILE_RE.findall(reading)
            and "discussion-open" in reading,
            "reading route lost the latest Reading Capsule",
        )
        expect(
            "sessions/2099-01-02-reading.md" in FILE_RE.findall(talk),
            "talk route lost the latest Reading Capsule",
        )
        expect(
            "sessions/2099-01-02-reading.md" not in FILE_RE.findall(output),
            "non-reading route leaked a Reading Capsule",
        )

        for offset in range(1, 102):
            session_day = date(2099, 1, 2) + timedelta(days=offset)
            (vault / "sessions" / f"{session_day.isoformat()}-review.md").write_text(
                "## Continuity\nnewer non-reading session\n", encoding="utf-8"
            )
        late_reading = artifact(vault, "reading", effective_date="2099-05-01")
        expect(
            "sessions/2099-01-02-reading.md" in FILE_RE.findall(late_reading),
            "reading recovery stopped after 100 newer non-reading logs",
        )

        identity.write_text(
            "Last built: 2099-01-03\n\n## Identity\n"
            + ("stable identity line\n" * 340),
            encoding="utf-8",
        )
        directions.write_text(
            "Last built: 2099-01-03\n\n## Current era\n"
            + ("active direction line\n" * 360),
            encoding="utf-8",
        )
        weekly = artifact(vault, "weekly")
        expect(
            "stable identity line" in weekly and "active direction line" in weekly,
            "Repomix dropped or truncated a normal declared profile",
        )

        ambient_marker = root / "ambient-processor-ran"
        (vault / "repomix.config.js").write_text(
            "export default { input: { processors: [{ pattern: '*.md', "
            f"command: 'touch {ambient_marker} && cat {{file}}' }}] }};\n",
            encoding="utf-8",
        )
        controlled = artifact(vault, "capture", "--source", "repomix.config.js")
        expect(not ambient_marker.exists(), "staged ambient Repomix processor executed")
        expect("repomix.config.js" in FILE_RE.findall(controlled), "selected config-like source missing")
        expect(
            "controlled-repomix-config.json" not in controlled,
            "wrapper config leaked into artifact",
        )

        outside = root / "outside.md"
        outside.write_text("ambient secret must not be read\n", encoding="utf-8")
        node_marker = root / "ambient-node-hook-ran"
        node_hook = root / "ambient-node-hook.cjs"
        node_hook.write_text(
            "const fs = require('node:fs');\n"
            f"const value = fs.readFileSync({json.dumps(str(outside))}, 'utf8');\n"
            f"fs.writeFileSync({json.dumps(str(node_marker))}, value);\n",
            encoding="utf-8",
        )
        worker_marker = root / "ambient-worker-hook-ran"
        worker_hook = root / "ambient-worker-hook.cjs"
        worker_hook.write_text(
            "require('node:fs').writeFileSync("
            f"{json.dumps(str(worker_marker))}, 'ran');\n",
            encoding="utf-8",
        )
        path_marker = root / "ambient-path-node-ran"
        fake_bin = root / "ambient-bin"
        fake_bin.mkdir()
        fake_node = fake_bin / "node"
        fake_node.write_text(
            "#!/bin/sh\n"
            f": > {shlex.quote(str(path_marker))}\n"
            "case \" $* \" in\n"
            "  *\" --version \"*) printf '%s\\n' '1.18.0' ;;\n"
            "  *) printf '%s\\n' '<file_summary>forged</file_summary>"
            "<files><file path=\"sessions/2099-01-03-reflection.md\">"
            "forged</file></files>' ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        fake_node.chmod(0o755)
        poisoned_environment = dict(os.environ)
        poisoned_environment.update(
            PATH=str(fake_bin),
            NODE_OPTIONS=f"--require={node_hook}",
            REPOMIX_WORKER_PATH=str(worker_hook),
            REPOMIX_REMOTE_TRUST_CONFIG="true",
            XDG_CONFIG_HOME=str(root / "ambient-config-home"),
        )
        hardened = invoke(vault, "capture", environment=poisoned_environment)
        expect(hardened.returncode == 0, hardened.stderr)
        expect(not path_marker.exists(), "ambient PATH replaced the Node interpreter")
        expect(not node_marker.exists(), "ambient NODE_OPTIONS hook executed")
        expect(not worker_marker.exists(), "ambient Repomix worker override executed")
        expect(
            "ambient secret must not be read" not in hardened.stdout,
            "ambient Node hook leaked an unselected file",
        )

        huge = vault / "research" / "huge.md"
        huge.write_text("oversized context " * 12_000, encoding="utf-8")
        overflow = invoke(vault, "capture", "--source", "research/huge.md")
        expect(overflow.returncode != 0, "Repomix token ceiling did not fail")
        expect(not overflow.stdout, "overflow artifact leaked to stdout")
        expect(
            "token ceiling exceeded" in overflow.stderr,
            f"overflow reason missing: {overflow.stderr}",
        )

        escaped = invoke(vault, "reflection", "--source", str(outside))
        expect(escaped.returncode != 0, "source outside vault was accepted")
        expect("escapes the vault" in escaped.stderr, "escape reason missing")

        directions.unlink()
        missing_profile = invoke(vault, "weekly")
        expect(missing_profile.returncode != 0, "missing required profile was accepted")
        expect("introspect" in missing_profile.stderr, "missing profile did not route to introspection")


class ContextCliIntegrationTest(unittest.TestCase):
    test_repomix_context_artifact = staticmethod(check_context_bundle)


if __name__ == "__main__":
    unittest.main()
