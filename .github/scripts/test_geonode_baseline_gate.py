"""TASK-2302 (epic 2290 W2) — geonode differential-green comparator +
two-step re-pin governance, unit-tested hermetically (no DB, no docker —
see the `comparator-selftest` job in .github/workflows/tests.yml). The
baseline PIN itself stays soak-gated — see the soak-immaturity tests below;
nothing here pins a real baseline.

Originally lived in Hydrata/deploy (tests/unit/test_geonode_baseline_gate.py)
alongside the script it tests; both were relocated into this repo under
.github/scripts/ (a CODEOWNERS-protected path, see /.github/CODEOWNERS) by
the TASK-2302 fix-pass (finding A) so the comparator no longer needs a
private cross-repo checkout of Hydrata/deploy to run in geonode's own CI.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gate = _load("geonode_baseline_gate")


def _write_tsv(path, suite, rows, extra_suites=()):
    """Write a TSV. `extra_suites` appends additional `# suite` header lines
    (used to exercise the multi-suite-header-per-file case, finding B/C(2))."""
    lines = [f"# suite\t{suite}"]
    lines.extend(f"# suite\t{s}" for s in extra_suites)
    lines.extend(["# run_utc\t2026-07-18T04:00:00+00:00", "test_id\tstatus"])
    lines.extend(f"{tid}\t{status}" for tid, status in rows)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --- parse_tsv / load_run ----------------------------------------------------

class TestParseTsv:
    def test_parses_rows(self, tmp_path):
        p = tmp_path / "main.tsv"
        _write_tsv(p, "main", [("geonode.a.test_x", "passed"), ("geonode.a.test_y", "failed")])
        assert gate.parse_tsv(p) == {"geonode.a.test_x": "passed", "geonode.a.test_y": "failed"}

    def test_rejects_empty_file(self, tmp_path):
        p = tmp_path / "empty.tsv"
        p.write_text("", encoding="utf-8")
        with pytest.raises(ValueError, match="no data rows"):
            gate.parse_tsv(p)

    def test_rejects_bad_header(self, tmp_path):
        p = tmp_path / "bad.tsv"
        p.write_text("# suite\tmain\nwrong\theader\nfoo\tbar\n", encoding="utf-8")
        with pytest.raises(ValueError, match="unexpected header"):
            gate.parse_tsv(p)

    def test_rejects_malformed_row(self, tmp_path):
        p = tmp_path / "malformed.tsv"
        p.write_text("test_id\tstatus\nonly_one_column\n", encoding="utf-8")
        with pytest.raises(ValueError, match="malformed row"):
            gate.parse_tsv(p)


class TestParseTsvSuites:
    def test_single_suite_header(self, tmp_path):
        p = tmp_path / "main.tsv"
        _write_tsv(p, "main", [("geonode.a.test_x", "passed")])
        assert gate.parse_tsv_suites(p) == {"main"}

    def test_multiple_suite_headers_all_collected(self, tmp_path):
        # A pre-merged per-night file can legitimately carry more than one
        # `# suite` header. The old parse_tsv_suite() (singular) only
        # returned the first — this is the exact undercount finding B/C(2)
        # describes as bypassable by a narrower check.
        p = tmp_path / "merged.tsv"
        _write_tsv(p, "main", [("geonode.a.test_x", "passed")], extra_suites=("rest_apis",))
        assert gate.parse_tsv_suites(p) == {"main", "rest_apis"}

    def test_no_suite_header(self, tmp_path):
        p = tmp_path / "no_header.tsv"
        p.write_text("test_id\tstatus\nt1\tpassed\n", encoding="utf-8")
        assert gate.parse_tsv_suites(p) == set()


class TestLoadRun:
    def test_merges_disjoint_suites(self, tmp_path):
        p1, p2 = tmp_path / "smoke.tsv", tmp_path / "main.tsv"
        _write_tsv(p1, "smoke", [("geonode.smoke.test_a", "passed")])
        _write_tsv(p2, "main", [("geonode.main.test_b", "failed")])
        merged = gate.load_run([p1, p2])
        assert merged == {"geonode.smoke.test_a": "passed", "geonode.main.test_b": "failed"}

    def test_rejects_overlapping_test_ids_across_suites(self, tmp_path):
        p1, p2 = tmp_path / "s1.tsv", tmp_path / "s2.tsv"
        _write_tsv(p1, "s1", [("geonode.dup.test_a", "passed")])
        _write_tsv(p2, "s2", [("geonode.dup.test_a", "failed")])
        with pytest.raises(ValueError, match="already seen"):
            gate.load_run([p1, p2])


# --- compute_baseline (soak-immaturity tripwire + intersection semantics) --

class TestComputeBaseline:
    def test_refuses_when_below_min_runs(self):
        runs = [{"t1": "passed"}] * 3
        with pytest.raises(gate.SoakImmatureError, match="soak-immature"):
            gate.compute_baseline(runs, min_runs=14)

    def test_affirm_mature_overrides_the_tripwire(self):
        runs = [{"t1": "passed"}] * 3
        baseline = gate.compute_baseline(runs, min_runs=14, affirm_mature=True)
        assert baseline["expected_pass"] == ["t1"]
        assert baseline["soak_runs_sampled"] == 3

    def test_intersection_excludes_flaky_tests(self):
        # t1 passes every run (expected_pass); t2 is flaky (fails once) so it
        # is excluded from expected_pass but still tracked in known_tests
        # (the W0 quarantine class owns tolerating it, not silent promotion).
        runs = [
            {"t1": "passed", "t2": "passed"},
            {"t1": "passed", "t2": "failed"},
            {"t1": "passed", "t2": "passed"},
        ]
        baseline = gate.compute_baseline(runs, min_runs=3)
        assert baseline["expected_pass"] == ["t1"]
        assert baseline["known_tests"] == ["t1", "t2"]

    def test_xfail_counts_as_pass_xpass_does_not(self):
        runs = [
            {"t1": "xfail", "t2": "xpass"},
            {"t1": "xfail", "t2": "xpass"},
            {"t1": "xfail", "t2": "xpass"},
        ]
        baseline = gate.compute_baseline(runs, min_runs=3)
        assert baseline["expected_pass"] == ["t1"]

    def test_empty_runs_raises(self):
        with pytest.raises(ValueError):
            gate.compute_baseline([], min_runs=0)

    def test_pin_and_load_round_trip(self, tmp_path):
        runs = [{"t1": "passed"}] * 3
        baseline = gate.compute_baseline(runs, min_runs=3)
        out = tmp_path / "baseline.json"
        gate.save_baseline(baseline, out)
        loaded = gate.load_baseline(out)
        assert loaded == baseline
        # Written as real JSON, not a repr — a human/CODEOWNERS reviewer must
        # be able to read a re-pin diff in a normal PR view.
        json.loads(out.read_text(encoding="utf-8"))


# --- compare (the actual PR-gate comparator) --------------------------------

class TestCompare:
    @pytest.fixture
    def baseline(self):
        return {
            "known_tests": ["stable.test_a", "flaky.test_b"],
            "expected_pass": ["stable.test_a"],
        }

    def test_ok_when_nothing_changed(self, baseline):
        current = {"stable.test_a": "passed", "flaky.test_b": "failed"}
        result = gate.compare(current, baseline)
        assert result["ok"] is True
        assert result["regressions"] == []

    def test_regression_on_baseline_expected_pass_now_failing(self, baseline):
        current = {"stable.test_a": "failed", "flaky.test_b": "passed"}
        result = gate.compare(current, baseline)
        assert result["ok"] is False
        assert result["regressions"] == ["stable.test_a"]

    def test_flaky_known_test_failing_is_not_a_regression(self, baseline):
        # flaky.test_b is known but never was expected_pass (excluded at pin
        # time for being flaky) — a PR run seeing it fail must NOT gate.
        current = {"stable.test_a": "passed", "flaky.test_b": "failed"}
        result = gate.compare(current, baseline)
        assert result["ok"] is True

    def test_new_test_must_be_green(self, baseline):
        # Not in known_tests at all -> Hydrata-added-tests-must-be-green.
        current = {"stable.test_a": "passed", "flaky.test_b": "passed", "brand.new_test": "failed"}
        result = gate.compare(current, baseline)
        assert result["ok"] is False
        assert result["new_test_failures"] == ["brand.new_test"]

    def test_new_test_error_status_is_also_red(self, baseline):
        current = {"stable.test_a": "passed", "flaky.test_b": "passed", "brand.new_test": "error"}
        result = gate.compare(current, baseline)
        assert result["ok"] is False
        assert result["new_test_failures"] == ["brand.new_test"]

    def test_new_test_passing_is_fine(self, baseline):
        current = {"stable.test_a": "passed", "flaky.test_b": "passed", "brand.new_test": "passed"}
        result = gate.compare(current, baseline)
        assert result["ok"] is True

    def test_new_test_skipped_is_informational_not_red(self, baseline):
        # TASK-2302 finding f2: a brand-new test that is merely skipped
        # (e.g. an environment-gated skipif) is not evidence it's broken.
        current = {"stable.test_a": "passed", "flaky.test_b": "passed", "brand.new_test": "skipped"}
        result = gate.compare(current, baseline)
        assert result["ok"] is True
        assert result["new_test_failures"] == []
        assert result["new_test_informational"] == ["brand.new_test"]

    def test_new_test_xpass_is_informational_not_red(self, baseline):
        # A stale xfail marker on an otherwise-fine new test is a lint
        # concern (TASK-2293/2304's xfail ratchet), not a red here.
        current = {"stable.test_a": "passed", "flaky.test_b": "passed", "brand.new_test": "xpass"}
        result = gate.compare(current, baseline)
        assert result["ok"] is True
        assert result["new_test_failures"] == []
        assert result["new_test_informational"] == ["brand.new_test"]

    def test_missing_baseline_test_is_a_regression_not_silence(self, baseline):
        # The suite that would have run stable.test_a didn't run at all this
        # time (e.g. a suite job crashed before producing output) — absence
        # must gate, not pass-by-default.
        current = {"flaky.test_b": "passed"}
        result = gate.compare(current, baseline)
        assert result["ok"] is False
        assert result["missing_from_run"] == ["stable.test_a"]

    def test_format_report_is_human_readable(self, baseline):
        current = {"stable.test_a": "failed"}
        result = gate.compare(current, baseline)
        report = gate.format_report(result)
        assert "stable.test_a" in report
        assert "REGRESSIONS" in report

    def test_format_report_shows_informational_section(self, baseline):
        current = {"stable.test_a": "passed", "flaky.test_b": "passed", "brand.new_test": "skipped"}
        result = gate.compare(current, baseline)
        report = gate.format_report(result)
        assert "NEW-TEST INFORMATIONAL" in report
        assert "brand.new_test" in report


# --- tombstones (TASK-2320) ---------------------------------------------------

class TestCompareTombstones:
    @pytest.fixture
    def baseline(self):
        return {
            "known_tests": ["stable.test_a", "deleted.test_z", "flaky.test_b"],
            "expected_pass": ["stable.test_a", "deleted.test_z"],
            "tombstones": {
                "deleted.test_z": {
                    "reason": "removed dead coverage in cleanup PR #2320",
                    "ticket": "TASK-2320",
                    "date": "2026-07-19",
                },
            },
        }

    def test_tombstoned_test_absent_is_not_missing(self, baseline):
        # deleted.test_z is gone from the run entirely -- exactly what a
        # legitimate deletion looks like. Must NOT gate.
        current = {"stable.test_a": "passed", "flaky.test_b": "failed"}
        result = gate.compare(current, baseline)
        assert result["ok"] is True
        assert result["missing_from_run"] == []
        assert result["tombstone_still_present"] == []

    def test_tombstoned_test_still_present_and_failing_still_gates(self, baseline):
        # The tombstone claim was wrong (or stale) -- the test still exists
        # and is failing. Must NOT be silently swallowed by the tombstone.
        current = {"stable.test_a": "passed", "deleted.test_z": "failed"}
        result = gate.compare(current, baseline)
        assert result["ok"] is False
        assert result["tombstone_still_present"] == ["deleted.test_z"]

    def test_tombstoned_test_still_present_even_if_passing_still_gates(self, baseline):
        # Passing doesn't rescue it either -- a tombstone means "this test
        # does not exist"; if it does, the tombstone entry is simply false
        # and must be pruned/investigated, not silently trusted forever.
        current = {"stable.test_a": "passed", "deleted.test_z": "passed"}
        result = gate.compare(current, baseline)
        assert result["ok"] is False
        assert result["tombstone_still_present"] == ["deleted.test_z"]

    def test_baseline_without_tombstones_key_is_unaffected(self):
        # Backwards compatibility: a baseline pinned before TASK-2320 has no
        # "tombstones" key at all.
        baseline = {"known_tests": ["t1"], "expected_pass": ["t1"]}
        result = gate.compare({"t1": "passed"}, baseline)
        assert result["ok"] is True
        assert result["tombstone_still_present"] == []

    def test_format_report_shows_tombstone_still_present_section(self, baseline):
        current = {"stable.test_a": "passed", "deleted.test_z": "failed"}
        result = gate.compare(current, baseline)
        report = gate.format_report(result)
        assert "TOMBSTONE STILL PRESENT" in report
        assert "deleted.test_z" in report


class TestCheckTombstoneOnlyChange:
    def _old(self, **overrides):
        base = {
            "known_tests": ["stable.test_a", "deleted.test_z"],
            "expected_pass": ["stable.test_a", "deleted.test_z"],
            "suites": ["main"],
            "pinned_at": "2026-07-19T00:00:00+00:00",
            "soak_runs_sampled": 14,
            "tombstones": {},
        }
        base.update(overrides)
        return base

    def _valid_tombstone_entry(self):
        return {"reason": "dead coverage removed", "ticket": "TASK-2320", "date": "2026-07-19"}

    def test_valid_addition_is_accepted(self):
        old = self._old()
        new = self._old(tombstones={"deleted.test_z": self._valid_tombstone_entry()})
        ok, reason = gate.check_tombstone_only_change(old, new)
        assert ok is True
        assert "deleted.test_z" in reason

    def test_refuses_when_known_tests_also_changed(self):
        # Smuggling a real re-pin through the narrow exception.
        old = self._old()
        new = self._old(
            known_tests=["stable.test_a", "deleted.test_z", "sneaky.new_test"],
            tombstones={"deleted.test_z": self._valid_tombstone_entry()},
        )
        ok, reason = gate.check_tombstone_only_change(old, new)
        assert ok is False
        assert "known_tests" in reason

    def test_refuses_when_expected_pass_also_changed(self):
        old = self._old()
        new = self._old(
            expected_pass=["stable.test_a"],
            tombstones={"deleted.test_z": self._valid_tombstone_entry()},
        )
        ok, reason = gate.check_tombstone_only_change(old, new)
        assert ok is False
        assert "expected_pass" in reason

    def test_refuses_when_a_non_allowlisted_field_is_smuggled(self):
        # W3 gapcheck regression lock: the check is whole-document-minus-tombstones
        # equality, NOT a hardcoded field allowlist. A brand-new top-level field
        # (a future schema addition the old allowlist never listed) added on a
        # tombstone-only branch must be refused — the exact smuggling channel the
        # allowlist->whole-doc change closed. The other refuse-tests only mutate
        # known_tests/expected_pass (which the OLD allowlist already covered), so
        # this is the only test that fails under the pre-fix code.
        old = self._old()
        new = self._old(
            tombstones={"deleted.test_z": self._valid_tombstone_entry()},
            quarantine=["stable.test_a"],  # not a field any old allowlist listed
        )
        ok, reason = gate.check_tombstone_only_change(old, new)
        assert ok is False
        assert "quarantine" in reason

    def test_refuses_when_existing_tombstone_removed(self):
        old = self._old(tombstones={"deleted.test_z": self._valid_tombstone_entry()})
        new = self._old(tombstones={})
        ok, reason = gate.check_tombstone_only_change(old, new)
        assert ok is False
        assert "removed or edited" in reason

    def test_refuses_when_existing_tombstone_edited(self):
        old = self._old(tombstones={"deleted.test_z": self._valid_tombstone_entry()})
        edited = dict(self._valid_tombstone_entry())
        edited["reason"] = "a different reason now"
        new = self._old(tombstones={"deleted.test_z": edited})
        ok, reason = gate.check_tombstone_only_change(old, new)
        assert ok is False
        assert "removed or edited" in reason

    def test_refuses_when_no_new_tombstones(self):
        old = self._old()
        new = self._old()
        ok, reason = gate.check_tombstone_only_change(old, new)
        assert ok is False
        assert "nothing for this exception to authorize" in reason

    def test_refuses_tombstoning_a_test_not_in_known_tests(self):
        # Not verifiable — this function cannot see the code diff, but it CAN
        # refuse a claim about a test the baseline never even knew about
        # ("must require the deletion to be verifiable... not just an
        # assertion" — TASK-2320 AC).
        old = self._old()
        new = self._old(tombstones={"never.existed": self._valid_tombstone_entry()})
        ok, reason = gate.check_tombstone_only_change(old, new)
        assert ok is False
        assert "not in known_tests" in reason

    def test_refuses_tombstone_missing_required_fields(self):
        old = self._old()
        new = self._old(tombstones={"deleted.test_z": {"reason": "dead coverage"}})  # no ticket/date
        ok, reason = gate.check_tombstone_only_change(old, new)
        assert ok is False
        assert "missing required field" in reason


class TestRepinGovernanceTombstoneBranch:
    BASELINE_PATH = ".github/geonode-baseline.json"

    def _old(self):
        return {
            "known_tests": ["stable.test_a", "deleted.test_z"],
            "expected_pass": ["stable.test_a", "deleted.test_z"],
            "suites": ["main"],
            "pinned_at": "2026-07-19T00:00:00+00:00",
            "soak_runs_sampled": 14,
            "tombstones": {},
        }

    def _new_valid(self):
        new = self._old()
        new["tombstones"] = {
            "deleted.test_z": {"reason": "dead coverage removed", "ticket": "TASK-2320", "date": "2026-07-19"},
        }
        return new

    def test_tombstone_branch_without_baseline_content_is_refused(self):
        # Fail closed: the branch NAME alone is not proof of a tombstone-only
        # change — old/new baseline content must be supplied to verify it.
        ok, reason = gate.check_repin_governance(
            "test-tombstone/2320-dead-coverage", {self.BASELINE_PATH}, self.BASELINE_PATH,
        )
        assert ok is False
        assert "not supplied" in reason

    def test_tombstone_branch_with_valid_change_is_authorized(self):
        ok, reason = gate.check_repin_governance(
            "test-tombstone/2320-dead-coverage", {self.BASELINE_PATH}, self.BASELINE_PATH,
            old_baseline=self._old(), new_baseline=self._new_valid(),
        )
        assert ok is True

    def test_tombstone_branch_smuggling_a_repin_is_refused(self):
        sneaky = self._new_valid()
        sneaky["expected_pass"] = ["stable.test_a"]  # dropped without a tombstone
        ok, reason = gate.check_repin_governance(
            "test-tombstone/2320-dead-coverage", {self.BASELINE_PATH}, self.BASELINE_PATH,
            old_baseline=self._old(), new_baseline=sneaky,
        )
        assert ok is False
        assert "not tombstone-only" in reason

    def test_ordinary_branch_still_refused_regardless_of_baseline_content(self):
        # An ordinary (non-governed) branch attempting the narrow exception
        # by naming convention alone must still be refused (TASK-2320 AC:
        # "an attempt to tombstone/narrow-re-pin on an ordinary branch being
        # refused same as today's baseline-touch guard").
        ok, reason = gate.check_repin_governance(
            "ci/2311-fix-thumb-test", {self.BASELINE_PATH}, self.BASELINE_PATH,
            old_baseline=self._old(), new_baseline=self._new_valid(),
        )
        assert ok is False
        assert "upstream-sync" in reason
        assert "test-tombstone" in reason

    def test_upstream_sync_branch_may_still_touch_tombstones_directly(self):
        # The full re-pin path remains a superset — an upstream-sync PR can
        # touch tombstones (or anything else) without going through the
        # narrow verification, same as TASK-2302's existing behavior.
        ok, reason = gate.check_repin_governance(
            "upstream-sync/2026-08-01", {self.BASELINE_PATH}, self.BASELINE_PATH,
        )
        assert ok is True


# --- two-step re-pin governance ---------------------------------------------

class TestRepinGovernance:
    BASELINE_PATH = ".github/geonode-baseline.json"

    def test_ordinary_pr_may_not_touch_baseline(self):
        ok, reason = gate.check_repin_governance(
            "ci/2311-fix-thumb-test", {self.BASELINE_PATH, "geonode/thumbs/tests/test_unit.py"}, self.BASELINE_PATH
        )
        assert ok is False
        assert "upstream-sync" in reason

    def test_upstream_sync_branch_may_touch_baseline(self):
        ok, reason = gate.check_repin_governance(
            "upstream-sync/2026-08-01", {self.BASELINE_PATH, "geonode/some/upstream_file.py"}, self.BASELINE_PATH
        )
        assert ok is True

    def test_ordinary_pr_not_touching_baseline_is_fine(self):
        ok, reason = gate.check_repin_governance(
            "ci/2311-fix-thumb-test", {"geonode/thumbs/tests/test_unit.py"}, self.BASELINE_PATH
        )
        assert ok is True

    def test_upstream_sync_branch_not_touching_baseline_is_fine_too(self):
        # A sync that happens not to need a re-pin this time is not an error.
        ok, reason = gate.check_repin_governance(
            "upstream-sync/2026-08-01", {"geonode/some/upstream_file.py"}, self.BASELINE_PATH
        )
        assert ok is True

    def test_r6_scenario_upstream_merge_plus_hydrata_change_same_pr_is_blocked(self):
        # The exact hole brief §3 R6 describes: an upstream-merge PR that
        # ALSO carries a Hydrata change, on a branch that isn't the
        # dedicated sync branch, re-pinning the baseline.
        ok, reason = gate.check_repin_governance(
            "feature/2400-some-hydrata-feature",
            {self.BASELINE_PATH, "geonode/some/upstream_file.py", "geonode/hydrata_patch.py"},
            self.BASELINE_PATH,
        )
        assert ok is False


# --- CLI smoke tests (argparse wiring) ---------------------------------------

class TestCliPinSingleNight:
    def test_bare_single_tsv_still_works(self, tmp_path):
        # A single bare --tsv is unambiguous shorthand for "one night, one
        # file" and stays legal.
        p = tmp_path / "run0.tsv"
        _write_tsv(p, "main", [("t1", "passed")])
        out = tmp_path / "baseline.json"
        rc = gate.main(["pin", "--out", str(out), "--tsv", str(p)])
        assert rc == 1  # soak-immature (only 1 run) — expected refusal, not a crash
        assert not out.exists()

    def test_multiple_bare_tsv_is_refused_with_guidance(self, tmp_path, capsys):
        # TASK-2302 finding C(1): this used to silently mis-pin. Now it's a
        # hard error pointing at --night.
        p1 = tmp_path / "run0.tsv"
        p2 = tmp_path / "run1.tsv"
        _write_tsv(p1, "main", [("t1", "passed")])
        _write_tsv(p2, "main", [("t1", "passed")])
        out = tmp_path / "baseline.json"
        rc = gate.main(["pin", "--out", str(out), "--tsv", str(p1), "--tsv", str(p2)])
        assert rc == 2
        assert not out.exists()
        captured = capsys.readouterr()
        assert "REFUSED" in captured.err
        assert "--night" in captured.err

    def test_tsv_and_night_combined_is_refused(self, tmp_path, capsys):
        p1 = tmp_path / "run0.tsv"
        _write_tsv(p1, "main", [("t1", "passed")])
        out = tmp_path / "baseline.json"
        rc = gate.main(["pin", "--out", str(out), "--tsv", str(p1), "--night", str(p1)])
        assert rc == 2
        captured = capsys.readouterr()
        assert "REFUSED" in captured.err

    def test_pin_refuses_immature_soak_via_cli(self, tmp_path, capsys):
        p = tmp_path / "run0.tsv"
        _write_tsv(p, "main", [("t1", "passed")])
        out = tmp_path / "baseline.json"
        rc = gate.main(["pin", "--out", str(out), "--night", str(p)])
        assert rc == 1
        assert not out.exists()
        captured = capsys.readouterr()
        assert "REFUSED" in captured.err


class TestCliPinNightGrouping:
    """TASK-2302 finding C: --night grouping fixes the multi-suite-per-night
    pin (the "6-file garbage pin" scenario) and adds the missing coverage
    the old suite did not have — a probe of these scenarios previously
    passed (rc 0) when it should have failed."""

    def _make_nights(self, tmp_path, n=3):
        main_tsvs, rest_tsvs = [], []
        for i in range(n):
            m = tmp_path / f"main{i}.tsv"
            _write_tsv(m, "main", [("geonode.a.test_x", "passed")])
            main_tsvs.append(str(m))
            r = tmp_path / f"rest{i}.tsv"
            _write_tsv(r, "rest_apis", [("geonode.b.test_y", "passed")])
            rest_tsvs.append(str(r))
        return main_tsvs, rest_tsvs

    def test_multi_suite_night_grouping_produces_correct_expected_pass(self, tmp_path, capsys):
        main_tsvs, rest_tsvs = self._make_nights(tmp_path, n=3)
        out = tmp_path / "baseline.json"
        argv = ["pin", "--out", str(out), "--i-affirm-mature-soak"]
        for m, r in zip(main_tsvs, rest_tsvs):
            argv += ["--night", f"{m},{r}"]
        rc = gate.main(argv)
        assert rc == 0
        pinned = json.loads(out.read_text(encoding="utf-8"))
        # BOTH suites' tests must be expected_pass — the old flat-file form
        # would have collapsed this to [] (finding C(1)'s false-green bug).
        assert pinned["expected_pass"] == ["geonode.a.test_x", "geonode.b.test_y"]
        assert pinned["suites"] == ["main", "rest_apis"]

    def test_every_test_regressed_check_exits_nonzero(self, tmp_path):
        # The exact probe finding C(1) called out: with a correctly-pinned
        # multi-suite baseline, a check run where EVERY test regressed must
        # fail loudly (rc 1), not silently pass (the old bug's near-empty
        # expected_pass meant there was nothing left to regress against).
        main_tsvs, rest_tsvs = self._make_nights(tmp_path, n=3)
        out = tmp_path / "baseline.json"
        argv = ["pin", "--out", str(out), "--i-affirm-mature-soak"]
        for m, r in zip(main_tsvs, rest_tsvs):
            argv += ["--night", f"{m},{r}"]
        assert gate.main(argv) == 0

        current_main = tmp_path / "current_main.tsv"
        current_rest = tmp_path / "current_rest.tsv"
        _write_tsv(current_main, "main", [("geonode.a.test_x", "failed")])
        _write_tsv(current_rest, "rest_apis", [("geonode.b.test_y", "failed")])
        rc = gate.main([
            "check", "--baseline", str(out),
            "--tsv", str(current_main), "--tsv", str(current_rest),
        ])
        assert rc == 1

    def test_inconsistent_suite_sets_across_nights_refused(self, tmp_path, capsys):
        main_tsvs, rest_tsvs = self._make_nights(tmp_path, n=3)
        out = tmp_path / "baseline.json"
        argv = ["pin", "--out", str(out), "--i-affirm-mature-soak"]
        # Night 0 and 1 are main+rest_apis; night 2 is main-only — inconsistent.
        argv += ["--night", f"{main_tsvs[0]},{rest_tsvs[0]}"]
        argv += ["--night", f"{main_tsvs[1]},{rest_tsvs[1]}"]
        argv += ["--night", main_tsvs[2]]
        rc = gate.main(argv)
        assert rc == 2
        assert not out.exists()
        captured = capsys.readouterr()
        assert "inconsistent" in captured.err

    def test_pin_records_all_suite_headers_from_a_merged_night_file(self, tmp_path, capsys):
        # TASK-2302 finding C(2): a pre-merged per-night file recording only
        # its FIRST `# suite` header let a narrower check bypass the
        # scope guard. Three nights, each a single pre-merged file carrying
        # BOTH suite headers.
        out = tmp_path / "baseline.json"
        argv = ["pin", "--out", str(out), "--i-affirm-mature-soak"]
        for i in range(3):
            merged = tmp_path / f"night{i}.tsv"
            _write_tsv(
                merged, "main",
                [("geonode.a.test_x", "passed"), ("geonode.b.test_y", "passed")],
                extra_suites=("rest_apis",),
            )
            argv += ["--night", str(merged)]
        rc = gate.main(argv)
        assert rc == 0
        pinned = json.loads(out.read_text(encoding="utf-8"))
        assert pinned["suites"] == ["main", "rest_apis"]

        # A check covering only "main" (per its own header) must now be
        # REFUSED — the merged night file's rest_apis coverage is correctly
        # on record, so the scope guard catches the narrower check instead
        # of being bypassed by the old first-header-only undercount.
        narrow_current = tmp_path / "narrow_current.tsv"
        _write_tsv(narrow_current, "main", [("geonode.a.test_x", "passed")])
        rc = gate.main(["check", "--baseline", str(out), "--tsv", str(narrow_current)])
        assert rc == 2
        captured = capsys.readouterr()
        assert "REFUSED" in captured.err
        assert "rest_apis" in captured.err


class TestCliCheck:
    def test_pin_then_check_round_trip(self, tmp_path, capsys):
        night_tsvs = []
        for i in range(3):
            p = tmp_path / f"run{i}.tsv"
            _write_tsv(p, "main", [("geonode.a.test_x", "passed")])
            night_tsvs.append(str(p))

        out = tmp_path / "baseline.json"
        argv = ["pin", "--out", str(out), "--i-affirm-mature-soak"]
        for t in night_tsvs:
            argv += ["--night", t]
        rc = gate.main(argv)
        assert rc == 0
        assert out.exists()

        current = tmp_path / "current.tsv"
        _write_tsv(current, "main", [("geonode.a.test_x", "passed")])
        rc = gate.main(["check", "--baseline", str(out), "--tsv", str(current)])
        assert rc == 0

    def test_check_exits_nonzero_on_regression(self, tmp_path):
        baseline = {"known_tests": ["t1"], "expected_pass": ["t1"]}
        bpath = tmp_path / "baseline.json"
        gate.save_baseline(baseline, bpath)

        current = tmp_path / "current.tsv"
        _write_tsv(current, "main", [("t1", "failed")])
        rc = gate.main(["check", "--baseline", str(bpath), "--tsv", str(current)])
        assert rc == 1


class TestCliRepinGuard:
    def test_repin_guard_cli(self, capsys):
        rc = gate.main([
            "repin-guard",
            "--branch", "ci/some-feature",
            "--baseline-path", ".github/geonode-baseline.json",
            "--changed-file", ".github/geonode-baseline.json",
        ])
        assert rc == 1

    def test_repin_guard_cli_tombstone_branch_round_trip(self, tmp_path, capsys):
        # TASK-2320: --old-baseline/--new-baseline wiring, end to end.
        old = {
            "known_tests": ["stable.test_a", "deleted.test_z"],
            "expected_pass": ["stable.test_a", "deleted.test_z"],
            "suites": ["main"],
            "pinned_at": "2026-07-19T00:00:00+00:00",
            "soak_runs_sampled": 14,
            "tombstones": {},
        }
        new = dict(old)
        new["tombstones"] = {
            "deleted.test_z": {"reason": "dead coverage removed", "ticket": "TASK-2320", "date": "2026-07-19"},
        }
        old_path = tmp_path / "old_baseline.json"
        new_path = tmp_path / "new_baseline.json"
        gate.save_baseline(old, old_path)
        gate.save_baseline(new, new_path)

        rc = gate.main([
            "repin-guard",
            "--branch", "test-tombstone/2320-dead-coverage",
            "--baseline-path", ".github/geonode-baseline.json",
            "--changed-file", ".github/geonode-baseline.json",
            "--old-baseline", str(old_path),
            "--new-baseline", str(new_path),
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "tombstone-only change verified" in out
