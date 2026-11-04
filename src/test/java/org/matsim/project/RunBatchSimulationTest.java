package org.matsim.project;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;

import java.io.BufferedReader;
import java.io.ByteArrayOutputStream;
import java.io.PrintStream;
import java.io.StringReader;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.function.ToIntFunction;

import static org.junit.jupiter.api.Assertions.assertArrayEquals;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

class RunBatchSimulationTest {

    @Test
    void executesSequentialJobsAndPreservesNonzeroExitCodes() throws Exception {
        List<String[]> calls = new ArrayList<>();
        Result result = run("""
                {"run_id":"first","args":["--workspace","a path","--run-id","first"]}
                {"run_id":"second","args":["--run-id=second"]}
                """, args -> {
            calls.add(args);
            return calls.size() == 1 ? 17 : 0;
        });

        assertEquals(0, result.exitCode());
        assertEquals("""
                {"run_id":"first","exit_code":17}
                {"run_id":"second","exit_code":0}
                """, result.output());
        assertEquals("", result.diagnostics());
        assertEquals(2, calls.size());
        assertArrayEquals(new String[]{"--workspace", "a path", "--run-id", "first"}, calls.getFirst());
        assertArrayEquals(new String[]{"--run-id=second"}, calls.getLast());
    }

    @Test
    void emptyEofDoesNotExecuteOrEmitAnything() throws Exception {
        Result result = run("", args -> {
            throw new AssertionError("EOF must not execute a job");
        });
        assertEquals(0, result.exitCode());
        assertEquals("", result.output());
        assertEquals("", result.diagnostics());
    }

    @Test
    void acceptsFinalRequestWithoutNewlineAndEscapesIds() throws Exception {
        String id = "train \"one\" \u00e9";
        ObjectMapper mapper = new ObjectMapper();
        String request = mapper.writeValueAsString(
                java.util.Map.of("run_id", id, "args", List.of("--run-id", id)));
        Result result = run(request, args -> 0);
        assertEquals(0, result.exitCode());
        assertEquals(1, result.output().lines().count());
        assertEquals(id, mapper.readTree(result.output()).get("run_id").textValue());
    }

    @Test
    void rejectsMalformedJobsBeforeExecutionAndStopsReading() throws Exception {
        List<String> invalid = List.of(
                "", "{", "null", "[]", "123", "{}",
                "{\"run_id\":null,\"args\":[]}",
                "{\"run_id\":5,\"args\":[]}",
                "{\"run_id\":\" \",\"args\":[]}",
                "{\"run_id\":\"x\"}",
                "{\"run_id\":\"x\",\"args\":null}",
                "{\"run_id\":\"x\",\"args\":\"--run-id=x\"}",
                "{\"run_id\":\"x\",\"args\":[]}",
                "{\"run_id\":\"x\",\"args\":[null]}",
                "{\"run_id\":\"x\",\"args\":[5]}",
                "{\"run_id\":\"x\",\"args\":[\"--workspace\",\"w\"]}",
                "{\"run_id\":\"x\",\"args\":[\"--run-id\"]}",
                "{\"run_id\":\"x\",\"args\":[\"--run-id\",\"other\"]}",
                "{\"run_id\":\"x\",\"args\":[\"--run-id=other\"]}",
                "{\"run_id\":\"x\",\"args\":[\"--run-id=x\",\"--run-id\",\"x\"]}",
                "{\"run_id\":\"x\",\"args\":[\"run-once\",\"--run-id=x\"]}",
                "{\"run_id\":\"x\",\"run_id\":\"x\",\"args\":[\"--run-id=x\"]}",
                "{\"run_id\":\"x\",\"args\":[\"--run-id=x\"]} {}");
        for (String request : invalid) {
            Result result = run(request + "\n{\"run_id\":\"next\",\"args\":[\"--run-id=next\"]}\n",
                    args -> {
                        throw new AssertionError("Invalid request must fail fast: " + request);
                    });
            assertEquals(2, result.exitCode(), request);
            assertEquals(1, result.output().lines().count(), request);
            assertEquals(2, new ObjectMapper().readTree(result.output()).get("exit_code").intValue(), request);
            assertTrue(result.diagnostics().contains("Invalid job at line 1:"), request);
            assertFalse(result.output().contains("next"), request);
        }
    }

    @Test
    void invalidArgsResponseRetainsKnownRunId() throws Exception {
        Result result = run("{\"run_id\":\"x\",\"args\":[]}\n", args -> 0);
        assertEquals("{\"run_id\":\"x\",\"exit_code\":2}\n", result.output());
    }

    private static Result run(String input, ToIntFunction<String[]> executor) throws Exception {
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        ByteArrayOutputStream diagnostics = new ByteArrayOutputStream();
        try (PrintStream protocol = new PrintStream(output, false, StandardCharsets.UTF_8);
             PrintStream errors = new PrintStream(diagnostics, false, StandardCharsets.UTF_8)) {
            int exitCode = RunBatchSimulation.runLoop(new BufferedReader(new StringReader(input)),
                    protocol, errors, executor);
            return new Result(exitCode, output.toString(StandardCharsets.UTF_8),
                    diagnostics.toString(StandardCharsets.UTF_8));
        }
    }

    private record Result(int exitCode, String output, String diagnostics) { }
}
