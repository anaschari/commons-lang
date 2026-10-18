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

"""Prospective shadow adapter for the unchanged Article 2 model."""
import argparse
import hashlib
import json
import math
import time
from pathlib import Path

import frozen_model as frozen

FROZEN_SHA256 = "eb64450a11c1c4515896d6cbdfb555f892d3590cc136c124768e1681704d62a1"
ROOT = Path(__file__).resolve().parent
if hashlib.sha256((ROOT / "frozen_model.py").read_bytes()).hexdigest() != FROZEN_SHA256:
    raise ValueError("Article 2 frozen model bytes changed")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("expected finite nonnegative number")
    return value


def score(inventory, prior_closed_events, decision_index, *, decision_id, cutoff_at,
          project, environment, acquisition_s=0.):
    """Score only supplied prospective events available at cutoff_at.

    Rule scores are display ranks (ascending), with their actual ordering keys
    saved separately. No estimated duration is invented during bootstrap.
    The latest scheduled refit is reconstructed from its original cutoff and
    frozen feature snapshots; reconstruction work is charged on every call.
    """
    feature_clock = frozen.Clock()
    number(cutoff_at)
    number(acquisition_s)
    if type(decision_index) is not int or decision_index < 0:
        raise ValueError("decision_index must be a nonnegative integer")
    if not inventory or any(not isinstance(n, str) or not n for n in inventory) or len(set(inventory)) != len(inventory):
        raise ValueError("inventory must contain unique nonempty test IDs")
    names = sorted(inventory)
    decisions, results, closures, ids = {}, {}, {}, set()
    for event in prior_closed_events:
        if event["id"] in ids:
            raise ValueError("duplicate historical event ID")
        ids.add(event["id"])
        if event["type"] == "decision":
            if (event["project"], event["environment"]) != (project, environment):
                continue
            if event["feature_target"] != "raw_failed":
                raise ValueError("adapter admits only unchanged raw_failed feature target")
            if (event["id"] == decision_id or event["decision_at"] >= cutoff_at
                    or event["decision_index"] >= decision_index or number(event["recorded_at"]) > cutoff_at):
                continue
            if digest(event["features"]) != event["features_sha256"]:
                raise ValueError("original feature snapshot hash mismatch")
            if not (number(event["history_cutoff_at"]) <= number(event["features_frozen_at"])
                    <= number(event["decision_at"]) <= number(event["recorded_at"])):
                raise ValueError("historical feature snapshot was not frozen before decision")
            decisions[event["id"]] = event
        elif event["type"] == "result":
            results[event["id"]] = event
        elif event["type"] == "suite_end":
            if event["decision_id"] in closures:
                raise ValueError("duplicate historical suite closure")
            closures[event["decision_id"]] = event
    if len({d["decision_index"] for d in decisions.values()}) != len(decisions):
        raise ValueError("duplicate historical decision index")

    released, per_decision = [], {}
    nonbinary = unavailable = 0
    for result in results.values():
        source = decisions.get(result["decision_id"])
        if source is None:
            continue
        if result["test_id"] not in source["inventory"]:
            raise ValueError("historical result outside original inventory")
        by_test = per_decision.setdefault(source["id"], {})
        if result["test_id"] in by_test:
            raise ValueError("duplicate primary result; reruns are not history")
        by_test[result["test_id"]] = result
        available = result["available_at"]
        if available is None:
            unavailable += 1
            continue
        if result["published_at"] is None or available != max(number(result["published_at"]), number(result["ingested_at"])):
            raise ValueError("historical availability lacks publication/ingestion evidence")
        if number(available) > cutoff_at or number(result["recorded_at"]) > cutoff_at:
            unavailable += 1
            continue
        if result["raw_outcome"] not in ("passed", "failed"):
            if result["raw_outcome"] not in ("error", "timed_out", "aborted", "skipped"):
                raise ValueError("unknown historical raw outcome")
            nonbinary += 1
            continue
        number(result["duration_s"])
        released.append(result)
    released.sort(key=lambda r: (r["available_at"], decisions[r["decision_id"]]["decision_at"], r["id"]))
    history, supporting = {}, {}
    for result in released:
        source = decisions[result["decision_id"]]
        name = result["test_id"]
        frozen.admit(history, {"index": source["decision_index"], "available": result["available_at"],
                              "tests": {name: (result["duration_s"], int(result["raw_outcome"] == "failed"))}})
        supporting.setdefault(name, []).append(result["id"])
    x, costs, lastfails, lrts = frozen.features(names, history, decision_index)
    features = [dict(test_id=name,
                     values={k: float(v) if math.isfinite(v) else None for k, v in zip(frozen.FEATURES, row)},
                     history_result_ids=supporting.get(name, []), history_diagnosis_ids=[])
                for name, row in zip(names, x)]
    features_sha256 = digest(features)
    features_frozen_at = time.time()
    feature_seconds, feature_cpu = feature_clock.elapsed()
    valid_costs = bool(frozen.np.isfinite(costs).all() and (costs > 0).all())

    refit_clock = frozen.Clock()
    scheduled_index = decision_index - decision_index % 10
    if scheduled_index == decision_index:
        refit_decision = dict(key=decision_id, index=decision_index, start=cutoff_at)
    else:
        scheduled = next((d for d in decisions.values() if d["decision_index"] == scheduled_index), None)
        if scheduled is None:
            raise ValueError("history missing latest frozen scheduled-refit decision")
        refit_decision = dict(key=scheduled["id"], index=scheduled_index,
                              start=scheduled["history_cutoff_at"])
    fit_cutoff = number(refit_decision["start"])
    past, references = [], {}
    for source in sorted(decisions.values(), key=lambda d: (d["decision_at"], d["id"])):
        closure = closures.get(source["id"])
        observed = per_decision.get(source["id"], {})
        if (source["decision_index"] >= scheduled_index or source["decision_at"] >= fit_cutoff
                or closure is None or closure["status"] != "complete"
                or not closure["normal_ci_full_suite"] or closure["recorded_at"] > fit_cutoff
                or set(observed) != set(source["inventory"])):
            continue
        labels = [observed[n] for n in sorted(source["inventory"])]
        if any(r["raw_outcome"] not in ("passed", "failed") or r["available_at"] is None
               or r["available_at"] > fit_cutoff or r["recorded_at"] > fit_cutoff for r in labels):
            continue
        if set(closure["result_ids"]) != {r["id"] for r in labels}:
            raise ValueError("training closure does not cover original primary results")
        original = {f["test_id"]: f for f in source["features"]}
        if set(original) != set(source["inventory"]) or len(original) != len(source["features"]):
            raise ValueError("training feature inventory differs from original inventory")
        xx = []
        for r in labels:
            values = original[r["test_id"]]["values"]
            if set(values) != set(frozen.FEATURES):
                raise ValueError("original feature schema differs from frozen Article 2 model")
            xx.append([frozen.np.nan if values[k] is None else number(values[k]) for k in frozen.FEATURES])
        past.append(dict(key=source["id"], index=source["decision_index"], start=source["decision_at"],
                         available=max(r["available_at"] for r in labels),
                         tests={r["test_id"]: (r["duration_s"], int(r["raw_outcome"] == "failed")) for r in labels},
                         x_asof=frozen.np.asarray(xx, float),
                         y=frozen.np.asarray([int(r["raw_outcome"] == "failed") for r in labels], int)))
        references[source["id"]] = dict(decision_id=source["id"], features_sha256=source["features_sha256"],
                                        label_ids=[r["id"] for r in labels])
    model, refit_log = frozen.refit(past, refit_decision)
    training_inputs = [references[k] for k in refit_log["window_builds"]]
    model_state = None
    if model is not None:
        model_state = dict(refit_decision=refit_decision, training_inputs=training_inputs,
                           C=model["C"], prep=[a.tolist() for a in model["prep"]],
                           base_coefficients=model["base"].coef_.tolist(),
                           base_intercept=model["base"].intercept_.tolist(),
                           calibration_coefficients=model["calib"].coef_.tolist(),
                           calibration_intercept=model["calib"].intercept_.tolist())
    model_sha256 = digest(model_state) if model_state else None
    refit_seconds, refit_cpu = refit_clock.elapsed()
    clock = frozen.Clock()
    code_sha256 = hashlib.sha256(Path(__file__).read_bytes() + b"\0" + (ROOT / "frozen_model.py").read_bytes()).hexdigest()
    policies = []
    # Undefined costs are sorted last without replacing the frozen feature values.
    ordering_costs = frozen.np.where(frozen.np.isfinite(costs), costs, frozen.np.inf)
    ordering_lrts = [v if math.isfinite(v) else math.inf for v in lrts]
    common_finalization_seconds = clock.elapsed()[0]
    for method in frozen.METHODS:
        for seed in (range(30) if method == "B2" else [0]):
            effective = "B4" if method == "LOGIT" and (model is None or not valid_costs) else method
            inference_seconds = 0.
            probabilities = None
            if effective == "LOGIT":
                clock = frozen.Clock()
                raw, calibrated = frozen.probabilities(model, x)
                numeric_scores = calibrated / costs
                probabilities = dict(raw=raw.tolist(), calibrated=calibrated.tolist())
                inference_seconds = clock.elapsed()[0]
            clock = frozen.Clock()
            if effective == "LOGIT":
                order = sorted(range(len(names)), key=lambda i: (-numeric_scores[i], names[i]))
            else:
                order = frozen.rule_order(names, ordering_costs, lastfails, ordering_lrts,
                                           effective, seed, decision_id)
                ranks = {index: rank for rank, index in enumerate(order)}
                numeric_scores = [float(ranks[i]) for i in range(len(names))]
            ordering_seconds = clock.elapsed()[0]
            clock = frozen.Clock()
            if effective == "LOGIT":
                contributions, residual = frozen.explain_adjacent(model, x, costs, order)
                explanation = dict(adjacent_contributions=contributions.tolist() if len(contributions) else [],
                                   components=["history", "cost", "missingness"],
                                   maximum_completeness_residual=residual)
            else:
                reasons = frozen.rule_reasons(effective, names, ordering_costs, lastfails,
                                               ordering_lrts, order, seed)
                explanation = dict(ordering_keys=[[v if not isinstance(v, float) or math.isfinite(v) else None
                                                  for v in row] for row in reasons])
            explanation_seconds = clock.elapsed()[0]
            clock = frozen.Clock()
            config = dict(method=method, effective_method=effective, seed=seed,
                          frozen_model_sha256=FROZEN_SHA256, random_key=decision_id if method == "B2" else None,
                          score_definition="calibrated_probability_over_positive_historical_mean_duration"
                          if effective == "LOGIT" else "ordinal_display_rank_zero_first",
                          direction="descending" if effective == "LOGIT" else "ascending",
                          ties="canonical_test_id", unknown_cost_order="unknown_last_then_canonical_test_id",
                          refit_every_decisions=10, refit_window_builds=200,
                          minimum_prior_builds=50, minimum_positive_rows=20,
                          raw_target="raw_failed", historical_outcomes=["passed", "failed"])
            fallback = (refit_log["reason"] if model is None else "undefined_positive_cost") if method == "LOGIT" and effective != "LOGIT" else None
            policy = dict(policy_id=("B2-seed-%02d" % seed if method == "B2" else method),
                          method=method, seed=seed, effective_method=effective,
                          order=[names[i] for i in order],
                          scores=[dict(test_id=n, score=float(s)) for n, s in zip(names, numeric_scores)],
                          code_sha256=code_sha256, config_sha256=digest(config), configuration=config,
                          model_sha256=model_sha256 if effective == "LOGIT" else None,
                          model_fitted=effective == "LOGIT", fallback_reason=fallback,
                          scope="defined" if valid_costs else "bootstrap_order_logging_only_undefined_cost",
                          training_cutoff_at=fit_cutoff if method == "LOGIT" else cutoff_at,
                          training_inputs=training_inputs if method == "LOGIT" else [],
                          probabilities=probabilities, explanation=explanation)
            finalization_seconds = clock.elapsed()[0]
            components = dict(acquisition=acquisition_s, features=feature_seconds,
                              refit_selection_calibration=refit_seconds if method == "LOGIT" else 0.,
                              inference=inference_seconds, ordering=ordering_seconds,
                              explanations=explanation_seconds,
                              finalization=common_finalization_seconds + finalization_seconds)
            policy["costs_s"] = dict(components, total=sum(components.values()))
            policy["scored_at"] = time.time()
            policies.append(policy)
    return dict(features=features, features_sha256=features_sha256,
                features_frozen_at=features_frozen_at, policies=policies,
                model_state=model_state,
                state=dict(collector_stage="history_costs_defined" if valid_costs else "bootstrap_unknown_cost",
                           estimated_suite_seconds=float(costs.sum()) if valid_costs else None,
                           budget_prefix_defined=valid_costs, budget_recall=None,
                           history_cutoff_at=cutoff_at, raw_feature_target="raw_failed",
                           prior_binary_results=len(released), nonbinary_results_excluded=nonbinary,
                           unavailable_results_excluded=unavailable,
                           complete_binary_builds_at_refit=len(past), scheduled_refit_index=scheduled_index,
                           reconstructed_refit=scheduled_index != decision_index, refit_log=refit_log,
                           feature_wall_seconds=feature_seconds, feature_cpu_seconds=feature_cpu,
                           refit_wall_seconds=refit_seconds, refit_cpu_seconds=refit_cpu))


