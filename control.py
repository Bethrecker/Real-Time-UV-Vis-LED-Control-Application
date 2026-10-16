"""Absolute phase deadlines with explicit LED command overhead; overruns stop."""
import time
from models import Stopped, TimingOverrun
from processing import process, usable_reference


def command_result(future, timeout, operation):
    try:
        return future.result(timeout)
    except TimeoutError as exc:
        raise TimingOverrun(f"{operation} did not acknowledge within the LED command budget ({timeout*1000:.0f} ms). Further pulses inhibited; Disable requested during shutdown. Increase this budget only after checking the device/SDK response.") from exc


def validate_observed_read_budget(settings, *captures):
    """Reject an already-observed slow read before irradiation starts."""
    budget = settings.timing.sdk_read_budget(settings.acquisition)
    for capture in captures:
        for call in capture.provenance.get("scan_calls", []):
            if "call_start_monotonic_s" in call and "call_end_monotonic_s" in call:
                elapsed = call["call_end_monotonic_s"] - call["call_start_monotonic_s"]
                if elapsed > budget:
                    raise ValueError(f"Background read took {elapsed*1000:.1f} ms; run SDK read budget is {budget*1000:.1f} ms. Increase the measured read budget and off-period, apply settings, and recapture backgrounds before Start. No experiment pulse was issued.")


