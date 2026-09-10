"""One finding contract for the lint-shaped CLIs: severity, code, where, message.

`harness_lint.py` and `lint.py` each carried their own
copy of this record and its two renderers. Domain audits with a different shape
(`dining_audit.py`, `zk_audit.py`) keep their own.
"""
from __future__ import annotations

from dataclasses import dataclass
import json


@dataclass(frozen=True)
class Finding:
    severity: str  # "ERROR" | "WARN" | "INFO"
    code: str
    where: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "severity": self.severity,
            "code": self.code,
            "where": self.where,
            "message": self.message,
        }


def add(findings: list[Finding], severity: str, code: str, where: str, message: str) -> None:
    """Append a Finding built from the same four positional fields, in order."""
    findings.append(Finding(severity, code, where, message))


def counts(findings: list[Finding]) -> dict[str, int]:
    tally = {"ERROR": 0, "WARN": 0, "INFO": 0}
    for finding in findings:
        tally[finding.severity] = tally.get(finding.severity, 0) + 1
    return tally


def format_table(findings: list[Finding], *, empty: str, label: str, separator: str) -> str:
    if not findings:
        return empty
    tally = counts(findings)
    lines = [
        f"{label}{separator}{tally['ERROR']} error, {tally['WARN']} warn, {tally['INFO']} info",
        "",
    ]
    for finding in findings:
        lines.append(f"[{finding.severity:5s}] {finding.code}")
        lines.append(f"    where:   {finding.where}")
        lines.append(f"    message: {finding.message}")
        lines.append("")
    return "\n".join(lines)


def format_json(findings: list[Finding], **extra: object) -> str:
    tally = counts(findings)
    payload = {
        **extra,
        "counts": {
            "error": tally["ERROR"],
            "warn": tally["WARN"],
            "info": tally["INFO"],
        },
        "findings": [finding.to_dict() for finding in findings],
    }
    return json.dumps(payload, indent=2) + "\n"
