"""Regression checks for the recorded LED-command abort and live absorbance."""
from bootstrap import configure
configure()
from dataclasses import replace
from concurrent.futures import Future
from pathlib import Path
import math
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from acquisition import SimulationSpectrometer, acquire_capture
from control import ExperimentController, validate_observed_read_budget
from models import RunSettings, TimingBudget
from processing import process
from spectrometer import Settings
from test_application import capture
from test_safety import MemoryStore


def done(value=None):
    f = Future()
    f.set_result(value)
    return f


class RecordedCommandTimingTests(unittest.TestCase):
    def run_delayed(self, on_delay=0.0946395, cycles=10, read_overrun=False):
        clock = [50.0]
        stop = threading.Event()
        settings = RunSettings(on_s=1, off_s=1, cycles=cycles, acquisition=Settings(25000, 3, 3))
        calls = []
        class LED:
            enabled = False
            def submit(self, operation, *args):
                if operation == "off":
                    clock[0] += 0.0477957
                    self.enabled = False
                calls.append((operation, clock[0], args))
                return done()
            def arm(self):
                pass
            def on(self):
                clock[0] += on_delay
                self.enabled = True
                calls.append(("on_ack", clock[0], ()))
                return done()
            def stop(self):
                return self.submit("off")
        class Source:
            identity = SimulationSpectrometer().identity
            def capture(self, budget, phase, cycle, deadline, interrupt):
                clock[0] += budget.collection_window(settings.acquisition) + (1 if read_overrun and cycle > 0 else 0)
                return capture(5750, settings.acquisition)
        class Controller(ExperimentController):
            def wait_until(self, target):
                clock[0] = max(clock[0], target)
        led, store, events = LED(), MemoryStore(), []
        controller = Controller(led, Source(), store, stop, lambda *args: events.append(args), now=lambda: clock[0])
        result = controller.run(settings, capture(1500, settings.acquisition), capture(10000, settings.acquisition))
        return result, calls, store, events, led, settings

    def test_recorded_95ms_on_and_48ms_off_complete_all_ten_cycles(self):
        result, calls, store, events, led, settings = self.run_delayed()
        self.assertEqual(result, ("completed", None, 10))
        self.assertEqual(sum(name == "on_ack" for name, _, _ in calls), 10)
        self.assertEqual(len(store.records), 10)
        self.assertFalse(led.enabled)
        for cycle, sample, processed, timing in store.records:
            self.assertAlmostEqual(timing["led_on_command_elapsed_s"], 0.0946395)
            self.assertGreaterEqual(timing["host_on_end_target_monotonic_s"]-timing["host_on_ack_monotonic_s"], 1)
            self.assertAlmostEqual(timing["off_end_target_monotonic_s"]-timing["off_start_host_ack_monotonic_s"], 1)
            midpoint = (timing["collection_target_start_monotonic_s"]+timing["collection_target_end_monotonic_s"])/2
            self.assertAlmostEqual(midpoint, timing["off_start_host_ack_monotonic_s"]+0.5)
            self.assertAlmostEqual(processed.absorbance[50], math.log10(2))
            self.assertFalse(timing["overrun"])
        records = store.records
        for previous, following in zip(records, records[1:]):
            self.assertGreaterEqual(following[3]["host_schedule_start_monotonic_s"], previous[3]["off_end_target_monotonic_s"])

    def test_command_budget_still_aborts_before_acquisition_or_second_pulse(self):
        result, calls, store, events, led, settings = self.run_delayed(on_delay=0.8)
        self.assertEqual(result[0], "error")
        self.assertIn("800.0 ms", result[1])
        self.assertEqual(sum(name == "on_ack" for name, _, _ in calls), 1)
        self.assertEqual(store.records, [])
        self.assertIsNotNone(store.baseline)
        self.assertFalse(led.enabled)

    def test_read_overrun_preserves_first_result_and_prevents_second_pulse(self):
        result, calls, store, events, led, settings = self.run_delayed(read_overrun=True)
        self.assertEqual(result[0], "error")
        self.assertIn("off-period deadline", result[1])
        self.assertEqual(len(store.records), 1)
        self.assertTrue(store.records[0][3]["overrun"])
        self.assertEqual(sum(name == "on_ack" for name, _, _ in calls), 1)
        self.assertFalse(led.enabled)

    def test_idle_read_can_finish_but_known_slow_read_blocks_run_before_pulse(self):
        adapter = SimulationSpectrometer()
        adapter.apply(Settings(10000, 1, 0))
        original = adapter.single
        def slow():
            time.sleep(0.06)
            return original()
        adapter.single = slow
        source = acquire_capture(adapter, TimingBudget(), threading.Event(), enforce_timing=False)
        self.assertFalse(source.provenance["experiment_read_budget_enforced"])
        with self.assertRaisesRegex(ValueError, "No experiment pulse was issued"):
            validate_observed_read_budget(RunSettings(acquisition=adapter.settings), source)


