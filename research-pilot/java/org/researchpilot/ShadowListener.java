package org.researchpilot;

import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.channels.FileChannel;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.nio.file.StandardOpenOption;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
import java.util.concurrent.TimeUnit;

import org.junit.platform.engine.TestExecutionResult;
import org.junit.platform.engine.support.descriptor.ClassSource;
import org.junit.platform.launcher.TestExecutionListener;
import org.junit.platform.launcher.TestIdentifier;
import org.junit.platform.launcher.TestPlan;

/** Observes the unchanged JUnit plan; a collection error invalidates data, never cancels tests. */
public final class ShadowListener implements TestExecutionListener {
    private final Path output;
    private final List<String> bridge;
    private final Map<String, Unit> classes = new TreeMap<>();
    private final Map<String, Unit> owners = new HashMap<>();
    private final Set<String> terminalLeaves = new HashSet<>();
    private final long anchorMonotonic = System.nanoTime();
    private final long anchorEpoch = System.currentTimeMillis() * 1_000_000L;
    private final long anchorSampleAfter = System.nanoTime();
    private FileChannel raw;
    private TestPlan plan;
    private long sequence;
    private long startIndex;

    public ShadowListener() {
        this(System.getenv("PILOT_OUTPUT_DIR") == null ? null : Paths.get(System.getenv("PILOT_OUTPUT_DIR")),
                Arrays.asList(System.getenv().getOrDefault("PILOT_PYTHON", "python3"),
                        System.getenv().getOrDefault("PILOT_BRIDGE_SCRIPT", "research-pilot/pilot.py")));
    }

    ShadowListener(final Path output, final List<String> bridge) {
        this.output = output;
        this.bridge = bridge;
    }

    @Override
    public synchronized void testPlanExecutionStarted(final TestPlan testPlan) {
        if (output == null) {
            return;
        }
        try {
            final Path target = Paths.get("target").toAbsolutePath().normalize();
            final Path requested = output.toAbsolutePath().normalize();
            if (requested.equals(target) || !requested.startsWith(target)) {
                throw new IOException("PILOT_OUTPUT_DIR must be a dedicated directory under target");
            }
            Files.createDirectories(target);
            if (!target.toRealPath().equals(target)) {
                throw new IOException("Pilot target directory must not be a symlink");
            }
            Path ancestor = requested;
            while (!Files.exists(ancestor)) {
                ancestor = ancestor.getParent();
            }
            if (!ancestor.toRealPath().startsWith(target)) {
                throw new IOException("Pilot output ancestor symlink escapes target");
            }
            Files.createDirectories(requested);
            if (!requested.toRealPath().startsWith(target.toRealPath())) {
                throw new IOException("Pilot output symlink escapes target");
            }
            raw = FileChannel.open(requested.resolve("live-raw.jsonl"), StandardOpenOption.CREATE_NEW,
                    StandardOpenOption.WRITE);
            plan = testPlan;
            for (final TestIdentifier root : plan.getRoots()) {
                index(root, null);
            }
            final List<Object> inventory = new ArrayList<>();
            for (final Unit unit : classes.values()) {
                inventory.add(fields("class_id", unit.name, "junit_id", unit.id,
                        "tests_discovered", unit.discovered));
            }
            final Map<String, Object> document = fields("schema_version", 1,
                    "classes", inventory, "dynamic_test_leaf_count_known_before_execution", false,
                    "inventory_source", "JUnit TestPlan at testPlanExecutionStarted",
                    "class_unit_mapping", "outermost ClassSource ancestor in actual TestPlan",
                    "planned_execution_order", null,
                    "planned_execution_order_reason", "inventory is lexical; JUnit controls execution order",
                    "clock_anchor_epoch_ns", anchorEpoch, "clock_anchor_monotonic_ns", anchorMonotonic,
                    "clock_anchor_sample_after_ns", anchorSampleAfter,
                    "clock_anchor_uncertainty_ns", anchorSampleAfter - anchorMonotonic + 1_000_000L,
                    "clock_model", "epoch_ns=anchor_epoch_ns+monotonic_ns-anchor_monotonic_ns; millisecond wall anchor",
                    "duration_scope", "top class start-to-finish callbacks including instrumentation overhead");
            document.put("inventory_at_epoch_ns", epoch(System.nanoTime()));
            final Path inventoryPath = requested.resolve("inventory.json");
            try (FileChannel inventoryFile = FileChannel.open(inventoryPath, StandardOpenOption.CREATE_NEW,
                    StandardOpenOption.WRITE)) {
                durableWrite(inventoryFile, json(document) + "\n");
            }
            emit("inventory_published", fields("inventory_path", inventoryPath.toString(),
                    "class_count", classes.size(), "published_at_epoch_ns", epoch(System.nanoTime())));
            final List<String> command = new ArrayList<>(bridge);
            command.addAll(Arrays.asList("freeze", "--inventory", inventoryPath.toString(), "--output", requested.toString()));
            final Process callback = new ProcessBuilder(command).inheritIO().start();
            if (!callback.waitFor(180, TimeUnit.SECONDS)) {
                callback.destroyForcibly();
                throw new IOException("Freeze callback exceeded 180 seconds");
            }
            if (callback.exitValue() != 0) {
                throw new IOException("Freeze callback exited " + callback.exitValue());
            }
            emit("freeze_completed", fields("exit_code", 0));
        } catch (final Exception error) {
            collectionError("inventory_or_freeze", error);
        }
    }

