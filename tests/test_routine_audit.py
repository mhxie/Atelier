"""Tests for scripts/routine_audit.py.

Focused on the external-permission gate. Before this split, an external
capability that had never been exercised produced a warning and `ready` stayed
true, so the first run to exercise a send capability would have been an
unattended one with nobody watching. These pin the two properties that fix
depends on: writes block, and an unclassified verb counts as a write.
"""

from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import routine_audit as ra  # noqa: E402


class ExternalPermissionSplitTests(unittest.TestCase):
    def test_read_permissions_are_warnings(self):
        reads, writes = ra._split_unverified_external({"p": ["gmail:read"]})
        self.assertEqual(reads, {"p": ["gmail:read"]})
        self.assertEqual(writes, {})

    def test_readwise_read_is_a_read(self):
        """A read verb must not fail the preflight closed.

        It shipped in the write bucket because the classification set was not
        extended when the permission was added, which blocked the digest routine
        permanently: no probe could clear a verb no probe was written for.
        """
        reads, writes = ra._split_unverified_external({"p": ["readwise:read"]})
        self.assertEqual(reads, {"p": ["readwise:read"]})
        self.assertEqual(writes, {})

    def test_send_is_a_write(self):
        reads, writes = ra._split_unverified_external({"p": ["gmail:send-self"]})
        self.assertEqual(reads, {})
        self.assertEqual(writes, {"p": ["gmail:send-self"]})

    def test_an_unclassified_verb_defaults_to_write(self):
        """A new verb must fail closed, not slip through as a warning."""
        _reads, writes = ra._split_unverified_external({"p": ["gmail:invented-verb"]})
        self.assertEqual(writes, {"p": ["gmail:invented-verb"]})

    def test_a_profile_can_hold_both_classes(self):
        reads, writes = ra._split_unverified_external(
            {"p": ["gmail:read", "gmail:send-self"]}
        )
        self.assertEqual(reads, {"p": ["gmail:read"]})
        self.assertEqual(writes, {"p": ["gmail:send-self"]})

    def test_empty_input_produces_nothing(self):
        self.assertEqual(ra._split_unverified_external({}), ({}, {}))


class PermissionSmokeContractTests(unittest.TestCase):
    def test_every_smoke_probe_has_a_mutation_class(self):
        """The smoke script and the audit must agree on the probe set.

        They are separate files that only work together, so a probe added to
        one and not the other silently produces claims the audit ignores.
        """
        smoke = (SCRIPTS / "routine_permission_smoke.sh").read_text(encoding="utf-8")
        audit = (SCRIPTS / "routine_audit.py").read_text(encoding="utf-8")
        for permission in (
            "gmail:read",
            "readwise:read",
            "readwise:create-document",
            "gmail:send-self",
            "mail:send-self",
        ):
            self.assertIn(f'"{permission}"', audit, permission)
            self.assertIn(permission, smoke, permission)

    def test_every_declared_local_external_permission_has_a_probe(self):
        """The invariant the readwise:read block violated.

        A local profile can declare an external permission that no probe can
        exercise. Nothing fails at declaration time; the routine simply never
        passes preflight again. Checking the direction that matters -- declared
        permissions must be probeable -- catches it at the edit instead.
        """
        smoke = (SCRIPTS / "routine_permission_smoke.sh").read_text(encoding="utf-8")
        for name, profile in ra._load_profiles().items():
            if profile.get("surface") != "local":
                continue
            for permission in profile.get("permissions", []):
                namespace = permission.split(":", 1)[0]
                if namespace not in ra.EXTERNAL_PERMISSION_NAMESPACES:
                    continue
                self.assertIn(permission, smoke, f"{name}: {permission}")

    def test_send_probe_is_bounded_to_the_authenticated_account(self):
        """The recipient constraint is the enforcement, so it must be stated."""
        smoke = (SCRIPTS / "routine_permission_smoke.sh").read_text(encoding="utf-8")
        block = smoke[smoke.index("gmail:send-self)"):]
        self.assertIn("to no other recipient", block)
        self.assertIn("Do not add any CC or BCC recipient", block)


