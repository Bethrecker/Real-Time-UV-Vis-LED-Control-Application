"""Spectrometer setup and experiment orchestration; LED connection is independent."""
from dataclasses import asdict, replace
import queue
import threading
from acquisition import SpectrometerClient
from control import ExperimentController
from models import Stopped
from processing import process, usable_reference
from storage import RunStore


class Session(threading.Thread):
    def __init__(self, simulation, sdk, serial, approval, settings, events, led_connection=None):
        super().__init__(name="Spectrometer session", daemon=True)
        self.simulation, self.sdk, self.serial, self.approval = simulation, sdk, serial, approval
        self.settings, self.events = settings, events
        self.led_connection = led_connection
        self.commands = queue.Queue()
        self.stop_event = threading.Event()
        self.experiment_stop = None
        self.preview_absorbance = False
        self.preview = False
        self.phase = "blank"
        self.spec = self.store = None
        self.dark = self.reference = None
        self.experiment_started = self.setup_busy = False
        self.setup_lock = threading.Lock()

    @property
    def led(self):
        return self.led_connection.led if self.led_connection else None

    def emit(self, kind, value):
        self.events.put((kind, value))

    def halt(self):
        self.stop_event.set()
        if self.experiment_stop:
            self.experiment_stop.set()
        if self.spec:
            self.spec.cancel.set()
        if self.experiment_started and self.led_connection:
            self.led_connection.halt()

    def storage_error(self, message):
        self.emit("error", message)
        self.halt()  # independent LED-off path, even if a spectrum read blocks

    def invalidate_backgrounds(self):
        with self.setup_lock:
            self.dark = self.reference = None

    def _setup_command(self, command, payload):
        connection = None
        with self.setup_lock:
            self.setup_busy = True
        try:
            if command == "apply":
                self.spec.apply(payload.acquisition)
                self.spec.configure_preview(payload.preview_processing)
                self.settings = payload
                self.invalidate_backgrounds()
                self.emit("applied", payload)
            else:
                connection = self.led_connection
                if connection:
                    connection.begin_background()
                capture = self.spec.capture(self.settings.timing, self.phase, interrupt=self.stop_event)
                if command == "dark":
                    self.dark, self.reference = capture, None
                else:
                    if command == "blank":
                        capture = replace(capture, provenance={**capture.provenance, "reference_role": "new_blank_slide"})
                    self.reference = capture
                self.emit("reference" if command == "blank" else command, capture)
        finally:
            if connection:
                connection.end_background()
            with self.setup_lock:
                self.setup_busy = False

    def run(self):
        errors = []
        excel = False
        try:
            self.spec = SpectrometerClient(self.simulation, self.sdk, self.serial)
            self.spec.apply(self.settings.acquisition)
            self.spec.configure_preview(self.settings.preview_processing)
            if self.stop_event.is_set():
                raise Stopped()
            self.emit("connected", (self.spec.identity, None, self.settings.acquisition))
            while not self.stop_event.is_set():
                try:
                    command, payload = self.commands.get(timeout=0.1)
                except queue.Empty:
                    if self.preview:
                        with self.setup_lock:
                            dark, reference = self.dark, self.reference
                        if self.preview_absorbance and usable_reference(dark, reference, self.spec.identity.maximum_intensity).usable:
                            capture = self.spec.capture(self.settings.timing, self.phase, interrupt=self.stop_event)
                            result = process(capture, dark, reference, self.spec.identity.maximum_intensity)
                            self.emit("absorbance_preview", (capture, result, dark, reference))
                        else:
                            capture = self.spec.preview(self.settings.timing, self.settings.preview_processing, self.phase, interrupt=self.stop_event)
                            self.emit("preview", capture)
                    continue
                if self.stop_event.is_set():
                    raise Stopped()
                if command == "preview":
                    self.preview = payload[0]
                    if payload[1] is not None:
                        self.phase = payload[1]
                    self.preview_absorbance = bool(payload[2]) if len(payload) > 2 else False
                elif command in ("apply", "dark", "reference", "blank"):
                    self._setup_command(command, payload)
                elif command == "start":
                    if not self.led_connection:
                        raise ValueError("Connect the LED controller before starting the experiment.")
                    folder, name, excel = payload[:3]
                    if len(payload) > 3:
                        self.approval = payload[3]
                    if len(payload) > 4:
                        self.settings = payload[4]
                    self.settings.validate()
                    # Apply selected SDK settings before any LED pulse.
                    # Standard corrections remain the same in every mode.
                    self.spec.apply(self.settings.acquisition)
                    self.experiment_stop = threading.Event()
                    self.led_connection.reserve(self.approval, self.experiment_stop)
                    self.experiment_started = True
                    if self.stop_event.is_set():
                        raise Stopped()
                    self.preview = False
                    self.store = RunStore(folder, name, self.settings, self.dark, self.reference,
                                          {"simulation": self.simulation, "spectrometer": asdict(self.spec.identity),
                                           "mightex_serial": "SIMULATED" if self.simulation else "04-210326-002",
                                           "output_approval": asdict(self.approval) if self.approval else None,
                                           "sdk_directory": str(self.sdk),
                                           "timing_policy": "finite hardware pulse; on guard after SoftStart acknowledgement; off interval after Disable acknowledgement; command overhead adds host run time; fixed off acquisition deadline; optical timing unverified",
                                           "pulse_definition": {"leading_off_us": 20, "on_us": round(self.settings.on_s*1e6), "trailing_off_us": 20, "repeat_count": 1, "pulse_count": 1, "optical_timing_verified": False}}, on_error=self.storage_error, combined_excel_only=True)
                    run_store = self.store
                    self.led_connection.log = lambda record: run_store.event(record)
                    self.emit("run_folder", str(self.store.directory))
                    if self.store.cache_enabled:
                        self.emit("storage_cache", (str(self.store.working_directory), str(self.store.directory)))
                    controller = ExperimentController(self.led, self.spec, self.store, self.experiment_stop, self.emit)
                    status, error, completed = controller.run(self.settings, self.dark, self.reference)
                    self.emit("run_finished", (status, error, completed))
                    if status == "completed" and not self.stop_event.is_set() and not self.led_connection.stop_event.is_set():
                        # Disable is already acknowledged by the controller.
                        # Keep both connections open while finalizing this run.
                        self.emit("exporting", str(self.store.directory))
                        self.store.export(excel).result()
                        self.store.close()
                        self.led_connection.release()
                        self.experiment_started = False
                        self.experiment_stop = None
                        self.emit("exported", str(self.store.directory))
                        self.emit("run_ready", None)
                        continue
                    break
        except Stopped:
            pass
        except Exception as exc:
            errors.append(str(exc) or type(exc).__name__)
            self.emit("error", errors[-1])
            if self.store and not self.store.closed:
                try:
                    self.store.finish("error", errors[-1]).result(5)
                except Exception as exc:
                    errors.append(f"Failed to save error status: {exc}")
        finally:
            self.stop_event.set()
            # An experiment shuts down LED first, before spectrometer cleanup
            # and export. An idle QE-PRO disconnect leaves LED control independent.
            if self.experiment_started and self.led_connection:
                self.led_connection.halt()
                self.led_connection.join(4)
                if self.led_connection.is_alive():
                    errors.append("LED shutdown did not finish before spectrometer cleanup.")
                else:
                    errors.extend(self.led_connection.errors)
            if self.spec:
                errors.extend(self.spec.close())
            if self.store and not self.store.closed:
                try:
                    if errors:
                        self.store.finish("shutdown_error", "; ".join(errors)).result(5)
                    self.emit("exporting", str(self.store.directory))
                    self.store.export(excel).result()
                    self.emit("exported", str(self.store.directory))
                except Exception as exc:
                    errors.append(f"Combined export failed; measurements remain in the local recovery folder. {exc}")
                finally:
                    self.store.close()
            self.emit("disconnected", errors)