    private void index(final TestIdentifier id, Unit unit) throws IOException {
        if (unit == null && id.getSource().orElse(null) instanceof ClassSource) {
            final String name = ((ClassSource) id.getSource().get()).getClassName();
            if (classes.containsKey(name)) {
                throw new IOException("Duplicate top-class container " + name);
            }
            unit = new Unit(name, id.getUniqueId());
            classes.put(name, unit);
        }
        if (unit != null) {
            owners.put(id.getUniqueId(), unit);
            if (id.isTest()) {
                unit.discovered++;
            }
        }
        for (final TestIdentifier child : plan.getChildren(id)) {
            index(child, unit);
        }
    }

    @Override
    public synchronized void dynamicTestRegistered(final TestIdentifier id) {
        if (raw == null) {
            return;
        }
        try {
            final Unit unit = id.getParentId().map(owners::get).orElse(null);
            if (unit == null) {
                throw new IOException("Dynamic identifier lacks an inventoried top-class ancestor: " + id.getUniqueId());
            }
            owners.put(id.getUniqueId(), unit);
            if (id.isTest()) {
                unit.dynamic++;
            }
            emit("junit_registered", identifier(id));
        } catch (final Exception error) {
            collectionError("dynamic_registration", error);
        }
    }

    @Override
    public synchronized void executionStarted(final TestIdentifier id) {
        if (raw == null) {
            return;
        }
        try {
            final long now = System.nanoTime();
            final Unit unit = owner(id);
            emit("junit_started", identifier(id));
            if (unit != null && unit.id.equals(id.getUniqueId())) {
                unit.start = now;
                unit.startIndex = ++startIndex;
                emit("class_started", fields("class_id", unit.name, "junit_id", unit.id,
                        "actual_start_index", unit.startIndex, "started_at_epoch_ns", epoch(now),
                        "started_at_monotonic_ns", now));
            }
        } catch (final Exception error) {
            collectionError("execution_started", error);
        }
    }