class ExperimentController:
    def __init__(self, led, spectrometer, store, stop, emit, now=time.monotonic):
        self.led, self.spectrometer, self.store, self.stop, self.emit = led, spectrometer, store, stop, emit
        self.now = now

    def wait_until(self, target):
        while self.now() < target:
            if self.stop.wait(min(0.01, max(0, target-self.now()))):
                raise Stopped()
        if self.stop.is_set():
            raise Stopped()

    def wait_for_save(self, future, deadline, cycle):
        available = max(0.001, deadline-self.now())
        try:
            return future.result(available)
        except TimeoutError as exc:
            location = getattr(self.store, "working_directory", "the selected output folder")
            guidance = (
                "Saving already uses the local recovery cache. Increase LED off duration to give saving more time; "
                "changing the Excel output folder does not move this cache."
                if getattr(self.store, "cache_enabled", False) else
                "Use a local output folder or a longer LED off duration if disk saving remains too slow."
            )
            phase = "before irradiation" if cycle == 0 else "before the next pulse deadline"
            raise TimingOverrun(f"Cycle {cycle} durable file save did not finish within {available*1000:.1f} ms {phase}. Run stopped with LED off; queued files may finish during cleanup, so the final workbook may contain this cycle even if the displayed completed count excludes it. Recovery location: {location}. {guidance}") from exc

    def run(self, settings, dark, reference):
        settings.validate()
        if not usable_reference(dark, reference, self.spectrometer.identity.maximum_intensity).usable:
            raise ValueError("A usable matching dark/reference pair is required.")
        validate_observed_read_budget(settings, dark, reference)
        command_result(self.led.submit("prepare", settings.on_s), settings.timing.command_timeout_s, "LED profile preparation")
        if self.stop.is_set():
            raise Stopped()
        window = settings.timing.collection_window(settings.acquisition)
        save = None
        completed = 0
        status, error = "completed", None
        try:
            # Preparation leaves output disabled. Keep pulses inhibited until
            # the actual sample's time-zero measurement is durably saved.
            command_result(self.led.stop(), settings.timing.command_timeout_s, "Time-zero LED-off command")
            disabled_at = self.now()
            self.emit("phase", (0, "Time 0 UV-Vis / no irradiation", settings.estimated_duration_s))
            baseline_start = self.now()
            baseline_deadline = baseline_start + window + settings.timing.lateness_s
            capture = self.spectrometer.capture(settings.timing, "sample", 0, baseline_deadline, self.stop)
            if self.stop.is_set():
                raise Stopped()
            result = process(capture, dark, reference, self.spectrometer.identity.maximum_intensity)
            timing = {"cycle": 0, "measurement": "time_zero", "irradiation_elapsed_s": 0.0,
                      "host_disable_return_monotonic_s": disabled_at,
                      "collection_actual_host_start_monotonic_s": baseline_start,
                      "collection_actual_host_return_monotonic_s": self.now(),
                      "provenance": "pre-irradiation sample after LED Disable acknowledgement; optical timing not measured",
                      "overrun": self.now() > baseline_deadline}
            self.wait_for_save(self.store.save_cycle(0, capture, result, timing), self.now()+5, 0)
            self.emit("baseline", (0, capture, result, timing))
            if timing["overrun"]:
                raise TimingOverrun("Time-zero acquisition exceeded its deadline; result preserved, no pulse issued.")
            if any(flag in ("saturated_source_scan", "saturated_sdk_intensity") for flag in result.flags):
                raise ValueError("Time-zero sample saturated: values/flags preserved; no pulse issued.")
            if not any(value is not None for value in result.absorbance):
                raise ValueError("No valid time-zero absorbance pixels: values preserved; no pulse issued.")
            check_health = getattr(self.store, "check_health", None)
            if check_health:
                check_health()
            if self.stop.is_set():
                raise Stopped()
            self.led.arm()
            cycle_start = self.now() + 0.05
            for cycle in range(1, settings.cycles+1):
                if save is not None:
                    # LED remains disabled while waiting for durable storage.
                    self.wait_for_save(save, cycle_start, cycle-1)
                check_health = getattr(self.store, "check_health", None)
                if check_health:
                    check_health()
                self.wait_until(cycle_start)
                if self.now() > cycle_start + settings.timing.lateness_s:
                    raise TimingOverrun("Next cycle deadline missed; no next pulse was issued.")
                remaining_cycles = settings.cycles-cycle+1
                self.emit("phase", (cycle, "LED command / finite pulse", remaining_cycles * settings.estimated_cycle_s))
                command_result(self.led.on(), settings.timing.command_timeout_s, "LED-on command")
                on_ack = self.now()
                command_elapsed = on_ack-cycle_start
                if command_elapsed > settings.timing.command_timeout_s + settings.timing.lateness_s:
                    raise TimingOverrun(f"LED-on command took {command_elapsed*1000:.1f} ms; command budget is {settings.timing.command_timeout_s*1000:.1f} ms.")
                # SoftStart may start output during its call. Acknowledgement is
                # a conservative latest start proxy, NOT a measured optical edge.
                # The finite device pulse controls on-duration; do not truncate
                # it by counting SetMode/SoftStart latency as irradiation time.
                on_end = on_ack + settings.on_s + 20e-6
                self.emit("phase", (cycle, "LED on (host estimate)", max(0, remaining_cycles*settings.estimated_cycle_s-command_elapsed)))
                self.wait_until(on_end)
                if self.now() > on_end + settings.timing.lateness_s:
                    raise TimingOverrun("LED-disable request missed its host deadline; finite pulse duration is not measured.")
                command_result(self.led.submit("off"), settings.timing.command_timeout_s, "LED-off command")
                disabled_at = self.now()
                if disabled_at > on_end + settings.timing.command_timeout_s + settings.timing.lateness_s:
                    raise TimingOverrun("LED-off command exceeded its command budget. Optical duration is not measured.")
                # Requested off time begins only after Disable acknowledgement.
                # Command overhead is explicit additional run time. The next
                # pulse cannot consume this acquisition interval.
                cycle_end = disabled_at + settings.off_s
                self.emit("phase", (cycle, "LED off / acquisition", settings.off_s + (remaining_cycles-1)*settings.estimated_cycle_s))
                target = disabled_at + settings.off_s/2 - window/2
                deadline = cycle_end - settings.timing.edge_margin_s
                self.wait_until(target)
                actual_start = self.now()
                if actual_start-target > settings.timing.lateness_s:
                    raise TimingOverrun("Averaging envelope could not start near the center of the off-period.")
                capture = self.spectrometer.capture(settings.timing, "sample", cycle, deadline, self.stop)
                result = process(capture, dark, reference, self.spectrometer.identity.maximum_intensity)
                timing = {"cycle": cycle, "host_schedule_start_monotonic_s": cycle_start, "host_on_end_target_monotonic_s": on_end,
                          "host_disable_return_monotonic_s": disabled_at, "host_on_ack_monotonic_s": on_ack,
                          "off_start_host_ack_monotonic_s": disabled_at, "led_on_command_elapsed_s": command_elapsed, "requested_on_s": settings.on_s, "requested_off_s": settings.off_s, "off_end_target_monotonic_s": cycle_end,
                          "collection_target_start_monotonic_s": target, "collection_target_end_monotonic_s": target+window,
                          "collection_actual_host_start_monotonic_s": actual_start, "collection_actual_host_return_monotonic_s": self.now(),
                          "provenance": "estimated collection envelope and host SDK timestamps; optical timing not measured", "overrun": self.now() > deadline}
                save = self.store.save_cycle(cycle, capture, result, timing)
                # Preserve this result even if its flags or timestamp abort the run.
                self.wait_for_save(save, cycle_end, cycle)
                completed = cycle
                self.emit("cycle", (cycle, capture, result, timing))
                if timing["overrun"]:
                    raise TimingOverrun("Acquisition exceeded off-period deadline; result preserved, no next pulse.")
                if any(flag in ("saturated_source_scan", "saturated_sdk_intensity") for flag in result.flags):
                    raise ValueError("Sample saturated: raw values/flags preserved; run stopped.")
                if not any(value is not None for value in result.absorbance):
                    raise ValueError("No valid absorbance pixels: raw values preserved; run stopped.")
                self.wait_until(cycle_end)
                cycle_start = cycle_end
        except Stopped:
            status = "stopped"
        except Exception as exc:
            status, error = "error", f"{type(exc).__name__}: {exc}"
        finally:
            self.stop.set()  # inhibit future work before requesting off
            try:
                self.led.stop().result(settings.timing.command_timeout_s)
            except Exception as exc:
                status = "shutdown_error"
                error = f"{error or ''} LED-off failure: {exc or type(exc).__name__}".strip()
                self.emit("error", error)
            self.store.finish(status, error).result(5)
        return status, error, completed
