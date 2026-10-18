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
import java.util.stream.Stream;

import org.junit.jupiter.api.Assertions;
import org.junit.jupiter.api.Assumptions;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Disabled;
import org.junit.jupiter.api.DynamicTest;
import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestFactory;
import org.junit.platform.launcher.Launcher;
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
        final Launcher launcher = LauncherFactory.create(LauncherConfig.builder()
                .enableTestExecutionListenerAutoRegistration(false).build());
        launcher.registerTestExecutionListeners(listener);
        launcher.execute(LauncherDiscoveryRequestBuilder.request()
                .selectors(selectClass(MixedFixture.class), selectClass(DisabledFixture.class),
                        selectClass(FailedSetupFixture.class))
                .configurationParameter("junit.jupiter.execution.parallel.enabled", "false").build());
        final List<String> lines = Files.readAllLines(output.resolve("live-raw.jsonl"), StandardCharsets.UTF_8);
        final String mixed = result(lines, MixedFixture.class);
        require(mixed.contains("\"outcome\":\"FAILED\"") && mixed.contains("\"tests_succeeded\":3")
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
        System.out.println("Listener self-check passed: nested/dynamic mapping, failures, aborts, skips, freeze, publication.");
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
