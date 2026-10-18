/*
 * Copyright 2026 Anas Chari
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     https://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */
package org.researchpilot;

import static org.junit.platform.engine.discovery.DiscoverySelectors.selectClass;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.Arrays;
import java.util.List;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.stream.Stream;

import javax.tools.JavaCompiler;
import javax.tools.ToolProvider;

import org.junit.jupiter.api.Assertions;
import org.junit.jupiter.api.Assumptions;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Disabled;
import org.junit.jupiter.api.DynamicTest;
import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestFactory;
import org.junit.platform.engine.TestExecutionResult;
import org.junit.platform.launcher.Launcher;
import org.junit.platform.launcher.TestExecutionListener;
import org.junit.platform.launcher.TestIdentifier;
import org.junit.platform.launcher.core.LauncherConfig;
import org.junit.platform.launcher.core.LauncherDiscoveryRequestBuilder;
import org.junit.platform.launcher.core.LauncherFactory;

/** Executable synthetic probe; fixture failures are deliberate, never pilot observations. */
public final class ListenerSelfCheck {
    private static Path marker;

    public static void main(final String[] args) throws Exception {
        final Path output = Paths.get(args[0]).toAbsolutePath();
        marker = output.resolve("freeze.marker");
        final String callback = "import json,pathlib,sys; p=pathlib.Path(sys.argv[-1]); "
                + "j=json.loads((p/'inventory.json').read_text()); assert len(j['classes'])==3; "
                + "assert 'class_started' not in (p/'live-raw.jsonl').read_text(); "
                + "(p/'freeze.marker').write_text('frozen before execution')";
        final ShadowListener listener = new ShadowListener(output, Arrays.asList("python3", "-c", callback));
        final AtomicBoolean interruptedCallbackPreserved = new AtomicBoolean();
        final Launcher launcher = LauncherFactory.create(LauncherConfig.builder()
                .enableTestExecutionListenerAutoRegistration(false).build());
        // Finish callbacks run in reverse registration order: observe after the durable listener write.
        launcher.registerTestExecutionListeners(new TestExecutionListener() {
            @Override
            public void executionFinished(final TestIdentifier id, final TestExecutionResult result) {
                if (id.getDisplayName().equals("leavesInterruptSet()")) {
                    interruptedCallbackPreserved.set(Thread.currentThread().isInterrupted());
                }
            }
        }, listener);
        launcher.execute(LauncherDiscoveryRequestBuilder.request()
                .selectors(selectClass(MixedFixture.class), selectClass(DisabledFixture.class),
                        selectClass(FailedSetupFixture.class))
                .configurationParameter("junit.jupiter.execution.parallel.enabled", "false").build());
        require(interruptedCallbackPreserved.get(), "Durable callback write must preserve the test thread interrupt flag");
        final List<String> lines = Files.readAllLines(output.resolve("live-raw.jsonl"), StandardCharsets.UTF_8);
        final String mixed = result(lines, MixedFixture.class);
        require(mixed.contains("\"outcome\":\"FAILED\"") && mixed.contains("\"tests_succeeded\":4")
                && mixed.contains("\"tests_failed\":2") && mixed.contains("\"tests_aborted\":1")
                && mixed.contains("\"tests_skipped\":2") && mixed.contains("\"tests_dynamic_registered\":2"), mixed);
        require(result(lines, DisabledFixture.class).contains("\"outcome\":\"SKIPPED\""), "disabled class");
        require(result(lines, DisabledFixture.class).contains("\"duration_ns\":null"), "skipped has no execution time");
        require(result(lines, FailedSetupFixture.class).contains("\"outcome\":\"FAILED\""), "setup failure");
        require(result(lines, FailedSetupFixture.class).contains("\"tests_not_executed\":1"), "unstarted leaf");
        long finished = 0;
        long published = 0;
        for (final String line : lines) {
            require(!line.contains("\"event\":\"collection_error\""), line);
            if (line.contains("\"event\":\"class_finished\"")) {
                finished++;
                require(line.contains("\"finished_at_epoch_ns\":") && line.contains("\"duration_ns\":"), line);
            }
            if (line.contains("\"event\":\"class_published\"")) {
                published++;
                require(finished >= published && line.contains("\"data_sequence\":"), line);
            }
        }
        require(finished == 3 && published == 3, "three top class results, each published after durable data");
        require(lines.get(lines.size() - 1).contains("\"event\":\"plan_finished\""),
                "Writes after an interrupted callback must survive through complete plan publication");
        final String timingCheck = "import json,pathlib,sys\n"
                + "p=pathlib.Path(sys.argv[1]); rows=[json.loads(s) for s in (p/'live-raw.jsonl').read_text().splitlines()]\n"
                + "assert [r['sequence'] for r in rows]==list(range(1,len(rows)+1))\n"
                + "data={r['sequence']:r for r in rows if r['event']=='class_finished'}\n"
                + "for r in rows:\n"
                + " if r['event']=='class_finished' and r['started_at_monotonic_ns'] is not None:\n"
                + "  assert r['duration_ns']==r['finished_at_monotonic_ns']-r['started_at_monotonic_ns']>=0\n"
                + "  assert r['finished_at_epoch_ns']-r['started_at_epoch_ns']==r['duration_ns']\n"
                + " if r['event']=='class_published':\n"
                + "  d=data[r['data_sequence']]; assert d['class_id']==r['class_id']\n"
                + "  assert d['sequence']<r['sequence'] and d['finished_at_epoch_ns']<=r['published_at_epoch_ns']\n"
                + "  assert d['observed_at_monotonic_ns']<=r['observed_at_monotonic_ns']\n"
                + "freeze=next(r for r in rows if r['event']=='freeze_completed')\n"
                + "assert all(r['sequence']>freeze['sequence'] for r in rows if r['event']=='class_started')\n";
        require(new ProcessBuilder("python3", "-c", timingCheck, output.toString()).inheritIO().start().waitFor() == 0,
                "durable publication references and monotonic/epoch duration agreement");
        checkSuccessfulLauncherExit(output);
        System.out.println("Listener self-check passed: mapping, outcomes, freeze, durable publication, and leaked-thread process exit.");
    }