    @Override
    public synchronized void executionFinished(final TestIdentifier id, final TestExecutionResult result) {
        if (raw == null) {
            return;
        }
        try {
            final long now = System.nanoTime();
            final Unit unit = owner(id);
            final String outcome = result.getStatus().name();
            if (unit != null) {
                unit.failed |= result.getStatus() == TestExecutionResult.Status.FAILED;
                unit.aborted |= result.getStatus() == TestExecutionResult.Status.ABORTED;
                if (id.isTest() && terminalLeaves.add(id.getUniqueId())) {
                    if (result.getStatus() == TestExecutionResult.Status.SUCCESSFUL) {
                        unit.succeeded++;
                    } else if (result.getStatus() == TestExecutionResult.Status.FAILED) {
                        unit.failedLeaves++;
                        if (result.getThrowable().orElse(null) instanceof AssertionError) {
                            unit.assertionFailures++;
                        } else {
                            unit.errorFailures++;
                        }
                    } else {
                        unit.abortedLeaves++;
                    }
                }
            }
            final Map<String, Object> event = identifier(id);
            event.put("junit_outcome", outcome);
            event.put("finished_at_epoch_ns", epoch(now));
            event.put("throwable", result.getThrowable().map(Throwable::toString).orElse(null));
            event.put("throwable_class", result.getThrowable().map(value -> value.getClass().getName()).orElse(null));
            emit("junit_finished", event);
            if (unit != null && unit.id.equals(id.getUniqueId())) {
                finish(unit, unit.failed ? "FAILED" : unit.aborted ? "ABORTED" : "PASSED", outcome, now);
            }
        } catch (final Exception error) {
            collectionError("execution_finished", error);
        }
    }

    @Override
    public synchronized void executionSkipped(final TestIdentifier id, final String reason) {
        if (raw == null) {
            return;
        }
        try {
            final long now = System.nanoTime();
            final Unit unit = owner(id);
            if (unit != null) {
                final Set<TestIdentifier> skipped = new HashSet<>(plan.getDescendants(id));
                skipped.add(id);
                for (final TestIdentifier leaf : skipped) {
                    if (leaf.isTest() && terminalLeaves.add(leaf.getUniqueId())) {
                        unit.skipped++;
                    }
                }
            }
            final Map<String, Object> event = identifier(id);
            event.put("reason", reason);
            emit("junit_skipped", event);
            if (unit != null && unit.id.equals(id.getUniqueId())) {
                finish(unit, "SKIPPED", "SKIPPED", now);
            }
        } catch (final Exception error) {
            collectionError("execution_skipped", error);
        }
    }

    private Unit owner(final TestIdentifier id) throws IOException {
        final Unit unit = owners.get(id.getUniqueId());
        if (unit == null && id.isTest()) {
            throw new IOException("Executable leaf not mapped to inventoried class: " + id.getUniqueId());
        }
        return unit;
    }

    private Map<String, Object> identifier(final TestIdentifier id) {
        final Unit unit = owners.get(id.getUniqueId());
        return fields("junit_id", id.getUniqueId(), "parent_junit_id", id.getParentId().orElse(null),
                "class_id", unit == null ? null : unit.name, "is_test", id.isTest(), "is_container", id.isContainer());
    }

    private void finish(final Unit unit, final String outcome, final String junitOutcome, final long now) throws IOException {
        final long dataSequence = emit("class_finished", fields("class_id", unit.name, "junit_id", unit.id,
                "actual_start_index", unit.start == null ? null : unit.startIndex,
                "started_at_epoch_ns", unit.start == null ? null : epoch(unit.start),
                "started_at_monotonic_ns", unit.start, "finished_at_epoch_ns", epoch(now),
                "finished_at_monotonic_ns", now, "duration_ns", unit.start == null ? null : now - unit.start,
                "outcome", outcome, "junit_outcome", junitOutcome, "tests_discovered", unit.discovered,
                "tests_dynamic_registered", unit.dynamic, "tests_succeeded", unit.succeeded,
                "tests_failed", unit.failedLeaves, "tests_aborted", unit.abortedLeaves, "tests_skipped", unit.skipped,
                "tests_failure_assertions", unit.assertionFailures, "tests_failure_errors", unit.errorFailures,
                "tests_not_executed", unit.discovered + unit.dynamic - unit.succeeded - unit.failedLeaves
                        - unit.abortedLeaves - unit.skipped));
        // Publication follows the durable result, independently of callback observation time.
        emit("class_published", fields("class_id", unit.name, "data_sequence", dataSequence,
                "published_at_epoch_ns", epoch(System.nanoTime())));
    }

