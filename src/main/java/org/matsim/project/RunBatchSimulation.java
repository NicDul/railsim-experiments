package org.matsim.project;

import com.fasterxml.jackson.core.JsonParser;
import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import picocli.CommandLine;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import java.util.function.ToIntFunction;

/** Runs sequential run-once argument lists using a JSON-lines stdin/stdout protocol. */
public final class RunBatchSimulation {

    private RunBatchSimulation() { }

    public static void main(String[] args) throws IOException {
        PrintStream protocol = System.out;
        // Capture stdout before loading simulator classes or initializing their loggers.
        System.setOut(System.err);
        int exitCode = runLoop(new BufferedReader(new InputStreamReader(System.in, StandardCharsets.UTF_8)),
                protocol, System.err, jobArgs -> new CommandLine(new RunSingleSimulation()).execute(jobArgs));
        if (exitCode != 0) System.exit(exitCode);
    }

    /** Invalid requests receive exit code 2 and terminate the worker; job exit codes do not. */
    static int runLoop(BufferedReader input, PrintStream protocol, PrintStream diagnostics,
                       ToIntFunction<String[]> executor) throws IOException {
        ObjectMapper mapper = new ObjectMapper()
                .enable(DeserializationFeature.FAIL_ON_TRAILING_TOKENS)
                .enable(JsonParser.Feature.STRICT_DUPLICATE_DETECTION);
        String line;
        int lineNumber = 0;
        while ((line = input.readLine()) != null) {
            lineNumber++;
            String runId = null;
            String[] jobArgs;
            try {
                JsonNode request = mapper.readTree(line);
                if (request == null || !request.isObject()) {
                    throw new IllegalArgumentException("request must be a JSON object");
                }
                JsonNode id = request.get("run_id");
                if (id == null || !id.isTextual() || id.textValue().isBlank()) {
                    throw new IllegalArgumentException("run_id must be a nonblank string");
                }
                runId = id.textValue();
                jobArgs = validateArgs(request.get("args"), runId);
            } catch (JsonProcessingException | IllegalArgumentException e) {
                diagnostics.println("Invalid job at line " + lineNumber + ": " + e.getMessage());
                writeResponse(mapper, protocol, runId, 2);
                return 2;
            }
            int exitCode = executor.applyAsInt(jobArgs);
            writeResponse(mapper, protocol, runId, exitCode);
        }
        return 0;
    }

    private static String[] validateArgs(JsonNode args, String runId) {
        if (args == null || !args.isArray() || args.isEmpty()) {
            throw new IllegalArgumentException("args must be a nonempty array of strings");
        }
        String[] result = new String[args.size()];
        for (int i = 0; i < result.length; i++) {
            if (!args.get(i).isTextual()) {
                throw new IllegalArgumentException("args must contain only strings");
            }
            result[i] = args.get(i).textValue();
        }
        if ("run-once".equals(result[0])) {
            throw new IllegalArgumentException("args must omit the run-once command name");
        }
        int runIds = 0;
        for (int i = 0; i < result.length; i++) {
            String value;
            if ("--run-id".equals(result[i])) {
                if (++i == result.length) {
                    throw new IllegalArgumentException("--run-id requires a value");
                }
                value = result[i];
            } else if (result[i].startsWith("--run-id=")) {
                value = result[i].substring("--run-id=".length());
            } else {
                continue;
            }
            runIds++;
            if (!runId.equals(value)) {
                throw new IllegalArgumentException("--run-id must match request run_id");
            }
        }
        if (runIds != 1) {
            throw new IllegalArgumentException("args must contain exactly one --run-id");
        }
        return result;
    }

    private static void writeResponse(ObjectMapper mapper, PrintStream protocol, String runId,
                                      int exitCode) throws JsonProcessingException {
        var response = mapper.createObjectNode();
        response.put("run_id", runId);
        response.put("exit_code", exitCode);
        protocol.println(mapper.writeValueAsString(response));
        protocol.flush();
    }
}
