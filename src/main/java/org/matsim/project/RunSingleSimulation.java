package org.matsim.project;

import ch.sbb.matsim.contrib.railsim.RailsimModule;
import ch.sbb.matsim.contrib.railsim.qsimengine.RailsimQSimModule;
import ch.sbb.matsim.contrib.railsim.qsimengine.deadlocks.DeadlockAvoidance;
import ch.sbb.matsim.contrib.railsim.qsimengine.deadlocks.NoDeadlockAvoidance;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.matsim.api.core.v01.Scenario;
import org.matsim.core.api.experimental.events.EventsManager;
import org.matsim.core.config.Config;
import org.matsim.core.config.ConfigUtils;
import org.matsim.core.controler.Controller;
import org.matsim.core.controler.ControllerUtils;
import org.matsim.core.events.EventsUtils;
import org.matsim.core.events.MatsimEventsReader;
import org.matsim.core.mobsim.qsim.AbstractQSimModule;
import org.matsim.core.scenario.ScenarioUtils;
import org.matsim.project.analysis.delay.TrainDelayAnalysis;
import org.matsim.project.analysis.delay.TrainDelayEventHandler;
import org.matsim.project.analysis.headway.HeadwayInfo;
import org.matsim.project.analysis.headway.MinimumHeadwayEventHandler;
import org.matsim.project.analysis.headway.RailsimTrainLeavesLinkEventMapper;
import org.matsim.project.scenario.plan.OperationalPlan;
import org.matsim.project.scenario.plan.OperationalPlanReader;
import org.matsim.project.utils.RailsimConfigHelper;
import org.matsim.pt.transitSchedule.api.Departure;
import org.matsim.pt.transitSchedule.api.TransitRoute;
import org.matsim.pt.transitSchedule.api.TransitScheduleReader;
import org.matsim.vehicles.MatsimVehicleReader;
import picocli.CommandLine;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

/** Runs one explicit timetable and writes machine-readable terminal-delay evidence. */
@CommandLine.Command(name = "run-once", description = "Run one explicit workspace timetable once.")
public final class RunSingleSimulation implements java.util.concurrent.Callable<Integer> {

    @CommandLine.Option(names = "--workspace", required = true) private Path workspaceInput;
    @CommandLine.Option(names = "--output", required = true) private Path outputInput;
    @CommandLine.Option(names = "--operational-plan", required = true) private Path planInput;
    @CommandLine.Option(names = "--run-id", required = true) private String runIdInput;
    @CommandLine.Option(names = "--seed", required = true) private long seedInput;
    @CommandLine.Option(names = "--horizon-seconds", required = true) private int horizonInput;

