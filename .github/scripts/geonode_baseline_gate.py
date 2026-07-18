#!/usr/bin/env python3
"""geonode differential-green comparator + two-step re-pin governance — TASK-2302
(epic 2290, W2).

Implements the operator-decided gate model for Hydrata/geonode 5.x (decision
record: docs/reports/2026-07-18-q-1-geonode-differential-green.html in
Hydrata/deploy, option A): geonode is an upstream fork and carries
upstream-inherited reds, so absolute green is unreachable without an
open-ended upstream-fixing detour. Instead:

  * Hydrata-ADDED tests (not present when the baseline was pinned) must be
    green — but a NEW test that is merely skipped or an unexpected xpass is
    informational, not a red (see ``compare()``'s new_test_informational);
    a NEW test that actually FAILED or ERRORed is still red.
  * The upstream suite must show NO REGRESSION vs a governed baseline pinned
    from the intersection of MATURE nightly-soak runs (a test is
    baseline-expected-pass only if it passed in EVERY sampled run; a test
    that is merely FLAKY in the soak goes to the W0 quarantine class
    (TASK-2293) — never silently promoted into the green baseline, and never
    silently used to fail a PR either. See ``compare()``.

Two-step re-pin governance (brief §3 R6): an upstream-merge PR that also
carries Hydrata changes could re-pin the baseline and quietly absorb a real
regression as "new normal". The baseline file may only move on a PR that is
ITSELF the upstream-sync step (branch name convention below) — ordinary
Hydrata work PRs may never touch it. See ``check_repin_governance()``. This
script lives at .github/scripts/geonode_baseline_gate.py, a path covered by
this repo's own CODEOWNERS `/.github/**` rule (TASK-2297) — the re-pin
governance this script implements is itself protected by requiring human
(CODEOWNERS) review on any change to this file, same as the baseline JSON it
guards. (Residual: an account with push rights to an *unprotected* branch
could still run this script out-of-band, off a modified copy, outside CI —
CODEOWNERS review gates changes that land in this repo, not what someone
does on their own machine with their own copy.)

Soak-immaturity is enforced structurally, not just by policy: ``pin_baseline()``
refuses (raises ``SoakImmatureError``) unless the caller affirms >=
MIN_MATURE_RUNS runs spanning >= MIN_MATURE_DAYS days — the epic's
soak-immature tripwire (constraint 2) lives in the tool, not only in the
operator's head.

CLI:
    geonode_baseline_gate.py pin --night run1.tsv --night run2.tsv ... --out baseline.json \\
        --i-affirm-mature-soak
    geonode_baseline_gate.py pin --night main0.tsv,rest_apis0.tsv --night main1.tsv,rest_apis1.tsv ... \\
        --out baseline.json --i-affirm-mature-soak
    geonode_baseline_gate.py check --baseline baseline.json --tsv current1.tsv ...
    geonode_baseline_gate.py repin-guard --branch <name> --baseline-path <path> \\
        --changed-file <path> [--changed-file <path> ...]

No third-party dependencies (stdlib only) — this must run in a bare CI step
with nothing installed yet.
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

# Soak-immaturity gate (epic TASK-2290 constraint 2): do not pin before
# 2026-07-31 + >=14 soak runs. This tool enforces the run-count half
# structurally; the date half is the caller's responsibility (the soak
# fires nightly, so 14 runs ~= 14 days in practice, but the tool checks
# what it's actually given rather than trusting a clock).
MIN_MATURE_RUNS = 14

# Statuses that count as "the test ran and passed". xfail is an accepted-pass
# for baseline purposes (a known, tracked, non-strict xfail is not a
# regression signal); xpass is NOT accepted here — an unexpectedly-passing
# xfail is a real signal (the marker is stale) but is out of this gate's
# concern (TASK-2293/2304 own the xfail ratchet) and should not silently
# count as a fresh "pass" toward the baseline.
PASS_STATUSES = frozenset({"passed", "xfail"})

# Statuses for a NEW (not-in-baseline) test that are informational rather
# than red (TASK-2302 finding f2). A brand-new test that is merely skipped
# (e.g. an environment-gated skipif) or an unexpected xpass (a stale xfail
# marker on a test that is otherwise fine) is not evidence the new test is
# broken — only an actual failed/error status on a new test gates the PR.
NEW_TEST_INFORMATIONAL_STATUSES = frozenset({"skipped", "xpass"})


class SoakImmatureError(RuntimeError):
    """Raised by pin_baseline() when fewer than MIN_MATURE_RUNS runs are given."""


class RepinGovernanceError(RuntimeError):
    """Raised by check_repin_governance() when a non-sync PR touches the baseline."""


# --- TSV parsing (soak-nightly.yml's own format — see this repo's
# .github/workflows/soak-nightly.yml "Parse per-test results" step) ---------

def parse_tsv(path):
    """Parse one soak-results TSV into {test_id: status}.

    Format (written by soak-nightly.yml): two leading ``# key\\tvalue`` comment
    lines (suite, run_utc), a ``test_id\\tstatus`` header, then one row per
    test. Tolerant of blank lines; raises ValueError on a malformed data row
    so a corrupt artifact fails loudly rather than silently pinning a partial
    baseline.
    """
    results = {}
    with open(path, encoding="utf-8") as fh:
        lines = [line.rstrip("\n") for line in fh]

    data_lines = [line for line in lines if line and not line.startswith("#")]
    if not data_lines:
        raise ValueError(f"{path}: no data rows found")

    header = data_lines[0].split("\t")
    if header != ["test_id", "status"]:
        raise ValueError(f"{path}: unexpected header {header!r} (expected ['test_id', 'status'])")

    for i, line in enumerate(data_lines[1:], start=1):
        parts = line.split("\t")
        if len(parts) != 2:
            raise ValueError(f"{path}: malformed row {i} ({line!r})")
        test_id, status = parts
        results[test_id] = status
    return results


def parse_tsv_suites(path):
    """Read ALL ``# suite\\t<name>`` comment lines from a TSV, as a set
    (empty if none). A file is normally single-suite (one header), but a
    pre-merged multi-suite night file can legitimately carry more than one
    header line — the earlier implementation returned only the FIRST match
    (TASK-2302 finding B/C: this let a pre-merged file's baseline-suite
    bookkeeping under-report what it actually covers, which meant the
    check-side scope guard in ``_cmd_check`` could be bypassed by a check
    run narrower than the baseline actually needs). Collecting every header
    line closes that hole.
    """
    suites = set()
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if line.startswith("# suite\t"):
                suites.add(line.split("\t", 1)[1])
    return suites


def load_run(paths):
    """Merge one run's per-suite TSVs (e.g. smoke.tsv + main.tsv + ...) into
    one {test_id: status} dict for that run. Suites partition disjoint test
    IDs (each test belongs to exactly one soak-nightly.yml matrix suite), so
    a later file overwriting an earlier key would indicate a genuine
    collision worth surfacing rather than silently masking.
    """
    merged = {}
    for path in paths:
        parsed = parse_tsv(path)
        overlap = set(parsed) & set(merged)
        if overlap:
            raise ValueError(f"{path}: {len(overlap)} test_id(s) already seen in this run (e.g. {sorted(overlap)[0]!r})")
        merged.update(parsed)
    return merged


# --- Baseline pinning (intersection-of-mature-runs) -------------------------

def compute_baseline(runs, *, min_runs=MIN_MATURE_RUNS, affirm_mature=False):
    """Build the baseline dict from a list of per-run {test_id: status} dicts.

    Each element of ``runs`` MUST already be one fully-merged night (see
    ``load_run``) — passing one suite's TSV per element when a night spans
    multiple suites silently collapses ``expected_pass`` to near-empty (a
    test only present in ITS OWN suite's file fails the
    all-runs-must-have-it intersection check against every OTHER suite's
    file treated as a separate "run") — this was TASK-2302 finding C(1), a
    false-green: a PR where every test regressed still passed because there
    was almost nothing left in expected_pass to regress against. The CLI's
    ``--night`` flag (see ``_cmd_pin``) is what prevents a caller from
    passing suite-files directly here without merging first.

    A test is ``expected_pass`` only if it PASSED (see PASS_STATUSES) in
    EVERY sampled run — the intersection, not the union or majority. A test
    that is present but flaky (passes in some runs, not others) is
    deliberately excluded from expected_pass (the W0 quarantine class,
    TASK-2293, is the only legal home for tolerated flakes) but stays in
    known_tests so it is not mistaken for a brand-new test later.

    Raises SoakImmatureError if fewer than ``min_runs`` runs are supplied and
    the caller has not passed ``affirm_mature=True`` — the soak-immaturity
    tripwire lives here, not only in operator memory. ``affirm_mature`` exists
    so a test can exercise this function with a small fixture without
    faking 14 real runs; the CLI only exposes it as an explicit, logged
    override flag (--i-affirm-mature-soak), never a default.
    """
    if len(runs) < min_runs and not affirm_mature:
        raise SoakImmatureError(
            f"only {len(runs)} run(s) supplied; the epic's soak-immature tripwire "
            f"(TASK-2290 constraint 2) requires >= {min_runs} mature soak runs before "
            f"a baseline may be pinned. Pass affirm_mature=True (CLI: "
            f"--i-affirm-mature-soak) only if you have independently verified soak "
            f"maturity (>= 2026-07-31, >= {min_runs} runs) — this flag does not check "
            f"the calendar for you."
        )
    if not runs:
        raise ValueError("compute_baseline() called with zero runs")

    known_tests = set()
    for run in runs:
        known_tests |= set(run)

    expected_pass = set()
    for test_id in known_tests:
        if all(run.get(test_id) in PASS_STATUSES for run in runs):
            expected_pass.add(test_id)

    return {
        "pinned_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "soak_runs_sampled": len(runs),
        "known_tests": sorted(known_tests),
        "expected_pass": sorted(expected_pass),
    }


def load_baseline(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save_baseline(baseline, path):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(baseline, fh, indent=2, sort_keys=True)
        fh.write("\n")


# --- Differential-green comparator ------------------------------------------

def compare(current, baseline):
    """Compare one current run's {test_id: status} against a pinned baseline.

    Returns a dict:
      regressions           -- baseline-expected-pass tests that did NOT pass
                                now (upstream no-regression violation)
      new_test_failures     -- tests absent from baseline.known_tests that
                                actually FAILED/ERRORed now (Hydrata-added-
                                tests-must-be-green violation)
      new_test_informational -- tests absent from baseline.known_tests that
                                are merely skipped or an unexpected xpass —
                                reported for visibility but NOT a red (a new
                                test isn't "broken" just because it's
                                environment-skipped or has a stale xfail
                                marker; TASK-2302 finding f2)
      missing_from_run       -- baseline-expected-pass tests that are absent
                                from this run entirely (a suite that didn't
                                run is itself a regression signal, not
                                silence)
      ok                     -- True iff regressions, new_test_failures and
                                missing_from_run are all empty
                                (new_test_informational never affects ok)
    """
    known_tests = set(baseline["known_tests"])
    expected_pass = set(baseline["expected_pass"])

    regressions = sorted(
        t for t in expected_pass if t in current and current[t] not in PASS_STATUSES
    )
    missing_from_run = sorted(t for t in expected_pass if t not in current)
    new_tests_not_passing = {
        t: current[t] for t in current if t not in known_tests and current[t] not in PASS_STATUSES
    }
    new_test_failures = sorted(
        t for t, status in new_tests_not_passing.items() if status not in NEW_TEST_INFORMATIONAL_STATUSES
    )
    new_test_informational = sorted(
        t for t, status in new_tests_not_passing.items() if status in NEW_TEST_INFORMATIONAL_STATUSES
    )

    return {
        "regressions": regressions,
        "new_test_failures": new_test_failures,
        "new_test_informational": new_test_informational,
        "missing_from_run": missing_from_run,
        "ok": not (regressions or new_test_failures or missing_from_run),
    }


def format_report(result):
    lines = []
    if result["regressions"]:
        lines.append(f"REGRESSIONS ({len(result['regressions'])}) — baseline-expected-pass, now failing:")
        lines.extend(f"  - {t}" for t in result["regressions"])
    if result["missing_from_run"]:
        lines.append(f"MISSING ({len(result['missing_from_run'])}) — baseline-expected-pass, absent from this run:")
        lines.extend(f"  - {t}" for t in result["missing_from_run"])
    if result["new_test_failures"]:
        lines.append(f"NEW-TEST FAILURES ({len(result['new_test_failures'])}) — not in baseline, must be green:")
        lines.extend(f"  - {t}" for t in result["new_test_failures"])
    if result.get("new_test_informational"):
        lines.append(
            f"NEW-TEST INFORMATIONAL ({len(result['new_test_informational'])}) — not in baseline, "
            f"skipped/xpass (not gating):"
        )
        lines.extend(f"  - {t}" for t in result["new_test_informational"])
    if result["ok"]:
        lines.append("OK — no regressions, no new-test failures, nothing missing.")
    return "\n".join(lines)


# --- Two-step re-pin governance ---------------------------------------------

# A re-pin may only happen on a branch that IS the dedicated upstream-sync
# step (brief §3 R6: "upstream-merge+re-pin in PR 1, Hydrata work in PR 2").
# Mechanical, CI-checkable proxy: the branch name convention below. This does
# not (and cannot, from a file list alone) verify the diff is semantically an
# upstream merge — that is what CODEOWNERS human review is for (the baseline
# file is on this repo's protected-path list, TASK-2297/CODEOWNERS) — it only
# closes the R6 hole that an ORDINARY Hydrata PR could quietly re-pin.
UPSTREAM_SYNC_BRANCH_PREFIX = "upstream-sync/"


def check_repin_governance(branch_name, changed_files, baseline_relpath):
    """Two-step re-pin governance check (TASK-2302 AC3).

    Returns (ok: bool, reason: str). Raises nothing — callers (CLI, CI step)
    decide how to surface a False result.
    """
    baseline_touched = baseline_relpath in changed_files
    is_upstream_sync = branch_name.startswith(UPSTREAM_SYNC_BRANCH_PREFIX)

    if baseline_touched and not is_upstream_sync:
        return False, (
            f"{baseline_relpath} is changed on branch {branch_name!r}, which is not an "
            f"{UPSTREAM_SYNC_BRANCH_PREFIX}* branch. The baseline may only be re-pinned "
            f"in the dedicated upstream-sync PR (brief R6: upstream-merge+re-pin in PR 1, "
            f"Hydrata work in PR 2) — split this change into two PRs."
        )
    return True, "ok"


# --- CLI ---------------------------------------------------------------------

def _cmd_pin(args):
    # --night groups TSV paths that together make up ONE soak night
    # (comma-separated if the night spans multiple suite files). This is
    # the fix for TASK-2302 finding C(1): the old flat `--tsv a --tsv b`
    # form treated EACH file as its own independent "run", so pinning from
    # >1 suite per night collapsed expected_pass to near-empty (see the
    # compute_baseline() docstring). A single bare --tsv is still accepted
    # as shorthand for "one night, one file" (unambiguous); more than one
    # bare --tsv is refused outright rather than silently mis-pinning.
    if args.tsv and args.night:
        print("REFUSED: --tsv and --night may not be combined in the same invocation — use --night exclusively "
              "once you have more than one night's data.", file=sys.stderr)
        return 2

    if args.tsv:
        if len(args.tsv) > 1:
            print(
                "REFUSED: multiple bare --tsv files is the old ambiguous flat form (TASK-2302 finding C). "
                "Each --tsv used to be silently treated as its own independent 'run' — pinning a baseline "
                "from more than one suite per night collapsed expected_pass to near-empty, because a test "
                "only present in its own suite's file failed the all-runs-must-have-it intersection check "
                "against every OTHER suite's file (treated as if it were a separate run that should also "
                "have contained that test). Use --night instead: one `--night <path[,path,...]>` per soak "
                "night, comma-separating multiple suite TSVs that belong to the SAME night, e.g.\n"
                "  --night main0.tsv,rest_apis0.tsv --night main1.tsv,rest_apis1.tsv --night main2.tsv,rest_apis2.tsv",
                file=sys.stderr,
            )
            return 2
        night_specs = [args.tsv]
    elif args.night:
        night_specs = [spec.split(",") for spec in args.night]
    else:
        print("REFUSED: pin requires at least one --night (or exactly one bare --tsv).", file=sys.stderr)
        return 2

    night_paths = [[Path(p) for p in paths] for paths in night_specs]

    try:
        runs = [load_run(paths) for paths in night_paths]
    except ValueError as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2

    try:
        baseline = compute_baseline(runs, affirm_mature=args.i_affirm_mature_soak)
    except SoakImmatureError as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 1

    # SUITE-SCOPE GUARD: record which suites this baseline was pinned from
    # (every `# suite` header actually present across every file of every
    # night — see parse_tsv_suites(), which fixed the first-header-only
    # undercount from finding B/C(2)). Nights must all cover the SAME suite
    # set, or the baseline's own suite bookkeeping would be ambiguous about
    # what it actually represents.
    per_night_suites = [sorted(set().union(*(parse_tsv_suites(p) for p in paths))) for paths in night_paths]
    distinct = {tuple(s) for s in per_night_suites}
    if len(distinct) > 1:
        print(
            f"REFUSED: nights carry inconsistent suite sets — every night must cover the same suites. "
            f"Got: {per_night_suites!r}",
            file=sys.stderr,
        )
        return 2
    suites = per_night_suites[0] if per_night_suites else []

    # A PR-gate `check` run that does not cover every suite recorded here
    # would otherwise report a flood of spurious `missing_from_run`
    # "regressions" for suites it never ran — see the check-side guard
    # below, which refuses rather than emits that false signal. Concretely:
    # today's geonode differential_green workflow job only runs the `main` +
    # `rest_apis` suites (not the full soak's 7), so whoever eventually pins
    # the real baseline (post-2026-07-31) MUST pin it from those same two
    # suites' soak data, not the full soak — widen both together, never
    # just one.
    baseline["suites"] = suites
    save_baseline(baseline, args.out)
    print(f"Pinned baseline from {len(runs)} night(s) (suites: {', '.join(suites) or 'unknown'}): "
          f"{len(baseline['expected_pass'])}/{len(baseline['known_tests'])} tests expected-pass -> {args.out}")
    return 0


def _cmd_check(args):
    baseline = load_baseline(args.baseline)
    current = load_run(args.tsv)

    baseline_suites = set(baseline.get("suites", []))
    if baseline_suites:
        check_suites = set().union(*(parse_tsv_suites(p) for p in args.tsv)) if args.tsv else set()
        uncovered = baseline_suites - check_suites
        if uncovered:
            print(
                f"REFUSED: baseline was pinned from suite(s) {sorted(baseline_suites)} but this "
                f"check only covers {sorted(check_suites)} — missing {sorted(uncovered)}. "
                f"Checking a narrower suite scope than the baseline covers would report every "
                f"uncovered suite's baseline-expected-pass tests as false 'missing' regressions. "
                f"Either widen this check to cover all baseline suites, or re-pin the baseline "
                f"scoped to exactly the suites this check runs.",
                file=sys.stderr,
            )
            return 2

    result = compare(current, baseline)
    print(format_report(result))
    return 0 if result["ok"] else 1


def _cmd_repin_guard(args):
    ok, reason = check_repin_governance(args.branch, set(args.changed_file), args.baseline_path)
    print(reason)
    return 0 if ok else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_pin = sub.add_parser("pin", help="Pin a baseline from N soak nights")
    p_pin.add_argument("--tsv", action="append", default=[],
                        help="a single night, single file (unambiguous shorthand). Repeating this flag for "
                             "more than one file is refused — use --night instead.")
    p_pin.add_argument("--night", action="append", default=[], dest="night",
                        help="one soak night's TSV path(s), comma-separated if the night spans multiple suite "
                             "files (repeatable — pass once per night)")
    p_pin.add_argument("--out", type=Path, required=True)
    p_pin.add_argument("--i-affirm-mature-soak", action="store_true",
                        help="override the >=%d-run soak-immature tripwire (only after independently "
                             "verifying >=2026-07-31 and >=%d runs)" % (MIN_MATURE_RUNS, MIN_MATURE_RUNS))
    p_pin.set_defaults(func=_cmd_pin)

    p_check = sub.add_parser("check", help="Compare current-run TSVs against a pinned baseline")
    p_check.add_argument("--baseline", type=Path, required=True)
    p_check.add_argument("--tsv", action="append", required=True, help="one current-run TSV (repeatable)")
    p_check.set_defaults(func=_cmd_check)

    p_guard = sub.add_parser("repin-guard", help="Enforce two-step re-pin governance on a PR diff")
    p_guard.add_argument("--branch", required=True)
    p_guard.add_argument("--baseline-path", required=True, help="baseline file path relative to repo root")
    p_guard.add_argument("--changed-file", action="append", required=True, dest="changed_file")
    p_guard.set_defaults(func=_cmd_repin_guard)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