class PlistRecoveryTests(unittest.TestCase):
    """A single-shot plist loses a whole cycle to one deferral.

    Verified against the live vault on 2026-08-31: 16 of 18 private routine
    plists fired once on one weekday with no catch-up, which is what an
    intermittent hit rate looks like from the inside.
    """

    def test_hour_wildcard_is_hourly(self):
        self.assertEqual(
            ra.plist_recovery({"StartCalendarInterval": {"Minute": 0}}), "hourly"
        )

    def test_a_pinned_hour_is_not_hourly(self):
        self.assertEqual(
            ra.plist_recovery({"StartCalendarInterval": {"Hour": 6, "Minute": 6}}),
            "none",
        )

    def test_weekly_single_shot_has_no_recovery(self):
        self.assertEqual(
            ra.plist_recovery(
                {"StartCalendarInterval": {"Hour": 3, "Minute": 10, "Weekday": 3}}
            ),
            "none",
        )

    def test_run_at_load_counts_on_its_own(self):
        self.assertEqual(
            ra.plist_recovery(
                {"StartCalendarInterval": {"Hour": 9}, "RunAtLoad": True}
            ),
            "run-at-load",
        )

    def test_start_interval_counts_as_hourly(self):
        self.assertEqual(ra.plist_recovery({"StartInterval": 3600}), "hourly")

    def test_a_dense_list_counts_as_hourly(self):
        self.assertEqual(
            ra.plist_recovery(
                {"StartCalendarInterval": [{"Hour": h} for h in range(12)]}
            ),
            "hourly",
        )

    def test_a_sparse_list_does_not(self):
        self.assertEqual(
            ra.plist_recovery({"StartCalendarInterval": [{"Hour": 6}, {"Hour": 18}]}),
            "none",
        )

    def test_both_mechanisms_are_reported(self):
        self.assertEqual(
            ra.plist_recovery({"StartCalendarInterval": {"Minute": 0}, "RunAtLoad": True}),
            "hourly+run-at-load",
        )

    def test_the_shipped_reference_plist_can_recover(self):
        """autoevo-nightly is the shape the private plists should copy."""
        import plistlib

        path = REPO_ROOT / "scripts" / "launchd" / "com.atelier.autoevo-nightly.plist"
        with path.open("rb") as handle:
            plist = plistlib.load(handle)
        self.assertEqual(ra.plist_recovery(plist), "hourly+run-at-load")


class ScheduleAgreementTests(unittest.TestCase):
    """The precondition for letting the declared cron gate execution.

    Once the cron decides whether a cycle is owed, a plist firing on a day the
    cron does not claim means that run is silently dropped. Only that direction
    is a finding; the reverse costs nothing, because nothing runs on a day the
    plist never fires anyway.
    """

    TODAY = date(2026, 8, 30)

    def _check(self, cron: str, plist: dict) -> dict:
        return ra.schedule_disagreements(
            [{"name": "r", "cron": cron}], {"r": plist}, today=self.TODAY
        )

    def test_matching_weekday_agrees(self):
        self.assertEqual(
            self._check(
                "0 6 * * 1", {"StartCalendarInterval": {"Hour": 6, "Weekday": 1}}
            ),
            {},
        )

    def test_a_plist_firing_on_an_unclaimed_day_is_reported(self):
        found = self._check(
            "0 6 * * 1", {"StartCalendarInterval": {"Hour": 6, "Weekday": 2}}
        )
        self.assertIn("r", found)
        self.assertTrue(found["r"])

    def test_a_cron_claiming_a_day_the_plist_skips_is_not_a_finding(self):
        """Harmless: nothing runs that day today either."""
        self.assertEqual(
            self._check(
                "0 6 * * 1,2", {"StartCalendarInterval": {"Hour": 6, "Weekday": 1}}
            ),
            {},
        )

    def test_a_daily_single_shot_plist_under_a_weekly_cron_is_reported(self):
        """This is precisely the regression the cron gate exists to prevent."""
        found = self._check("0 6 * * 1", {"StartCalendarInterval": {"Hour": 6}})
        self.assertIn("r", found)

    def test_a_plist_that_can_recover_is_exempt(self):
        """Over-firing is the design once the cron gates execution.

        Reporting it would flag the fix as the fault: the hourly wildcard exists
        so a missed cycle can be picked up, and the selector skips the days the
        cron does not claim.
        """
        self.assertEqual(
            self._check("0 6 * * 1", {"StartCalendarInterval": {"Minute": 40}}), {}
        )
        self.assertEqual(
            self._check(
                "0 6 * * 1", {"StartCalendarInterval": {"Hour": 6}, "RunAtLoad": True}
            ),
            {},
        )

    def test_an_unevaluable_cron_is_skipped_not_guessed(self):
        self.assertEqual(
            self._check("garbage", {"StartCalendarInterval": {"Weekday": 2}}), {}
        )

    def test_a_routine_without_a_plist_is_skipped(self):
        self.assertEqual(
            ra.schedule_disagreements(
                [{"name": "r", "cron": "0 6 * * 1"}], {}, today=self.TODAY
            ),
            {},
        )

    def test_the_live_fleet_has_no_dangerous_disagreement(self):
        """Guards the wiring step: if this fails, a routine would lose runs."""
        try:
            _watch_path, routines = ra._load_watch()
        except Exception as exc:  # no private vault on this machine
            self.skipTest(f"routine_watch.toml unavailable: {exc}")
        names = {str(r.get("name")) for r in routines if r.get("name")}
        found = ra.schedule_disagreements(routines, ra._plists_by_routine(names))
        self.assertEqual(found, {}, f"plists fire on unclaimed days: {found}")