    private static void checkSuccessfulLauncherExit(final Path output) throws Exception {
        final Path classes = Files.createDirectories(output.resolve("exit-probe-classes"));
        final Path evidence = output.resolve("exit-probe-evidence");
        final Path source = output.resolve("ExitTest.java");
        final String fixture = "package org.pilotfixture; public class ExitTest {"
                + " @org.junit.jupiter.api.Test void passesWithLeakedThread() throws Exception {"
                + " org.junit.jupiter.api.Assertions.assertTrue(java.nio.file.Files.exists("
                + "java.nio.file.Paths.get(System.getenv(\"PILOT_OUTPUT_DIR\"),\"frozen\")));"
                + " Thread worker=new Thread(() -> { try { new java.util.concurrent.CountDownLatch(1).await(); }"
                + " catch(InterruptedException e) { Thread.currentThread().interrupt(); } },\"synthetic-leaked-worker\");"
                + " worker.setDaemon(false); worker.start();"
                + " org.junit.jupiter.api.Assertions.assertTrue(worker.isAlive()); }"
                + " @org.junit.jupiter.api.AfterAll static void completed() throws Exception {"
                + " java.nio.file.Files.write(java.nio.file.Paths.get(System.getenv(\"PILOT_OUTPUT_DIR\"),"
                + "\"after-all-completed\"),new byte[]{1}); } }";
        Files.write(source, fixture.getBytes(StandardCharsets.UTF_8));
        final JavaCompiler compiler = ToolProvider.getSystemJavaCompiler();
        require(compiler != null, "Exit regression probe requires the JDK compiler");
        require(compiler.run(null, null, null, "-classpath", System.getProperty("java.class.path"),
                "-d", classes.toString(), source.toString()) == 0, "Compile isolated passing exit fixture");
        final Path bridgeScript = output.resolve("exit-probe-bridge.py");
        Files.write(bridgeScript, Arrays.asList("import json,pathlib,sys",
                "p=pathlib.Path(sys.argv[-1])",
                "assert len(json.loads((p/'inventory.json').read_text())['classes'])==1",
                "(p/'frozen').write_text('frozen before execution')"), StandardCharsets.UTF_8);
        final Path log = output.resolve("exit-probe.log");
        final ProcessBuilder command = new ProcessBuilder(Paths.get(System.getProperty("java.home"), "bin", "java").toString(),
                "-cp", classes + System.getProperty("path.separator") + System.getProperty("java.class.path"),
                "org.researchpilot.PilotLauncher", classes.toString()).redirectErrorStream(true).redirectOutput(log.toFile());
        command.environment().put("PILOT_OUTPUT_DIR", evidence.toString());
        command.environment().put("PILOT_BRIDGE_SCRIPT", bridgeScript.toString());
        command.environment().put("PILOT_PYTHON", System.getenv().getOrDefault("PILOT_PYTHON", "python3"));
        final Process process = command.start();
        if (!process.waitFor(30, TimeUnit.SECONDS)) {
            process.destroyForcibly();
            process.waitFor(5, TimeUnit.SECONDS);
            throw new AssertionError("Successful launcher did not exit with a live non-daemon test thread; see " + log);
        }
        require(process.exitValue() == 0, "Passing launcher exit code; see " + log);
        require(Files.exists(evidence.resolve("after-all-completed")), "Exit must follow full test lifecycle");
        final String events = new String(Files.readAllBytes(evidence.resolve("live-raw.jsonl")), StandardCharsets.UTF_8);
        require(events.contains("\"event\":\"class_published\"") && events.contains("\"event\":\"plan_finished\"")
                && !events.contains("\"event\":\"collection_error\""), "Exit must follow durable result and plan completion");
        final String summary = new String(Files.readAllBytes(log), StandardCharsets.UTF_8);
        require(summary.contains("PILOT_SUITE found=1 started=1 succeeded=1 failed=0 aborted=0 skipped=0 container_failures=0"),
                "Exit must follow complete, flushed suite summary; see " + log);
    }

