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

import static org.junit.platform.engine.discovery.DiscoverySelectors.selectClasspathRoots;
import static org.junit.platform.engine.discovery.ClassNameFilter.excludeClassNamePatterns;
import static org.junit.platform.engine.discovery.ClassNameFilter.includeClassNamePatterns;

import java.io.PrintWriter;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.Collections;

import org.junit.platform.launcher.Launcher;
import org.junit.platform.launcher.TestPlan;
import org.junit.platform.launcher.core.LauncherDiscoveryRequestBuilder;
import org.junit.platform.launcher.core.LauncherFactory;
import org.junit.platform.launcher.listeners.SummaryGeneratingListener;
import org.junit.platform.launcher.listeners.TestExecutionSummary;

/** Executes one complete discovered plan; the service-loaded listener observes it once. */
public final class PilotLauncher {
    public static void main(final String[] args) throws Exception {
        if (args.length > 1) {
            throw new IllegalArgumentException("Usage: PilotLauncher [target/test-classes]");
        }
        final Path root = Paths.get(args.length == 0 ? "target/test-classes" : args[0]).toRealPath();
        if (!Files.isDirectory(root)) {
            throw new IllegalArgumentException("Test classpath root must be a directory: " + root);
        }
        final Launcher launcher = LauncherFactory.create();
        final TestPlan plan = launcher.discover(LauncherDiscoveryRequestBuilder.request()
                .selectors(selectClasspathRoots(Collections.singleton(root)))
                .filters(includeClassNamePatterns(".*Test"), excludeClassNamePatterns("org\\.researchpilot\\..*"))
                .configurationParameter("junit.jupiter.execution.parallel.enabled", "false").build());
        if (!plan.containsTests()) {
            throw new IllegalStateException("No tests discovered under " + root);
        }
        final SummaryGeneratingListener summaryListener = new SummaryGeneratingListener();
        launcher.execute(plan, summaryListener);
        final TestExecutionSummary summary = summaryListener.getSummary();
        final PrintWriter writer = new PrintWriter(System.out, true);
        summary.printTo(writer);
        summary.printFailuresTo(writer);
        writer.printf("PILOT_SUITE found=%d started=%d succeeded=%d failed=%d aborted=%d skipped=%d container_failures=%d%n",
                summary.getTestsFoundCount(), summary.getTestsStartedCount(), summary.getTestsSucceededCount(),
                summary.getTestsFailedCount(), summary.getTestsAbortedCount(), summary.getTestsSkippedCount(),
                summary.getContainersFailedCount());
        writer.flush();
        // All callbacks are complete; upstream tests can leave non-daemon executor threads alive.
        System.exit(summary.getTotalFailureCount() > 0 ? 1 : 0);
    }
}
