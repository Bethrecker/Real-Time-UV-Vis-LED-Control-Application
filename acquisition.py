"""Isolated spectrometer process: a stuck native read cannot hold the LED path."""
from dataclasses import asdict, replace
from datetime import datetime, timezone
import multiprocessing as mp
import queue
import time
import math
from models import Capture, Stopped, TimingOverrun, PreviewProcessing, SDK_PROCESSING
from processing import average, boxcar
from spectrometer import OceanDirectAdapter, Identity, Settings, Spectrum, validate_spectrum


def utc():
    return datetime.now(timezone.utc).isoformat()


class SimulationSpectrometer:
    def __init__(self):
        self.identity = Identity("SIM-QEP06321", "Simulated QE-PRO", (1, 30, 4), 10000, 1600000000, 1, 200000)
        self.settings = Settings(10000, 3, 2)
        self.wavelengths = tuple(200 + i * 2 for i in range(401))
        self.phase = "blank"
        self.cycle = 0

    def connect(self):
        return self.identity

    def apply(self, settings):
        if not self.identity.minimum_us <= settings.integration_us <= 5000000:
            raise ValueError("Simulation integration must be 10–5000 ms.")
        self.settings = settings

    def measurement_mode(self):
        return self.configure_preview(SDK_PROCESSING)

    def configure_preview(self, processing):
        processing.validate()
        self.preview_processing = processing
        return {"sdk_scans_to_average": self.settings.scans, "sdk_boxcar_width": self.settings.boxcar,
                "electric_dark": processing.electric_dark, "nonlinearity": processing.nonlinearity}

    def preview_result(self):
        values = average([self.single() for _ in range(self.settings.scans)])
        if self.preview_processing.electric_dark:
            values = tuple(value - 1500 for value in values)
        # No fabricated detector nonlinearity coefficients in simulation.
        return boxcar(values, self.settings.boxcar)

    def single(self):
        scale = self.settings.integration_us / 10000
        values = []
        for w in self.wavelengths:
            probe = scale * (24000 + 18000 * math.exp(-((w-530)/140)**2))
            attenuation = 1
            if self.phase == "sample":
                attenuation = 10 ** (-(0.15 + 0.012 * self.cycle) * math.exp(-((w-430)/45)**2))
            values.append(1500 + (0 if self.phase == "blocked" else probe * attenuation))
        return tuple(values)

    def close(self):
        return []