class DigestProfileTests(unittest.TestCase):
    """The digest profile's settings are each the narrowest that still works.

    Two of them were forced by findings rather than chosen. The Codex Gmail
    plugin marks send_email as requiring approval and unattended routines run
    under approval_policy = "never", so connector delivery is impossible there;
    moving the send into a script then made the plugin unnecessary, which is
    what let user_config go back to "ignore".
    """

    def test_shell_egress_is_granted_but_not_the_whole_sandbox(self):
        """The Readwise read needs the network; it does not need the fence gone."""
        profile = ra._load_profiles()["local-digest-mail"]
        self.assertEqual(profile["shell_network"], "enabled")
        self.assertNotEqual(profile["shell_network"], "unrestricted")
        self.assertEqual(profile["sandbox"], "workspace-write")

    def test_model_web_search_stays_off(self):
        """It needs a CLI, not a browser."""
        self.assertEqual(ra._load_profiles()["local-digest-mail"]["web_search"], "disabled")

    def test_user_config_stays_ignored(self):
        """The regression this guards is the expensive one.

        Loading user config to reach a connector also readmits user hooks and
        user MCP servers into an unattended run. Deterministic delivery is what
        buys this back; a future change that reintroduces a plugin here would
        silently give that up again.
        """
        profile = ra._load_profiles()["local-digest-mail"]
        self.assertEqual(profile["user_config"], "ignore")
        self.assertEqual(profile["required_plugins"], [])

    def test_delivery_is_not_a_connector_permission(self):
        permissions = ra._load_profiles()["local-digest-mail"]["permissions"]
        self.assertIn("mail:send-self", permissions)
        for verb in ("gmail:send", "gmail:send-self", "gmail:read"):
            self.assertNotIn(verb, permissions)

    def test_the_send_verb_is_still_gated_as_an_external_write(self):
        """Deterministic does not mean unverified: it still needs a smoke."""
        _reads, writes = ra._split_unverified_external({"p": ["mail:send-self"]})
        self.assertEqual(writes, {"p": ["mail:send-self"]})


if __name__ == "__main__":
    unittest.main()


class SmokeExemptionTests(unittest.TestCase):
    """A smoke cannot require the verification it exists to produce.

    Without the exemption the gate deadlocks: routine_permission_smoke.sh
    preflights through `resolve --check-system`, which errors on an unverified
    external write, and the only thing that can verify one is the smoke being
    blocked. The exemption is one permission wide and lives only on that path.
    """

    def test_the_flag_exists_and_documents_its_own_boundary(self):
        source = (SCRIPTS / "routine_audit.py").read_text(encoding="utf-8")
        self.assertIn("--smoke-permission", source)
        self.assertIn("routine_runner.sh never passes it", source)

    def test_only_the_smoke_script_passes_it(self):
        smoke = (SCRIPTS / "routine_permission_smoke.sh").read_text(encoding="utf-8")
        runner = (SCRIPTS / "routine_runner.sh").read_text(encoding="utf-8")
        self.assertIn("--smoke-permission", smoke)
        self.assertNotIn(
            "--smoke-permission",
            runner,
            "real execution must still fail closed on an unverified write",
        )

    def test_the_smoke_passes_the_permission_it_is_verifying(self):
        smoke = (SCRIPTS / "routine_permission_smoke.sh").read_text(encoding="utf-8")
        self.assertIn('--smoke-permission "$SMOKE_PERMISSION"', smoke)