    @Override
    public synchronized void testPlanExecutionFinished(final TestPlan testPlan) {
        if (raw == null) {
            return;
        }
        try {
            emit("plan_finished", fields("class_count", classes.size()));
            raw.close();
        } catch (final Exception error) {
            collectionError("plan_finished", error);
        }
    }

    private long emit(final String event, final Map<String, Object> body) throws IOException {
        final long now = System.nanoTime();
        final Map<String, Object> line = fields("schema_version", 1, "event", event, "sequence", ++sequence,
                "observed_at_epoch_ns", epoch(now), "observed_at_monotonic_ns", now);
        line.putAll(body);
        durableWrite(raw, json(line) + "\n");
        return sequence;
    }

    private void collectionError(final String stage, final Exception error) {
        System.err.println("Research pilot collection error (suite continues): " + stage + ": " + error);
        if (error instanceof InterruptedException) {
            Thread.currentThread().interrupt();
        }
        if (raw != null) {
            try {
                emit("collection_error", fields("stage", stage, "message", error.toString()));
            } catch (final IOException loggingError) {
                System.err.println("Research pilot cannot persist collection error: " + loggingError);
                try {
                    raw.close();
                } catch (final IOException ignored) {
                    // Full suite still runs; missing evidence invalidates the dataset.
                }
                raw = null;
            }
        }
    }

    private long epoch(final long monotonic) {
        return anchorEpoch + monotonic - anchorMonotonic;
    }

    private static void durableWrite(final FileChannel channel, final String text) throws IOException {
        final ByteBuffer bytes = StandardCharsets.UTF_8.encode(text);
        while (bytes.hasRemaining()) {
            channel.write(bytes);
        }
        channel.force(true);
    }

    private static Map<String, Object> fields(final Object... pairs) {
        final Map<String, Object> map = new LinkedHashMap<>();
        for (int i = 0; i < pairs.length; i += 2) {
            map.put((String) pairs[i], pairs[i + 1]);
        }
        return map;
    }

    private static String json(final Object value) {
        if (value == null || value instanceof Number || value instanceof Boolean) {
            return String.valueOf(value);
        }
        if (value instanceof Map) {
            final List<String> values = new ArrayList<>();
            for (final Map.Entry<?, ?> entry : ((Map<?, ?>) value).entrySet()) {
                values.add(json(entry.getKey()) + ":" + json(entry.getValue()));
            }
            return "{" + String.join(",", values) + "}";
        }
        if (value instanceof List) {
            final List<String> values = new ArrayList<>();
            for (final Object entry : (List<?>) value) {
                values.add(json(entry));
            }
            return "[" + String.join(",", values) + "]";
        }
        final StringBuilder quoted = new StringBuilder("\"");
        for (final char character : value.toString().toCharArray()) {
            if (character == '"' || character == '\\') {
                quoted.append('\\').append(character);
            } else if (character < 0x20) {
                quoted.append(String.format("\\u%04x", (int) character));
            } else {
                quoted.append(character);
            }
        }
        return quoted.append('"').toString();
    }

    private static final class Unit {
        private final String name;
        private final String id;
        private Long start;
        private long startIndex;
        private long discovered;
        private long dynamic;
        private long succeeded;
        private long failedLeaves;
        private long assertionFailures;
        private long errorFailures;
        private long abortedLeaves;
        private long skipped;
        private boolean failed;
        private boolean aborted;

        Unit(final String name, final String id) {
            this.name = name;
            this.id = id;
        }
    }
}
