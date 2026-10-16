"""Independent LED connection, bounded manual tests, and experiment ownership."""
import threading
from led import LEDService, MightexAdapter, SimulationLED, validate_test_duration
from models import Stopped


class LEDConnection(threading.Thread):
    def __init__(self, simulation, events, timeout=0.5):
        super().__init__(name="LED connection", daemon=True)
        self.simulation, self.events, self.timeout = simulation, events, timeout
        self.led = None
        self.ready = threading.Event()
        self.stop_event = threading.Event()
        self.test_lock = threading.Lock()
        self.test_cancel = threading.Event()
        self.test_active = self.setup_busy = self.reserved = False
        self.test_thread = None
        self.approval = None
        self.experiment_stop = None
        self.errors = []
        self.log = lambda record: self.emit("device_event", record)

    def emit(self, kind, value):
        self.events.put((kind, value))

    def stop_led_test(self, event="led_test_off"):
        with self.test_lock:
            self.test_cancel.set()
            if self.reserved and self.experiment_stop:
                self.experiment_stop.set()
            future = self.led.stop() if self.led else None
        if future:
            def acknowledge(result):
                exc = result.exception()
                self.emit(event, f"LED Disable failed: {exc}" if exc else "LED Disable acknowledged (optical state not measured).")
            future.add_done_callback(acknowledge)
        return future

    def halt(self):
        self.stop_event.set()
        self.stop_led_test("led_off")

    def validate_approval(self, approval):
        if not self.simulation:
            if approval is None:
                raise ValueError("Enter channel and approved output level before enabling the LED.")
            approval.validate()

    def test_led(self, seconds, approval=None):
        validate_test_duration(seconds)
        self.validate_approval(approval)
        with self.test_lock:
            if not self.ready.is_set() or self.stop_event.is_set() or self.reserved or self.setup_busy:
                raise ValueError("Connect an idle LED controller before testing; tests are unavailable during background capture, experiments or shutdown.")
            if self.test_active:
                raise ValueError("An LED test is already active.")
            self.test_active = True
            self.test_cancel.clear()
            self.test_thread = threading.Thread(target=self._test_led, args=(seconds, approval), name="Bounded LED test", daemon=True)
            self.test_thread.start()

    def _test_led(self, seconds, approval):
        error = None
        try:
            self.led.submit("configure", approval).result(self.timeout)
            self.approval = approval
            self.led.submit("prepare", seconds).result(self.timeout)
            with self.test_lock:
                if self.test_cancel.is_set() or self.stop_event.is_set():
                    raise Stopped()
                self.led.arm()
                pulse = self.led.on()
            pulse.result(self.timeout)
            self.emit("led_test_started", seconds)
            self.test_cancel.wait(seconds)
        except Stopped:
            pass
        except Exception as exc:
            error = f"LED test failed: {exc or type(exc).__name__}"
        finally:
            try:
                self.stop_led_test().result(self.timeout)
            except Exception as exc:
                error = (error + "; " if error else "") + f"LED OFF NOT CONFIRMED: {exc or type(exc).__name__}"
            if error:
                self.errors.append(error)
                self.halt()
            with self.test_lock:
                self.test_active = False
            self.emit("led_test_finished", error)
            if error:
                self.emit("led_error", error)

    def begin_background(self):
        with self.test_lock:
            if not self.ready.is_set() or self.stop_event.is_set() or self.test_active or self.reserved:
                raise ValueError("LED controller must be idle for background capture.")
            self.setup_busy = True
        try:
            self.led.stop().result(self.timeout)
        except Exception:
            self.end_background()
            self.halt()
            raise

    def end_background(self):
        with self.test_lock:
            self.setup_busy = False

    def reserve(self, approval, stop):
        self.validate_approval(approval)
        with self.test_lock:
            if not self.ready.is_set() or self.stop_event.is_set() or self.test_active or self.setup_busy or self.reserved:
                raise ValueError("Connect an idle LED controller before starting the experiment.")
            self.reserved = True
            self.experiment_stop = stop
        try:
            self.led.submit("configure", approval).result(self.timeout)
            self.approval = approval
            return self.led
        except Exception:
            self.halt()
            raise

    def release(self):
        """Return a completed experiment's connection to idle with output off."""
        self.led.stop().result(self.timeout)
        with self.test_lock:
            if self.stop_event.is_set():
                raise Stopped()
            self.reserved = False
            self.experiment_stop = None
            self.log = lambda record: self.emit("device_event", record)

    def run(self):
        try:
            adapter = SimulationLED() if self.simulation else MightexAdapter()
            self.led = LEDService(adapter, lambda record: self.log(record))
            info = self.led.submit("connect").result(10)
            if self.stop_event.is_set():
                raise Stopped()
            self.ready.set()
            self.emit("led_connected", info)
            self.stop_event.wait()
        except Stopped:
            pass
        except Exception as exc:
            self.errors.append(str(exc) or type(exc).__name__)
            self.emit("led_error", self.errors[-1])
        finally:
            self.halt()
            self.ready.clear()
            if self.test_thread:
                self.test_thread.join(2 * self.timeout + 0.2)
                if self.test_thread.is_alive():
                    self.errors.append("LED test cleanup did not finish before device shutdown.")
            if self.led:
                try:
                    self.led.stop().result(self.timeout)
                except Exception as exc:
                    self.errors.append(f"LED OFF NOT CONFIRMED: {exc or type(exc).__name__}")
                self.errors.extend(self.led.close())
            self.emit("led_disconnected", list(self.errors))