class LiveAbsorbanceGUITests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        from gui import Application
        self.root = tk.Tk()
        self.root.withdraw()
        from test_support import make_application
        self.app = make_application(self.root)
        self.dialog = patch("gui.messagebox.showerror")
        self.errors = self.dialog.start()

    def tearDown(self):
        if self.app.session:
            self.app.session.halt()
            self.app.session.join(5)
        if self.app.led_session:
            self.app.led_session.halt()
            self.app.led_session.join(5)
        self.app.export_worker.shutdown()
        self.app.destroy_window()
        self.dialog.stop()

    def wait_for(self, predicate, seconds=10):
        until = time.monotonic()+seconds
        while time.monotonic() < until:
            self.root.update()
            if predicate():
                return
            time.sleep(0.01)
        self.fail("GUI transition timed out: " + self.app.warning.get())

    def test_live_sample_absorbance_without_led_or_experiment_and_stale_reset(self):
        app = self.app
        app.variables["scans"].set("1")
        app.variables["boxcar"].set("0")
        app.connect()
        self.wait_for(lambda: app.identity is not None and not app.pending)
        app.tabs.select(1)
        app.capture("dark")
        self.wait_for(lambda: app.dark is not None and not app.pending)
        self.assertIsNone(app.abs_preview_line)
        app.probe_phase.set("blank")
        app.toggle_preview()
        app.capture("reference")
        self.wait_for(lambda: app.reference is not None and not app.pending)
        self.wait_for(lambda: app.abs_preview_line is not None)
        self.assertAlmostEqual(max(app.abs_preview_line.get_ydata()), 0, places=6)
        app.probe_phase.set("sample")
        app.toggle_preview()
        self.wait_for(lambda: max(app.abs_preview_line.get_ydata()) > 0.14)
        self.assertEqual(len(app.abs_axes.lines), 1)
        self.assertEqual(app.abs_lines, [])
        self.assertIsNone(app.led_session)
        self.assertFalse(app.running)
        self.assertIn("not saved", app.abs_status.get())
        app.variables["integration"].set("20")
        self.assertIsNone(app.abs_preview_line)
        self.assertEqual(str(app.start_button["state"]), "disabled")

    def test_run_failure_remains_visible_after_export_and_disconnect(self):
        app = self.app
        app.run_path = "saved-run"
        message = "TimingOverrun: recorded LED command failure"
        app.handle("run_finished", ("error", message, 0))
        app.handle("exported", "saved-run")
        app.handle("disconnected", [])
        self.assertIn("Run error; 0 completed cycles", app.status.get())
        self.assertIn(message, app.warning.get())
        self.assertIn(message, app.abs_status.get())
        self.errors.assert_called_once()

    def test_time_zero_trace_does_not_count_as_an_irradiation_cycle(self):
        app = self.app
        sample = capture(5750)
        result = process(sample, capture(1500), capture(10000), 200000)
        app.handle("baseline", (0, sample, result, {"irradiation_elapsed_s": 0}))
        self.assertEqual(app.completed, 0)
        self.assertEqual(float(app.progress["value"]), 0)
        self.assertEqual(len(app.abs_lines), 1)
        self.assertEqual(app.abs_lines[0].get_label(), "Time 0 (no irradiation)")
        self.assertIn("Time 0 (no irradiation)", app.abs_status.get())
        app.handle("cycle", (1, sample, result, {}))
        self.assertEqual(app.completed, 1)
        self.assertEqual(len(app.abs_lines), 2)

    def test_background_replacement_discards_inflight_absorbance(self):
        app = self.app
        settings = app.preview_settings()
        app.applied = settings
        app.identity = SimulationSpectrometer().identity
        app.dark = capture(1500, settings.acquisition)
        app.reference = capture(10000, settings.acquisition)
        sample = capture(5750, settings.acquisition)
        result = process(sample, app.dark, app.reference, app.identity.maximum_intensity)
        old = app.reference
        app.reference = capture(12000, settings.acquisition)
        app.handle("absorbance_preview", (sample, result, app.dark, old))
        self.assertIsNone(app.abs_preview_line)

    def test_controls_share_top_right_frame_and_duration_includes_command_allowance(self):
        app = self.app
        self.assertIs(app.start_button.master, app.experiment_controls)
        self.assertIs(app.stop_button.master, app.experiment_controls)
        self.assertEqual(app.experiment_controls.pack_info()["side"], "right")
        self.assertIs(app.experiment_controls.master, app.led_connect_button.master)
        self.assertGreater(app.settings().estimated_duration_s, app.settings().duration_s)
        self.assertIn("command allowances", app.summary.get())
        app.requested_cycles = 10
        app.handle("phase", (1, "LED command", 20))
        self.wait_for(lambda: "Cycle 1/10; completed 0" in app.progress_text.get())


if __name__ == "__main__":
    unittest.main()
