#!/usr/bin/env python3
# Copyright 2026 Anas Chari
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Frozen exploratory panel, upstream source audit and data-only fit readiness."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import tempfile

DATASET_ID = "commons-lang-exploratory-training-20261018-v3"
PHASE = "exploratory_training"
EXCLUDED_CLASSES = {
    "org.apache.commons.lang3.time.FastDateFormat_PrinterTest":
        "Calibration exposed inherited JDK-dependent assumptions; excluded from the new v3 panel, not relabelled.",
    "org.apache.commons.lang3.time.FastDateParser_TimeZoneStrategyTest":
        "Calibration exposed locale/JDK-dependent assumptions; excluded from the new v3 panel, not relabelled.",
    "org.apache.commons.lang3.time.FastDatePrinterTest":
        "Calibration exposed JDK-dependent assumptions; excluded from the new v3 panel, not relabelled.",
}


def require(ok, message):
    if not ok:
        raise ValueError(message)


def source_id(value):
    require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) is not None,
            "upstream source must be an exact lowercase 40-hex commit SHA")
    return value


def panel_inventory(full_inventory):
    """Exclude the same three names; future newly discovered classes stay included."""
    require(isinstance(full_inventory, list) and full_inventory
            and all(isinstance(name, str) and name for name in full_inventory)
            and len(set(full_inventory)) == len(full_inventory),
            "full inventory must contain unique nonempty class names")
    selected = sorted(set(full_inventory) - EXCLUDED_CLASSES.keys())
    require(selected, "predeclared training panel is empty")
    excluded = [{"test_id": name, "reason": EXCLUDED_CLASSES[name]}
                for name in sorted(set(full_inventory) & EXCLUDED_CLASSES.keys())]
    return selected, excluded


def audit_source(repo, source_sha):
    """Prove all upstream tracked files match, with only new collector files added."""
    source_id(source_sha)
    repo = Path(repo)

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=repo, text=True,
                                       stderr=subprocess.STDOUT).strip()

    require(git("cat-file", "-t", source_sha) == "commit", "upstream source is not a commit")
    official_tip = source_id(git("rev-parse", "FETCH_HEAD"))
    fetch_path = Path(git("rev-parse", "--git-path", "FETCH_HEAD"))
    if not fetch_path.is_absolute():
        fetch_path = repo / fetch_path
    fetch_lines = fetch_path.read_text().splitlines()
    provenance = [line for line in fetch_lines if line.startswith(official_tip + "\t")]
    require(len(provenance) == 1 and re.fullmatch(
        r"[0-9a-f]{40}\t(?:not-for-merge)?\tbranch 'master' of https://github.com/apache/commons-lang(?:\.git)?",
        provenance[0]) is not None,
        "FETCH_HEAD must record the workflow's public Apache master fetch")
    subprocess.run(["git", "merge-base", "--is-ancestor", source_sha, "FETCH_HEAD"],
                   cwd=repo, check=True, capture_output=True)
    require(source_sha in set(git("rev-list", "--first-parent", "FETCH_HEAD").splitlines()),
            "upstream source is not on the public master first-parent lineage")
    subprocess.run(["git", "merge-base", "--is-ancestor", source_sha, "HEAD"],
                   cwd=repo, check=True, capture_output=True)
    require(not git("status", "--porcelain", "--untracked-files=all"),
            "source audit requires a clean checkout before generated build files")
    raw = subprocess.check_output(["git", "diff", "--no-ext-diff", "--no-renames",
                                   "--name-status", "-z", source_sha, "HEAD"], cwd=repo)
    fields = raw.decode().split("\0")[:-1]
    require(len(fields) % 2 == 0, "unexpected source diff format")
    additions = []
    for status, path in zip(fields[::2], fields[1::2]):
        require(status == "A" and (path.startswith("research-pilot/")
                                  or path == ".github/workflows/shadow-pilot.yml"),
                "upstream source was modified outside added collector files: " + path)
        additions.append(path)
    return {"upstream_source_sha": source_sha,
            "upstream_source_tree_sha": git("rev-parse", source_sha + "^{tree}"),
            "collector_fork_sha": git("rev-parse", "HEAD"),
            "official_tip_sha": official_tip, "official_fetch_provenance": provenance[0],
            "source_is_ancestor": True, "official_first_parent_lineage": True,
            "original_upstream_files_unchanged": True,
            "added_collector_paths": sorted(additions)}