    @Override
    public Integer call() throws Exception {
        Path workspace = workspaceInput.toRealPath();
        Path output = outputInput.toAbsolutePath().normalize();
        Path plan = planInput.toRealPath();
        String runId = runIdInput;
        long seed = seedInput;
        int horizon = horizonInput;
        if (horizon < 600 || horizon > 172800) throw new IllegalArgumentException("horizon must be 600..172800 seconds");
        Files.createDirectories(output);

        Config config = ConfigUtils.loadConfig(workspace.resolve("config.xml").toString());
        config.global().setRandomSeed(seed);
        config.network().setInputFile(workspace.resolve("network.xml").toString());
        config.transit().setTransitScheduleFile(workspace.resolve("schedule.xml").toString());
        config.transit().setVehiclesFile(workspace.resolve("vehicles.xml").toString());
        config.controller().setRunId(runId);
        config.controller().setOutputDirectory(output.toString());
        config.qsim().setStartTime(0);
        config.qsim().setEndTime(horizon);
        config.qsim().setStuckTime(1_000_000);
        config.qsim().setRemoveStuckVehicles(false);
        RailsimConfigHelper.configure(config);

        Scenario scenario = ScenarioUtils.loadScenario(config);
        // Keep inactive route definitions in the workspace/snapshot, but do not feed
        // them to SwissRailRaptor, which requires an earliest departure per route.
        for (var line : new ArrayList<>(scenario.getTransitSchedule().getTransitLines().values())) {
            for (var route : new ArrayList<>(line.getRoutes().values())) {
                if (route.getDepartures().isEmpty()) line.removeRoute(route);
            }
            if (line.getRoutes().isEmpty()) scenario.getTransitSchedule().removeTransitLine(line);
        }
        Controller controller = ControllerUtils.createController(scenario);
        controller.addOverridingModule(new RailsimModule());
        controller.configureQSimComponents(c -> new RailsimQSimModule().configure(c));
        // Experiment switch: RAILSIM_DEADLOCK_AVOIDANCE=none disables railsim's default (simple) deadlock avoidance.
        String deadlockAvoidance = System.getenv().getOrDefault("RAILSIM_DEADLOCK_AVOIDANCE", "simple");
        if (!deadlockAvoidance.equals("simple")) {
            Class<? extends DeadlockAvoidance> implementation = switch (deadlockAvoidance) {
                case "none" -> NoDeadlockAvoidance.class;
                default -> throw new IllegalArgumentException("RAILSIM_DEADLOCK_AVOIDANCE must be simple or none");
            };
            controller.addOverridingQSimModule(new AbstractQSimModule() {
                @Override
                protected void configureQSim() {
                    bind(DeadlockAvoidance.class).to(implementation).asEagerSingleton();
                }
            });
        }
        controller.run();
        RailsimConfigHelper.writeStaticOutputFiles(controller);

        Path events = output.resolve("ITERS/it.0/" + runId + ".0.events.xml.gz");
        if (!Files.isRegularFile(events)) throw new IllegalStateException("Simulation completed without an events file");
        Scenario input = ScenarioUtils.createScenario(ConfigUtils.createConfig());
        new TransitScheduleReader(input).readFile(workspace.resolve("schedule.xml").toString());
        new MatsimVehicleReader(input.getTransitVehicles()).readFile(workspace.resolve("vehicles.xml").toString());
        Map<String, ExpectedDeparture> expected = new TreeMap<>();
        Map<String, String> vehicleDepartures = new TreeMap<>();
        for (var line : input.getTransitSchedule().getTransitLines().values()) {
            for (TransitRoute route : line.getRoutes().values()) {
                if (route.getStops().size() < 2) {
                    throw new IllegalArgumentException("Route " + route.getId() + " requires at least two stops");
                }
                var terminalStop = route.getStops().getLast();
                for (Departure departure : route.getDepartures().values()) {
                    String departureId = departure.getId().toString();
                    String vehicleId = departure.getVehicleId().toString();
                    String prior = vehicleDepartures.putIfAbsent(vehicleId, departureId);
                    if (prior != null) {
                        throw new IllegalArgumentException("Vehicle " + vehicleId + " is assigned to multiple "
                                + "scheduled departures (" + prior + ", " + departureId + ")");
                    }
                    if (departure.getDepartureTime() >= 0 && departure.getDepartureTime() < horizon) {
                        expected.put(departureId, new ExpectedDeparture(vehicleId, route.getId().toString(),
                                departure.getDepartureTime(), departure.getDepartureTime()
                                + terminalStop.getArrivalOffset().seconds(), terminalStop.getStopFacility().getId().toString()));
                    }
                }
            }
        }
        TrainDelayEventHandler handler = new TrainDelayEventHandler(input.getTransitSchedule(), input.getTransitVehicles());
        Map<String, TransitRoute> routes = routesById(input.getTransitSchedule().getTransitLines());
        OperationalPlan operationalPlan = new OperationalPlanReader().read(plan);
        MinimumHeadwayEventHandler headwayHandler = new MinimumHeadwayEventHandler(scenario, operationalPlan);
        EventsManager manager = EventsUtils.createEventsManager();
        manager.addHandler(handler);
        manager.addHandler(headwayHandler);
        MatsimEventsReader eventReader = new MatsimEventsReader(manager);
        eventReader.addCustomEventMapper(ch.sbb.matsim.contrib.railsim.events.RailsimTrainLeavesLinkEvent.EVENT_TYPE,
                new RailsimTrainLeavesLinkEventMapper());
        eventReader.readFile(events.toString());
        Map<String, TrainDelayAnalysis.DetailedStopInfo> terminal = new TreeMap<>();
        for (var row : handler.getStopEvents()) {
            int terminalSequence = routes.get(row.routeId().toString()).getStops().size() - 1;
            if (row.stopSequence() == terminalSequence) terminal.put(row.departureId().toString(), row);
        }
        List<Map<String, Object>> trains = new ArrayList<>();
        List<Double> delays = new ArrayList<>();
        var dispatchedVehicles = handler.getDepartedTrains().stream()
                .map(Object::toString).collect(java.util.stream.Collectors.toSet());
        var arrivedVehicles = handler.getArrivedTrains().stream()
                .map(Object::toString).collect(java.util.stream.Collectors.toSet());
        int notStarted = 0;
        int unfinished = 0;
        for (var item : expected.entrySet()) {
            String departureId = item.getKey();
            ExpectedDeparture schedule = item.getValue();
            var row = terminal.get(departureId);
            boolean started = dispatchedVehicles.contains(schedule.vehicleId());
            boolean arrived = arrivedVehicles.contains(schedule.vehicleId())
                    && row != null && Double.isFinite(row.actualArrival());
            if (!started) notStarted++;
            else if (!arrived) unfinished++;
            double delay = arrived ? row.arrivalDelay() : Double.NaN;
            if (arrived) delays.add(delay);
            Map<String, Object> train = new LinkedHashMap<>();
            train.put("vehicle_id", schedule.vehicleId());
            train.put("departure_id", departureId);
            train.put("route_id", schedule.routeId());
            train.put("terminal_stop_id", schedule.terminalStopId());
            train.put("scheduled_departure", schedule.departureTime());
            train.put("status", arrived ? "terminal_arrived" : started ? "unfinished" : "not_started");
            train.put("arrived", arrived);
            train.put("planned_arrival", schedule.plannedArrival());
            train.put("actual_arrival", arrived ? row.actualArrival() : null);
            train.put("terminal_delay_seconds", arrived ? delay : null);
            trains.add(train);
        }
        if (expected.isEmpty()) {
            throw new IllegalStateException("No scheduled departure falls within the simulation horizon");
        }
        Collections.sort(delays);
        double mean = delays.stream().mapToDouble(Double::doubleValue).average().orElse(0);
        double p95 = delays.isEmpty() ? 0 : delays.get(Math.max(0, (int)Math.ceil(delays.size() * .95) - 1));
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("run_id", runId);
        result.put("seed", seed);
        result.put("horizon_seconds", horizon);
        result.put("deadlock_avoidance", deadlockAvoidance);
        result.put("status", "completed");
        result.put("trains_expected", expected.size());
        result.put("trains_departed", handler.getDepartedTrains().size());
        result.put("trains_arrived", delays.size());
        result.put("trains_not_started", notStarted);
        result.put("trains_unfinished", unfinished);
        result.put("terminal_delay_sample_count", delays.size());
        result.put("mean_terminal_delay_seconds", delays.isEmpty() ? null : mean);
        result.put("p95_terminal_delay_seconds", delays.isEmpty() ? null : p95);
        result.put("max_terminal_delay_seconds", delays.isEmpty() ? null : delays.getLast());
        result.put("delayed_train_count", delays.stream().filter(d -> d > 0).count());
        result.put("trains", trains);
        List<Map<String, Object>> headwayConflicts = new ArrayList<>();
        for (HeadwayInfo info : headwayHandler.getHeadwayEvents()) {
            if (info.getViolationHeadToHead() <= 0 && info.getViolationTailToHead() <= 0) continue;
            Map<String, Object> conflict = new LinkedHashMap<>();
            conflict.put("link_id", info.getLinkId().toString());
            conflict.put("previous_vehicle_id", info.getPreviousVehicleId().toString());
            conflict.put("following_vehicle_id", info.getFollowingVehicleId().toString());
            conflict.put("headway_head_to_head_seconds", info.getHeadwayHeadToHead());
            conflict.put("headway_tail_to_head_seconds", info.getHeadwayTailToHead());
            conflict.put("required_headway_seconds", info.getMinimumHeadway());
            conflict.put("violation_head_to_head_seconds", info.getViolationHeadToHead());
            conflict.put("violation_tail_to_head_seconds", info.getViolationTailToHead());
            headwayConflicts.add(conflict);
        }
        result.put("headway_conflict_count", headwayConflicts.size());
        result.put("headway_conflicts", headwayConflicts);
        result.put("events_file", events.toString());
        new ObjectMapper().writerWithDefaultPrettyPrinter().writeValue(output.resolve("simulation.json").toFile(), result);
        return 0;
    }

    private static Map<String, TransitRoute> routesById(
            Map<org.matsim.api.core.v01.Id<org.matsim.pt.transitSchedule.api.TransitLine>,
                    org.matsim.pt.transitSchedule.api.TransitLine> lines) {
        Map<String, TransitRoute> routes = new TreeMap<>();
        lines.values().forEach(line -> line.getRoutes().forEach((id, route) -> routes.put(id.toString(), route)));
        return routes;
    }

    private record ExpectedDeparture(String vehicleId, String routeId, double departureTime,
                                     double plannedArrival, String terminalStopId) { }
}
