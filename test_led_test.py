"""Bounded manual LED tests: simulated adapters only, no hardware commands."""
from pathlib import Path
import queue
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from led import LEDService, SimulationLED, validate_test_duration
from models import RunSettings
from led_session import LEDConnection


ROOT = Path(__file__).resolve().parent


class LEDTestTests(unittest.TestCase):
    def make_session(self, adapter=None):
        session = LEDConnection(True, queue.Queue())
        session.led = LEDService(adapter or SimulationLED())
        session.ready.set()
        self.addCleanup(session.led.close)
        return session

    def wait_for(self, predicate, seconds=2):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.005)
        self.fail("LED test state transition timed out")

    def finish(self, session):
        session.test_thread.join(2)
        self.assertFalse(session.test_thread.is_alive())
        self.assertFalse(session.test_active)
        self.assertTrue(session.led.inhibit.is_set())

    def test_process_cancel_reply_is_normal_stop(self):
        from acquisition import SpectrometerClient
        from models import Stopped
        client = object.__new__(SpectrometerClient)
        client.conn = SimpleNamespace(poll=lambda timeout: True, recv=lambda: (False, "Stopped: "))
        with self.assertRaises(Stopped):
            client.receive(1)

    def test_duration_bounds_and_sdk_resolution(self):
        for value in (0.02, 1, 30):
            self.assertEqual(validate_test_duration(value), value)
        for value in (0, -1, 31, float("nan"), float("inf"), 0.020001):
            with self.assertRaises(ValueError):
                validate_test_duration(value)

    def test_timed_test_disables_and_invalidates_backgrounds(self):
        session = self.make_session()
        session.test_led(0.04)
        self.finish(session)
        self.assertFalse(session.led.adapter.enabled)
        calls = [operation for operation, stamp in session.led.adapter.calls]
        self.assertEqual(calls.count("on"), 1)
        self.assertEqual(calls[-1], "off")
        self.assertIn(("led_test_finished", None), list(session.events.queue))

    def test_off_ends_test_early_without_disconnecting(self):
        session = self.make_session()
        session.test_led(30)
        self.wait_for(lambda: session.led.adapter.enabled)
        session.stop_led_test().result(0.5)
        self.finish(session)
        self.assertFalse(session.led.adapter.enabled)
        self.assertFalse(session.stop_event.is_set())
        session.test_led(0.02)
        self.finish(session)
        self.assertEqual(sum(op == "on" for op, stamp in session.led.adapter.calls), 2)

    def test_off_during_prepare_prevents_later_pulse(self):
        preparing, release = threading.Event(), threading.Event()
        class SlowPrepare(SimulationLED):
            def prepare(self, seconds):
                preparing.set()
                release.wait(0.4)
                super().prepare(seconds)
        session = self.make_session(SlowPrepare())
        session.test_led(1)
        self.assertTrue(preparing.wait(0.5))
        future = session.stop_led_test()
        release.set()
        future.result(0.5)
        self.finish(session)
        self.assertFalse(any(op == "on" for op, stamp in session.led.adapter.calls))

    def test_off_independent_of_blocked_spectrometer(self):
        session = self.make_session()
        blocked, release = threading.Event(), threading.Event()
        def capture():
            blocked.set()
            release.wait(2)
        preview = threading.Thread(target=capture)
        preview.start()
        try:
            self.assertTrue(blocked.wait(0.5))
            session.test_led(30)
            self.wait_for(lambda: session.led.adapter.enabled)
            session.stop_led_test().result(0.5)
            self.finish(session)
            self.assertTrue(preview.is_alive())
            self.assertFalse(session.led.adapter.enabled)
        finally:
            release.set()
            preview.join(1)

    def test_tests_refused_during_capture_experiment_shutdown_or_another_test(self):
        session = self.make_session()
        for attribute in ("setup_busy", "reserved"):
            setattr(session, attribute, True)
            with self.assertRaises(ValueError):
                session.test_led(1)
            setattr(session, attribute, False)
        session.test_led(30)
        with self.assertRaises(ValueError):
            session.test_led(1)
        session.halt()
        self.finish(session)
        with self.assertRaises(ValueError):
            session.test_led(1)
        self.assertFalse(session.led.adapter.enabled)

    def test_disable_failure_is_visible_and_requests_shutdown(self):
        class FailingOff(SimulationLED):
            def off(self):
                if self.enabled:
                    raise RuntimeError("injected Disable failure")
                super().off()
        session = self.make_session(FailingOff())
        session.test_led(0.02)
        self.finish(session)
        self.assertTrue(session.stop_event.is_set())
        finished = [value for kind, value in list(session.events.queue) if kind == "led_test_finished"]
        self.assertIn("LED OFF NOT CONFIRMED", finished[0])

    def test_gui_test_gates_start_and_backgrounds_and_keeps_connection(self):
        import tkinter as tk
        from gui import Application
        root = tk.Tk()
        root.withdraw()
        from test_support import make_application
        app = make_application(root)
        dialog = patch("gui.messagebox.showerror")
        errors = dialog.start()
        self.addCleanup(dialog.stop)
        def wait_for(predicate, seconds=10):
            deadline = time.monotonic()+seconds
            while time.monotonic() < deadline:
                root.update()
                if predicate():
                    return
                time.sleep(0.01)
            self.fail("GUI transition timed out: " + app.warning.get())
        try:
            self.assertEqual(str(app.led_test_button["state"]), "disabled")
            app.connect()
            wait_for(lambda: app.identity is not None and not app.pending)
            app.connect_led()
            wait_for(lambda: app.led_session.ready.is_set() and not app.led_pending)
            app.capture("dark")
            wait_for(lambda: app.dark is not None and not app.pending)
            app.probe_phase.set("blank")
            app.toggle_preview()
            app.capture("reference")
            wait_for(lambda: app.reference is not None and not app.pending)
            self.assertEqual(str(app.start_button["state"]), "normal")
            app.variables["test_seconds"].set("30")
            app.test_led()
            self.assertTrue(app.led_testing)
            self.assertIsNone(app.dark)
            self.assertIsNone(app.reference)
            for button in (app.start_button, app.dark_button, app.reference_button, app.led_test_button):
                self.assertEqual(str(button["state"]), "disabled")
            self.assertEqual(str(app.led_test_off_button["state"]), "normal")
            self.assertEqual(str(app.preview_button["state"]), "normal")
            wait_for(lambda: app.led_session.led.adapter.enabled)
            app.stop_led_test()
            wait_for(lambda: not app.led_testing)
            self.assertIsNotNone(app.session)
            self.assertFalse(app.led_session.led.adapter.enabled)
            self.assertEqual(str(app.start_button["state"]), "disabled")
            self.assertEqual(str(app.dark_button["state"]), "normal")
            app.stop()
            wait_for(lambda: app.session is None and app.led_session is None)
            self.assertFalse(errors.called, errors.call_args_list)
        finally:
            if app.session:
                app.session.halt()
                app.session.join(5)
            if app.led_session:
                app.led_session.halt()
                app.led_session.join(5)
            app.export_worker.shutdown()
            app.destroy_window()


if __name__ == "__main__":
    unittest.main()
