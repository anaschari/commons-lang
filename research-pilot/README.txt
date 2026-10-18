Commons Lang prospective shadow pilot

This fork runs an instrumented full JUnit class suite on GitHub Actions. It does
not modify Apache upstream. The first launches are technical calibration and are
excluded from efficacy claims. Workflow success means collection validity, not
that every test passed. Actual test failures remain data and their process exit
status is retained. Synthetic probe failures are kept in a separate artifact.

Run the Prospective shadow pilot workflow on research/shadow-pilot. No recurring
schedule or deployment is installed. The upstream source base is recorded in
pilot.py; every run also records the actual fork commit and artifact provenance.
Original project sources and pom.xml remain unchanged. A temporary .pilot-pom.xml
adds JUnit Platform launcher to test dependencies, compiles the original test suite,
and the pilot listener is compiled separately into target/test-classes.

PilotLauncher discovers one JUnit TestPlan for all .*Test classes, excludes the
probe namespace, and runs that entire plan with Jupiter's own order. Java21 needs
no project-specific JDK15 class exclusion. This harness uses the project's Java9
module opens, Java21 agent flag and 1GB heap. It is an experimental runner
configuration; its timings are not untouched upstream Maven CI timings.

The entity is the outermost ClassSource container. Nested and dynamic descendants
roll into that class. The class inventory is frozen in the TestPlan callback before
any test runs. Dynamic method counts are not falsely claimed known in advance.
Inventory lexical order is not the engine's planned order; chosen_ci_order stays
null with a reason, and actual class starts are recorded separately.

The live listener fsyncs a class payload then emits a publication marker. Python
consumes that marker while the suite runs and records actual ingestion. The Java
clock is a millisecond wall anchor plus monotonic deltas, with uncertainty in the
inventory. Timings include observer callback/fsync overhead. Collection errors
invalidate evidence and the workflow, but do not cancel the underlying suite.

PASSED/FAILED class observations become raw passed/failed. ABORTED and SKIPPED
remain separate nonbinary outcomes. An entirely skipped class has unknown start
and runtime, never an invented zero. Leaf assertion/error/skip/abort/unstarted
counts are retained. Class-level complete means each inventoried class has an
observed binary result. full_leaf_coverage is a separate stricter field; disabled
methods are never silently claimed executed. Missing, skipped or aborted primary
classes keep suite status incomplete and prevent complete-build training.

35 policy snapshots cover B1, 30 B2 seeds, B3, B4, B5 and LOGIT. Raw failure target,
features, fitting and ordering reuse frozen_model.py byte-for-byte. The first run
has no past positive costs: cost features and estimated budget are null, LOGIT uses
B4 fallback, and no budget recall is reported. Future fitting needs the unchanged
50-build/20-positive and chronological-block criteria. Later refit state is
reconstructed from original snapshots at its original scheduled cutoff; the
extra reconstruction work is measured. No retrospective dataset seeds the pilot.

The v2 contract derives from the earlier kit, adding an unknown engine planned
order and honest skipped/aborted class timing. Run local checks with Python3:
  python research-pilot/check_collector.py
  python research-pilot/validate.py --self-check
  python research-pilot/policies.py --self-check
The scoring check needs requirements.txt. The JVM probe runs inside Actions.

History comes only from a successful collection workflow in this repository and
branch, an ancestor commit, an archive matching GitHub's SHA256 digest, and an
identical collector/config signature. It rejects synthetic history and validates
all references before scoring. Workflow completion never filters out failed tests.
A collector/config change requires a separately recorded dataset; it must not
silently inherit incompatible observations. Artifacts expire after 30 days: download
and preserve them for research. No publication or failure-diagnosis rule is inferred
from file modification times or build start+duration. No nonflaky diagnosis, model
superiority, production saving or confirmatory validation is established.

Measured policy component totals and the conservative one second callback tail
reserve are separate. preparation_charged_s adds both; actual callback tail and
whole collector preparation are measured after execution starts and reported.
A tail reserve overrun invalidates the technical collection rather than silently
undercharging a future budget. Dependency installation, compilation and the
synthetic probe remain separate from decision preparation.

Licensing: new pilot implementation and synthetic JSONL fixture are distributed
under this repository Apache License2.0. The JSONL fixture is listed as a data
exclusion in the temporary POM because comments would break its event format.
The temporary POM preserves the original Apache license comment. RAT remains
enabled. frozen_model.py retains its original research bytes and recorded hash.