class FreshOceanSpectrometer(OceanDirectAdapter):
    """Vendor-configured spectra with checks on retained spectrum history."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.processing_mode = None

    def connect(self):
        identity = super().connect()
        try:
            self.buffer_policy = self.check_buffer_policy()
            self.identity = replace(identity, buffer_policy=dict(self.buffer_policy))
            return self.identity
        except Exception as exc:
            errors = self.close()
            if errors:
                raise RuntimeError(f"{exc}; cleanup also failed: {'; '.join(errors)}") from exc
            raise

    @staticmethod
    def _sdk_code(exc):
        details = getattr(exc, "get_error_details", None)
        return details()[0] if callable(details) else None

    def _single_slot_policy(self):
        values = {}
        for method in ("get_data_buffer_capacity", "get_data_buffer_number_of_elements"):
            try:
                values[method] = getattr(self.device.Advanced, method)()
            except Exception as exc:
                raise RuntimeError(f"OceanDirect Advanced.{method} failed (SDK code {self._sdk_code(exc)}): {exc}") from exc
        capacity, elements = values.values()
        if type(capacity) is not int or capacity != 1 or type(elements) is not int or not 0 <= elements <= 1:
            raise RuntimeError(f"QE-PRO requires a single-slot buffer for this acquisition policy; capacity={capacity}, stored={elements}. No buffer settings were changed.")
        return {"mode": "qepro_single_slot", "capacity": capacity, "stored_at_query": elements,
                "enable_flag": "unsupported_command_24", "host_checked_utc": utc(),
                "queries": list(values), "freshness_verified": False,
                "scope": "Only one spectrum can be retained. This does not verify exposure identity, age, or optical timing."}

    def check_buffer_policy(self):
        # The QE-PRO/SDK 1.30.4 combination was queried on QEP06321: the
        # enable getter returns vendor error 24 while capacity/count succeed.
        # A one-slot buffer cannot hold a FIFO history. It is NOT disabled.
        # No buffer clears, setters, trigger changes or acquisition commands.
        try:
            enabled = self.device.Advanced.get_data_buffer_enable()
        except Exception as exc:
            if (self.identity.model == "QE-PRO" and tuple(self.identity.api_version) == (1, 30, 4)
                    and self._sdk_code(exc) == 24):
                return self._single_slot_policy()
            raise RuntimeError(f"OceanDirect Advanced.get_data_buffer_enable failed (SDK code {self._sdk_code(exc)}): {exc}") from exc
        if enabled:
            raise RuntimeError("Spectrometer data buffering is enabled. No buffer settings were changed.")
        return {"mode": "disabled_enable_flag", "enable_flag": False, "host_checked_utc": utc(),
                "queries": ["get_data_buffer_enable"], "freshness_verified": False}

    def _corrections(self, processing):
        processing.validate()
        d = self.device
        # Disable nonlinearity first, or enable dark first, to respect the
        # vendor's correction dependency. Read both settings back explicitly.
        if not processing.nonlinearity:
            d.set_nonlinearity_correction_usage(False)
        d.set_electric_dark_correction_usage(processing.electric_dark)
        if processing.nonlinearity:
            d.set_nonlinearity_correction_usage(True)
        actual = PreviewProcessing(d.get_electric_dark_correction_usage(), d.get_nonlinearity_correction_usage())
        if actual != processing:
            raise RuntimeError(f"SDK correction readback differs: requested {processing}, returned {actual}.")
        return actual

    def apply(self, settings):
        OceanDirectAdapter.apply(self, settings)
        self._corrections(SDK_PROCESSING)
        self.processing_mode = (self.settings, SDK_PROCESSING)
        return self.settings

    def measurement_mode(self):
        # Preview, dark, reference and sample share the standard SDK policy.
        # Keep selected integration/N/boxcar and SDK corrections unchanged.
        return self.configure_preview(SDK_PROCESSING)

    def configure_preview(self, processing):
        processing.validate()
        key = (self.settings, processing)
        if self.processing_mode != key:
            # Apply all three documented vendor settings and verify readback.
            OceanDirectAdapter.apply(self, self.settings)
            self._corrections(processing)
            self.processing_mode = key
        return {"sdk_scans_to_average": self.settings.scans, "sdk_boxcar_width": self.settings.boxcar,
                "electric_dark": processing.electric_dark, "nonlinearity": processing.nonlinearity}

    def preview_result(self):
        # SDK applies temporal averaging, smoothing and chosen corrections.
        # Do not repeat these operations on its already-processed result.
        return self.single()

    def single(self):
        # Count and capacity checks are included in the configured read budget.
        # Recheck before every spectrum; a changed/unknown state stops the run.
        if self.buffer_policy["mode"] == "qepro_single_slot":
            self.buffer_policy = self._single_slot_policy()
        else:
            self.buffer_policy = self.check_buffer_policy()
        actual = Settings(self.device.get_integration_time(), self.device.get_scans_to_average(), self.device.get_boxcar_width())
        if actual != self.settings:
            raise RuntimeError(f"SDK acquisition settings changed: expected {self.settings}, got {actual}. No spectrum accepted.")
        corrections = PreviewProcessing(self.device.get_electric_dark_correction_usage(), self.device.get_nonlinearity_correction_usage())
        if isinstance(self.processing_mode, tuple) and corrections != self.processing_mode[1]:
            raise RuntimeError(f"SDK correction settings changed: expected {self.processing_mode[1]}, got {corrections}. No spectrum accepted.")
        return tuple(self.device.get_formatted_spectrum())


def acquire_capture(adapter, budget, cancel, deadline=None, enforce_timing=True):
    """One vendor spectrum at the selected T/N/boxcar, without host filtering."""
    actual = adapter.measurement_mode()
    settings = adapter.settings
    start, start_mono = utc(), time.monotonic()
    target = start_mono + budget.scan_period(settings)
    while time.monotonic() < target:
        if cancel.wait(min(0.01, max(0, target-time.monotonic()))):
            raise Stopped()
    if cancel.is_set():
        raise Stopped()
    read_start, stamp = time.monotonic(), utc()
    if enforce_timing and read_start-target > budget.lateness_s:
        raise TimingOverrun(f"SDK spectrum read started {(read_start-target)*1000:.1f} ms late; allowance is {budget.lateness_s*1000:.1f} ms.")
    values = tuple(adapter.preview_result())
    read_end = time.monotonic()
    if cancel.is_set():
        raise Stopped()
    validate_spectrum(adapter.wavelengths, values)
    allowed = budget.sdk_read_budget(settings)
    if enforce_timing and read_end-read_start > allowed:
        raise TimingOverrun(f"SDK averaged spectrum read took {(read_end-read_start)*1000:.1f} ms; budget for {settings.scans} scans at {settings.integration_us/1000:g} ms is {allowed*1000:.1f} ms. Adjust measured budgets and off duration before a new experiment.")
    if deadline is not None and read_end > deadline:
        raise TimingOverrun("Spectrum returned after the off-period acquisition deadline.")
    finished = utc()
    spectrum = Spectrum(adapter.wavelengths, values, settings, adapter.identity.serial, start, finished, read_end-start_mono)
    provenance = {"strategy": "sdk_configured_intensity", **actual, "raw_source_scans_available": False,
                  "host_averaging_applied": False, "host_boxcar_applied": False,
                  "sdk_settings_readback_verified": not isinstance(adapter, SimulationSpectrometer),
                  "averaging_behavior_verified": False, "freshness_verified": False, "optical_timing_verified": False,
                  "freshness": "two estimated averaging windows before one SDK formatted result; individual detector exposure identities unavailable",
                  "scan_calls": [{"call_start_utc": stamp, "call_end_utc": finished, "call_start_monotonic_s": read_start,
                                  "call_end_monotonic_s": read_end, "fresh_wait_target_monotonic_s": target,
                                  "buffer_policy": dict(getattr(adapter, "buffer_policy", {}))}],
                  "buffer_policy_at_capture_end": dict(getattr(adapter, "buffer_policy", {})),
                  "estimated_envelope_start_monotonic_s": start_mono,
                  "estimated_envelope_end_monotonic_s": start_mono+budget.collection_window(settings),
                  "timing_budget": asdict(budget), "experiment_read_budget_enforced": enforce_timing,
                  "saturation_scope": "SDK returned pixels only; individual unaveraged/unsmoothed source saturation is unavailable",
                  "timestamps": "UTC/monotonic host timestamps; not measured optical events"}
    # This array is the exact SDK result, not an individual raw detector scan.
    return Capture(spectrum, (values,), provenance)


def acquire_preview(adapter, budget, processing, cancel):
    """One SDK intensity result with correction readback and no host filtering."""
    processing.validate()
    settings = adapter.settings
    actual = adapter.configure_preview(processing)
    started, start_mono = utc(), time.monotonic()
    # Wait for two estimated averaged windows after mode/parameter changes.
    # This remains an age estimate, never a proof of distinct detector frames.
    wait_s = 2 * settings.scans * (settings.integration_us / 1e6 + budget.readout_s + budget.uncertainty_s)
    if cancel.wait(wait_s):
        raise Stopped()
    call_start, stamp = time.monotonic(), utc()
    values = tuple(adapter.preview_result())
    call_end = time.monotonic()
    if cancel.is_set():
        raise Stopped()
    validate_spectrum(adapter.wavelengths, values)
    spectrum = Spectrum(adapter.wavelengths, values, settings, adapter.identity.serial, started, utc(), call_end-start_mono)
    provenance = {"strategy": "sdk_processed_preview", "raw_source_scans_available": False,
                  "host_averaging_applied": False, "host_boxcar_applied": False,
                  "preview_processing": asdict(processing), **actual,
                  "sdk_settings_readback_verified": not isinstance(adapter, SimulationSpectrometer),
                  "averaging_behavior_verified": False, "freshness_verified": False, "optical_timing_verified": False,
                  "simulation": isinstance(adapter, SimulationSpectrometer),
                  "simulation_nonlinearity_modelled": False,
                  "buffer_policy": dict(getattr(adapter, "buffer_policy", {})),
                  "scan_calls": [{"call_start_utc": stamp, "call_start_monotonic_s": call_start,
                                  "call_end_monotonic_s": call_end, "call_end_utc": spectrum.finished_utc}],
                  "settling_wait_estimate_s": wait_s,
                  "timestamps": "Host timestamps; preview is not an experiment acquisition."}
    # The sole stored array is the SDK's processed result, not a raw exposure.
    return Capture(spectrum, (values,), provenance)


def _spectrometer_process(conn, simulation, sdk, serial, cancel):
    adapter = SimulationSpectrometer() if simulation else FreshOceanSpectrometer(sdk, serial)
    try:
        identity = adapter.connect()
        conn.send((True, identity))
        while True:
            command, payload = conn.recv()
            try:
                if command == "close":
                    break
                if command == "apply":
                    adapter.apply(payload)
                    result = adapter.settings
                elif command == "configure_preview":
                    result = adapter.configure_preview(payload)
                elif command == "capture":
                    budget, deadline, phase, cycle = payload
                    if simulation:
                        adapter.phase, adapter.cycle = phase, cycle
                    result = acquire_capture(adapter, budget, cancel, deadline, enforce_timing=cycle > 0 or deadline is not None)
                elif command == "preview_capture":
                    budget, processing, phase = payload
                    if simulation:
                        adapter.phase = phase
                    result = acquire_preview(adapter, budget, processing, cancel)
                else:
                    raise ValueError("Unknown spectrometer request")
                conn.send((True, result))
            except Exception as exc:
                conn.send((False, f"{type(exc).__name__}: {exc}"))
    except Exception as exc:
        try:
            conn.send((False, str(exc)))
        except (BrokenPipeError, EOFError):
            pass
    finally:
        try:
            errors = adapter.close()
            conn.send((not errors, errors))
        except Exception as exc:
            try:
                conn.send((False, str(exc)))
            except (BrokenPipeError, EOFError):
                pass
        conn.close()


class SpectrometerClient:
    def __init__(self, simulation, sdk, serial):
        context = mp.get_context("spawn")
        self.cancel = context.Event()
        self.conn, child = context.Pipe()
        self.process = context.Process(target=_spectrometer_process, args=(child, simulation, sdk, serial, self.cancel), daemon=True)
        self.process.start()
        child.close()
        try:
            self.identity = self.receive(15)
        except Exception:
            self.close()
            raise

    def receive(self, timeout, interrupt=None):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            if interrupt is not None and interrupt.is_set():
                self.cancel.set()
                raise Stopped()
            if self.conn.poll(0.01):
                ok, value = self.conn.recv()
                if not ok:
                    if str(value).startswith("Stopped:"):
                        raise Stopped()
                    raise RuntimeError(str(value))
                return value
            if not self.process.is_alive():
                raise RuntimeError("Spectrometer process exited unexpectedly.")
        self.cancel.set()
        raise TimingOverrun("Spectrometer request timed out. LED control remains independent; no more pulses are allowed.")

    def apply(self, settings):
        self.conn.send(("apply", settings))
        return self.receive(5)

    def capture(self, budget, phase="sample", cycle=0, deadline=None, interrupt=None):
        self.cancel.clear()
        self.conn.send(("capture", (budget, deadline, phase, cycle)))
        timeout = 30 if deadline is None else max(0.01, deadline-time.monotonic())
        return self.receive(timeout, interrupt)

    def configure_preview(self, processing):
        self.conn.send(("configure_preview", processing))
        return self.receive(5)

    def preview(self, budget, processing=SDK_PROCESSING, phase="blank", interrupt=None):
        self.cancel.clear()
        self.conn.send(("preview_capture", (budget, processing, phase)))
        return self.receive(30, interrupt)

    def close(self):
        self.cancel.set()
        errors = []
        shutdown_reply = False
        try:
            if self.process.is_alive():
                self.conn.send(("close", None))
                # Drain replies so a large spectrum cannot block clean close.
                until = time.monotonic() + 2
                while self.process.is_alive() and time.monotonic() < until:
                    if self.conn.poll(0.02):
                        try:
                            ok, value = self.conn.recv()
                        except (EOFError, OSError):
                            break
                        if isinstance(value, list):
                            shutdown_reply = True
                        if not ok and not str(value).startswith("Stopped:"):
                            errors.append(str(value))
                    self.process.join(0.01)
                if self.process.is_alive():
                    self.process.terminate()
                    self.process.join(2)
                    errors.append("Spectrometer call did not return; process terminated. SDK setting restoration/clean close not confirmed.")
                else:
                    while self.conn.poll():
                        try:
                            ok, value = self.conn.recv()
                        except (EOFError, OSError):
                            break
                        if isinstance(value, list):
                            shutdown_reply = True
                        if not ok and not str(value).startswith("Stopped:"):
                            errors.append(str(value))
        except (BrokenPipeError, EOFError, OSError) as exc:
            if not shutdown_reply:
                errors.append(str(exc))
        finally:
            self.conn.close()
        if not shutdown_reply and not errors:
            errors.append("Spectrometer process exited without acknowledging SDK cleanup.")
        return errors