    private static String result(final List<String> lines, final Class<?> fixture) {
        return lines.stream().filter(line -> line.contains("\"event\":\"class_finished\"")
                && line.contains("\"class_id\":\"" + fixture.getName() + "\""))
                .findFirst().orElseThrow(() -> new AssertionError("Missing class result: " + fixture.getName()));
    }

    private static void require(final boolean value, final String message) {
        if (!value) {
            throw new AssertionError(message);
        }
    }

    static class MixedFixture {
        @BeforeAll static void gate() { Assertions.assertTrue(Files.exists(marker)); }
        @Test void pass() { }
        @Test void leavesInterruptSet() { Thread.currentThread().interrupt(); }
        @Test void fail() { Assertions.fail("deliberate synthetic failure"); }
        @Test void abort() { Assumptions.assumeTrue(false, "deliberate synthetic abort"); }
        @Disabled @Test void skipped() { Assertions.fail("must not execute"); }
        @TestFactory Stream<DynamicTest> dynamic() {
            return Stream.of(DynamicTest.dynamicTest("dynamic pass", () -> { }),
                    DynamicTest.dynamicTest("dynamic failure", () -> Assertions.fail("synthetic")));
        }
        @Nested class Inner {
            @Test void pass() { }
        }
        @Disabled @Nested class SkippedInner {
            @Test void skipped() { Assertions.fail("must not execute"); }
        }
    }

    @Disabled static class DisabledFixture {
        @Test void skipped() { Assertions.fail("must not execute"); }
    }

    static class FailedSetupFixture {
        @BeforeAll static void failSetup() { Assertions.fail("deliberate synthetic setup failure"); }
        @Test void unstarted() { Assertions.fail("must not execute"); }
    }
}