def readiness(events, next_decision_index=None):
    """Inspect already validated closed history; this never fits or scores a model.

    fit_eligible describes data available for a future scheduled refit, not a fitted
    model. The scorer still enforces the original scheduled cutoff and snapshots.
    """
    decisions = [row for row in events if row["type"] == "decision"]
    results, closures, sources = {}, {}, set()
    for decision in decisions:
        source = source_id(decision["upstream_source_sha"])
        require(source not in sources, "duplicate upstream primary source; retries are not new builds")
        sources.add(source)
    require(len({d["id"] for d in decisions}) == len(decisions), "duplicate decision ID")
    indices = [d["decision_index"] for d in decisions]
    require(indices == list(range(len(decisions))), "history decision indices are not contiguous")
    for row in events:
        if row["type"] == "result":
            observed = results.setdefault(row["decision_id"], {})
            require(row["test_id"] not in observed, "duplicate primary class result")
            observed[row["test_id"]] = row
        elif row["type"] == "suite_end":
            require(row["decision_id"] not in closures, "duplicate suite closure")
            closures[row["decision_id"]] = row
    eligible, incomplete, binary_rows, positive_rows = [], [], 0, 0
    for decision in decisions:
        observed = results.get(decision["id"], {})
        closure = closures.get(decision["id"])
        binary_rows += sum(r["raw_outcome"] in ("passed", "failed") for r in observed.values())
        positive_rows += sum(r["raw_outcome"] == "failed" for r in observed.values())
        binary = (set(observed) == set(decision["inventory"])
                  and all(r["raw_outcome"] in ("passed", "failed") for r in observed.values()))
        if closure and closure["status"] == "complete":
            require(binary and closure["normal_ci_full_suite"] is True,
                    "complete closure lacks its full binary panel")
            require(set(closure["result_ids"]) == {r["id"] for r in observed.values()},
                    "complete closure does not reference every panel result")
            eligible.append((decision, list(observed.values())))
        else:
            incomplete.append({"decision_id": decision["id"],
                               "reason": closure.get("incomplete_reason", "incomplete panel")
                               if closure else "no suite closure"})
    window = eligible[-200:]
    n = len(window)
    c1, c2 = int(.6 * n), int(.6 * n) + int(.2 * n)
    partitions = []
    for name, block in zip(("fit", "select", "calibrate"),
                           (window[:c1], window[c1:c2], window[c2:])):
        labels = [r["raw_outcome"] for _, rows in block for r in rows]
        positives = labels.count("failed")
        partitions.append({"name": name, "decision_ids": [d["id"] for d, _ in block],
                           "builds": len(block), "rows": len(labels),
                           "positive_rows": positives, "negative_rows": len(labels) - positives,
                           "both_classes": 0 < positives < len(labels)})
    positive_window = sum(p["positive_rows"] for p in partitions)
    minimum_builds = len(eligible) >= 50
    minimum_positives = positive_window >= 20
    both_labels = all(p["both_classes"] for p in partitions)
    reasons = []
    if not minimum_builds:
        reasons.append("insufficient_past_builds")
    if not minimum_positives:
        reasons.append("insufficient_positive_rows")
    if not both_labels:
        reasons.append("partition_lacks_both_classes")
    index = len(decisions) if next_decision_index is None else next_decision_index
    require(type(index) is int and index == len(decisions), "next decision must follow the closed history")
    return {"dataset_id": DATASET_ID, "phase": PHASE, "efficacy_eligible": False,
            "target": "raw_failed", "model_fitted_by_this_report": False,
            "fit_eligible": not reasons, "scope": "data-only readiness for a future scheduled refit",
            "decisions_seen": len(decisions), "distinct_upstream_revisions": len(sources),
            "complete_binary_builds": len(eligible), "incomplete_builds": incomplete,
            "binary_panel_rows_observed": binary_rows, "positive_panel_rows_observed": positive_rows,
            "window_builds": [d["id"] for d, _ in window], "window_positive_rows": positive_window,
            "chronological_partitions": partitions,
            "minimum_complete_builds": 50, "minimum_positive_window_rows": 20,
            "maximum_window_builds": 200, "minimum_builds_met": minimum_builds,
            "minimum_positives_met": minimum_positives, "both_labels_per_partition": both_labels,
            "complete_build_gap": max(0, 50 - len(eligible)),
            "positive_window_row_gap": max(0, 20 - positive_window),
            "next_decision_index": index, "refit_due_at_next_decision": index % 10 == 0,
            "next_scheduled_refit_index": ((index + 9) // 10) * 10,
            "blocking_reasons": reasons, "fallback_reason": reasons[0] if reasons else None}


def self_check():
    """Deliberately synthetic fixtures never become research history."""
    excluded = next(iter(EXCLUDED_CLASSES))
    selected, exclusions = panel_inventory(["suite.NewTest", excluded, "suite.ATest"])
    assert selected == ["suite.ATest", "suite.NewTest"] and exclusions[0]["test_id"] == excluded

    def history(count, positive=lambda i: False, incomplete=()):
        rows = []
        for i in range(count):
            did = "synthetic-%d" % i
            rows.append({"type": "decision", "id": did, "decision_index": i,
                         "upstream_source_sha": "%040x" % (i + 1), "inventory": ["a", "b"]})
            ids = []
            for name in ("a", "b"):
                rid = did + ":" + name
                ids.append(rid)
                outcome = "failed" if name == "a" and positive(i) else "passed"
                if name == "a" and i in incomplete:
                    outcome = "aborted"
                rows.append({"type": "result", "id": rid, "decision_id": did,
                             "test_id": name, "raw_outcome": outcome})
            rows.append({"type": "suite_end", "decision_id": did,
                         "status": "incomplete" if i in incomplete else "complete",
                         "normal_ci_full_suite": True, "result_ids": ids,
                         "incomplete_reason": "deliberate synthetic abort" if i in incomplete else None})
        return rows

    single = readiness(history(1))
    assert single["complete_binary_builds"] == 1 and single["window_positive_rows"] == 0
    assert not single["fit_eligible"] and single["complete_build_gap"] == 49
    assert single["next_scheduled_refit_index"] == 10
    assert readiness(history(2, incomplete=(1,)))["complete_binary_builds"] == 1
    missing_block = readiness(history(50, positive=lambda i: i < 30))
    assert missing_block["minimum_positives_met"] and not missing_block["both_labels_per_partition"]
    assert not missing_block["fit_eligible"]
    ready = readiness(history(50, positive=lambda i: i % 2 == 0))
    assert ready["fit_eligible"] and ready["refit_due_at_next_decision"]
    assert ready["window_positive_rows"] == 25 and not ready["model_fitted_by_this_report"]
    duplicate = history(2)
    duplicate[4]["upstream_source_sha"] = duplicate[0]["upstream_source_sha"]
    try:
        readiness(duplicate)
    except ValueError:
        pass
    else:
        raise AssertionError("duplicate upstream source admitted")
    with tempfile.TemporaryDirectory() as temp:
        repo = Path(temp)

        def git(*args):
            return subprocess.check_output(["git", "-c", "user.name=Synthetic check",
                                            "-c", "user.email=check@example.invalid", *args],
                                           cwd=repo, text=True, stderr=subprocess.STDOUT).strip()

        git("init", "--quiet")
        (repo / "source.txt").write_text("unchanged original source\n")
        git("add", ".")
        git("commit", "--quiet", "-m", "Synthetic upstream source")
        source = git("rev-parse", "HEAD")
        (repo / ".git/FETCH_HEAD").write_text(
            source + "\t\tbranch 'master' of https://github.com/apache/commons-lang\n")
        (repo / "research-pilot").mkdir()
        (repo / "research-pilot/probe.txt").write_text("synthetic collector\n")
        git("add", ".")
        git("commit", "--quiet", "-m", "Synthetic collector addition")
        audited = audit_source(repo, source)
        assert audited["official_tip_sha"] == source
        assert audited["added_collector_paths"] == ["research-pilot/probe.txt"]
        (repo / "source.txt").write_text("changed original source\n")
        try:
            audit_source(repo, source)
        except ValueError:
            pass
        else:
            raise AssertionError("dirty original source accepted")
        git("add", ".")
        git("commit", "--quiet", "-m", "Synthetic forbidden source change")
        try:
            audit_source(repo, source)
        except ValueError:
            pass
        else:
            raise AssertionError("committed original source change accepted")
        main = git("symbolic-ref", "--short", "HEAD")
        git("checkout", "--quiet", "-b", "synthetic-side", source)
        (repo / "secondary.txt").write_text("synthetic side branch\n")
        git("add", ".")
        git("commit", "--quiet", "-m", "Synthetic secondary lineage")
        side = git("rev-parse", "HEAD")
        git("checkout", "--quiet", main)
        git("merge", "--quiet", "--no-ff", "synthetic-side", "-m", "Synthetic merge")
        tip = git("rev-parse", "HEAD")
        (repo / ".git/FETCH_HEAD").write_text(
            tip + "\t\tbranch 'master' of https://github.com/apache/commons-lang\n")
        try:
            audit_source(repo, side)
        except ValueError as error:
            assert "first-parent" in str(error)
        else:
            raise AssertionError("secondary-branch source admitted as first-parent")
    print("PASS cohort checks: fixed panel, zero positives, incomplete builds, unique revisions, chronological fit gates (synthetic only)")
    print("PASS source audit: public-fetch provenance, first-parent ancestry, added collector, clean unchanged original source (synthetic repository only)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--history", type=Path, help="validated state.json, events JSON or JSONL")
    args = parser.parse_args()
    if args.self_check:
        self_check()
    elif args.history:
        text = args.history.read_text()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = [json.loads(line) for line in text.splitlines() if line.strip()]
        print(json.dumps(readiness(data["events"] if isinstance(data, dict) else data), indent=2))
    else:
        parser.error("choose --self-check or --history")