def self_check():
    """Synthetic cold-start and availability checks; never study observations."""
    context = dict(decision_id="synthetic-current", cutoff_at=time.time(),
                   project="synthetic", environment="synthetic")
    cold = score(["test.B", "test.A"], [], 0, **context)
    assert cold.get("state", {}).get("collector_stage") == "bootstrap_unknown_cost"
    assert all(f["values"]["c_est"] is None for f in cold["features"])
    assert len(cold["policies"]) == 35
    assert {p["seed"] for p in cold["policies"] if p["method"] == "B2"} == set(range(30))
    assert next(p for p in cold["policies"] if p["method"] == "LOGIT")["model_sha256"] is None
    prior = dict(id="synthetic-prior", type="decision", decision_index=0,
                 history_cutoff_at=9., decision_at=10., recorded_at=10.,
                 features_frozen_at=9., inventory=["test.A", "test.B"],
                 features=cold["features"], features_sha256=cold["features_sha256"],
                 feature_target="raw_failed", project="synthetic", environment="synthetic")
    events = [prior]
    for test, outcome, duration in (("test.A", "failed", 2.), ("test.B", "error", 3.)):
        events.append(dict(id="synthetic-result-" + test, type="result", decision_id=prior["id"],
                           test_id=test, duration_s=duration, raw_outcome=outcome,
                           published_at=14., ingested_at=20., available_at=20., recorded_at=20.))
    events.append(dict(id="synthetic-end", type="suite_end", decision_id=prior["id"],
                       status="complete", normal_ci_full_suite=True, recorded_at=21.,
                       result_ids=[e["id"] for e in events if e["type"] == "result"]))
    context["cutoff_at"] = 19.
    early = score(prior["inventory"], events, 1, **context)
    assert all(f["values"]["n"] == 0 for f in early["features"])
    context["cutoff_at"] = 22.
    released = score(prior["inventory"], events, 1, **context)
    a, b = released["features"]
    assert a["values"]["last1"] == 1 and a["values"]["c_est"] == 2
    assert b["values"]["n"] == 0 and b["values"]["duration_missing"] == 1
    assert b["values"]["c_est"] == 2  # Frozen median-of-positive-means fallback.
    assert a["history_result_ids"] == ["synthetic-result-test.A"]
    assert released["state"]["nonbinary_results_excluded"] == 1
    assert next(p for p in released["policies"] if p["method"] == "B4")["order"] == prior["inventory"]
    import copy
    skipped = copy.deepcopy(events)
    skipped[2].update(raw_outcome="skipped", started_at=None, duration_s=None)
    skipped_history = score(prior["inventory"], skipped, 1, **context)
    assert skipped_history["state"]["nonbinary_results_excluded"] == 1
    assert skipped_history["features"][1]["values"]["n"] == 0
    assert skipped_history["features"][1]["values"]["duration_missing"] == 1
    aborted = copy.deepcopy(events)
    aborted[2].update(raw_outcome="aborted", started_at=11., duration_s=3.)
    aborted_history = score(prior["inventory"], aborted, 1, **context)
    assert aborted_history["state"]["nonbinary_results_excluded"] == 1
    assert aborted_history["features"][1]["values"]["n"] == 0
    altered = copy.deepcopy(events)
    altered[0]["features"][0]["values"]["c_est"] = 999.
    try:
        score(prior["inventory"], altered, 1, **context)
    except ValueError:
        pass
    else:
        raise AssertionError("rewritten original feature snapshot accepted")
    training = []
    for i in range(50):
        values = {k: 0. for k in frozen.FEATURES}
        values.update(n=float(i), rate=.5, c_est=2., last1=float(i % 2),
                      last5=2., last20=10., decisionage=1.)
        snapshot = [dict(test_id=n, values=dict(values), history_result_ids=[],
                         history_diagnosis_ids=[]) for n in prior["inventory"]]
        at = 100. + 10 * i
        d = dict(prior, id="synthetic-fit-%d" % i, decision_index=i,
                 history_cutoff_at=at, decision_at=at + 1, recorded_at=at + 1,
                 features_frozen_at=at, features=snapshot, features_sha256=digest(snapshot))
        training.append(d)
        labels = []
        for j, name in enumerate(prior["inventory"]):
            rid = d["id"] + "-" + name
            labels.append(rid)
            training.append(dict(id=rid, type="result", decision_id=d["id"], test_id=name,
                                 raw_outcome="failed" if (i + j) % 2 else "passed", duration_s=2.,
                                 published_at=at + 4, ingested_at=at + 5,
                                 available_at=at + 5, recorded_at=at + 5))
        training.append(dict(id=d["id"] + "-end", type="suite_end", decision_id=d["id"],
                             status="complete", normal_ci_full_suite=True, result_ids=labels,
                             recorded_at=at + 6))
    context["cutoff_at"] = 610.
    fitted = score(prior["inventory"], training, 50, **context)
    logit = next(p for p in fitted["policies"] if p["method"] == "LOGIT")
    assert logit["model_fitted"] and logit["model_sha256"]
    assert len(logit["training_inputs"]) == 50
    assert logit["training_inputs"][0]["features_sha256"] == training[0]["features_sha256"]
    assert fitted["state"]["refit_log"]["positive_rows"] == 50
    assert all(math.isfinite(s["score"]) for p in fitted["policies"] for s in p["scores"])
    print("policy self-check passed (synthetic fixtures only)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-check", action="store_true", required=True)
    parser.parse_args()
    self_check()
