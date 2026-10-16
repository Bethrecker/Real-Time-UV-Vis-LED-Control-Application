"""SDK setting/correction readback and documented pulse constraints."""
from dataclasses import replace
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from acquisition import FreshOceanSpectrometer, acquire_capture, acquire_preview
from led import MightexAdapter, OutputApproval
from models import PreviewProcessing, TimingBudget
from spectrometer import Identity, Settings


class VendorDevice:
    def __init__(self):
        self.values = {"integration_time": 10000, "scans_to_average": 1, "boxcar_width": 0,
                       "electric_dark_correction_usage": False, "nonlinearity_correction_usage": False}
        self.calls = []
        self.Advanced = SimpleNamespace(get_data_buffer_enable=lambda: False)
        self.result = [10, 500, 10, 20, 10]
        self.delay = 0
        self.reads = 0
    def __getattr__(self, name):
        if name.startswith("set_"):
            def setter(value):
                self.calls.append((name, value))
                self.values[name[4:]] = value
            return setter
        if name.startswith("get_"):
            return lambda: self.values[name[4:]]
        raise AttributeError(name)
    def get_formatted_spectrum(self):
        time.sleep(self.delay)
        self.reads += 1
        return self.result


class SpectrumUpdateTests(unittest.TestCase):
    def adapter(self):
        adapter = FreshOceanSpectrometer()
        adapter.identity = Identity("QEP06321", "QE-PRO", (1, 30, 4), 10000, 1600000000, 1, 200000)
        adapter.wavelengths = tuple(range(200, 205))
        adapter.device = VendorDevice()
        adapter.settings = Settings(10000, 4, 2)
        adapter.processing_mode = "raw"
        adapter.buffer_policy = {"mode": "disabled_enable_flag"}
        return adapter

    def test_preview_passes_requested_averaging_boxcar_and_corrections_to_sdk(self):
        adapter = self.adapter()
        actual = adapter.configure_preview(PreviewProcessing(True, True))
        self.assertEqual(adapter.device.values["scans_to_average"], 4)
        self.assertEqual(adapter.device.values["boxcar_width"], 2)
        self.assertEqual(actual, {"sdk_scans_to_average": 4, "sdk_boxcar_width": 2, "electric_dark": True, "nonlinearity": True})
        calls = adapter.device.calls
        self.assertLess(calls.index(("set_electric_dark_correction_usage", True)), calls.index(("set_nonlinearity_correction_usage", True)))
        before = len(calls)
        adapter.configure_preview(PreviewProcessing(True, True))
        self.assertEqual(len(calls), before)

    def test_sdk_processed_result_is_not_averaged_or_smoothed_again(self):
        adapter = self.adapter()
        result = acquire_preview(adapter, TimingBudget(), PreviewProcessing(), threading.Event())
        self.assertEqual(result.spectrum.intensities, tuple(adapter.device.result))
        self.assertEqual(adapter.device.reads, 1)
        self.assertEqual(result.provenance["sdk_scans_to_average"], 4)
        self.assertEqual(result.provenance["sdk_boxcar_width"], 2)
        self.assertFalse(result.provenance["raw_source_scans_available"])
        self.assertFalse(result.provenance["averaging_behavior_verified"])

    def test_preview_does_not_use_the_raw_experiment_read_deadline(self):
        adapter = self.adapter()
        adapter.settings = Settings(10000, 1, 0)
        adapter.device.delay = 0.04
        result = acquire_preview(adapter, TimingBudget(readout_s=0.001, uncertainty_s=0.001), PreviewProcessing(), threading.Event())
        self.assertEqual(result.spectrum.intensities, tuple(adapter.device.result))

    def test_measurements_keep_selected_sdk_settings_and_standard_corrections(self):
        adapter = self.adapter()
        adapter.configure_preview(PreviewProcessing(True, True))
        adapter.measurement_mode()
        self.assertEqual(adapter.settings, Settings(10000, 4, 2))
        self.assertEqual(adapter.device.values["scans_to_average"], 4)
        self.assertEqual(adapter.device.values["boxcar_width"], 2)
        self.assertTrue(adapter.device.values["electric_dark_correction_usage"])
        self.assertTrue(adapter.device.values["nonlinearity_correction_usage"])
        result = acquire_capture(adapter, TimingBudget(), threading.Event())
        self.assertEqual(len(result.scans), 1)
        self.assertEqual(result.spectrum.intensities, tuple(adapter.device.result))
        self.assertEqual(result.provenance["sdk_scans_to_average"], 4)
        self.assertEqual(result.provenance["sdk_boxcar_width"], 2)

    def test_mismatched_sdk_settings_or_correction_readback_is_rejected(self):
        adapter = self.adapter()
        adapter.device.get_scans_to_average = lambda: 3
        with self.assertRaisesRegex(RuntimeError, "readback differs"):
            adapter.configure_preview(PreviewProcessing())
        adapter = self.adapter()
        adapter.device.get_nonlinearity_correction_usage = lambda: False
        with self.assertRaisesRegex(RuntimeError, "correction readback differs"):
            adapter.configure_preview(PreviewProcessing(True, True))

    def test_close_restores_dark_before_saved_nonlinearity(self):
        adapter = self.adapter()
        device = adapter.device
        original_set = device.set_nonlinearity_correction_usage
        def constrained(value):
            if value and not device.values["electric_dark_correction_usage"]:
                raise RuntimeError("electric dark is required")
            original_set(value)
        device.set_nonlinearity_correction_usage = constrained
        adapter.saved = [("get_electric_dark_correction_usage", "set_electric_dark_correction_usage", True),
                         ("get_nonlinearity_correction_usage", "set_nonlinearity_correction_usage", True)]
        class API:
            instance = SimpleNamespace(open_devices={})
            def close_device(self, device_id):
                self.closed = True
            def shutdown(self):
                self.stopped = True
        adapter.api = api = API()
        self.assertEqual(adapter.close(), [])
        self.assertTrue(api.closed and api.stopped)
        self.assertTrue(device.values["electric_dark_correction_usage"])
        self.assertTrue(device.values["nonlinearity_correction_usage"])
        self.assertLess(device.calls.index(("set_electric_dark_correction_usage", True)), device.calls.index(("set_nonlinearity_correction_usage", True)))

    def test_oceanview_nonlinearity_requires_dark_and_both_flags_are_boolean(self):
        for processing in (PreviewProcessing(False, True), PreviewProcessing(1, False)):
            with self.assertRaises(ValueError):
                processing.validate()

    def test_each_pulse_segment_meets_documented_minimum_and_resolution(self):
        calls = []
        class DLL:
            def __getattr__(self, name):
                def call(*args):
                    calls.append((name, args))
                    if name.endswith("SetPulseDetail"):
                        times = args[3:6]
                        self_test = all(value >= 20 and value % 20 == 0 for value in times)
                        return 0 if self_test else -1
                    return 0
                return call
        adapter = MightexAdapter(OutputApproval(2, 100, True))
        adapter.handle, adapter.dll = 0, DLL()
        adapter.prepare(1)
        detail = next(args for name, args in calls if name.endswith("SetPulseDetail"))
        self.assertEqual(detail, (0, 2, 0, 20, 1000000, 20, 0, 100, 0))
        self.assertFalse(any(name.endswith("SoftStart") for name, args in calls))

    def test_sdk_error_includes_exact_function_and_arguments(self):
        adapter = MightexAdapter(OutputApproval(2, 100, True))
        adapter.dll = SimpleNamespace(MTUSB_BLSDriverSetPulseDetail=lambda *args: -1)
        with self.assertRaisesRegex(RuntimeError, r"SetPulseDetail.*return -1.*arguments=\(0, 2"):
            adapter._call("SetPulseDetail", 0, 2, 0, 20, 1000000, 20, 0, 100, 0)

    def test_gui_update_displays_applied_parameters_and_invalidates_backgrounds(self):
        from bootstrap import configure
        configure()
        import tkinter as tk
        from gui import Application
        root = tk.Tk()
        root.withdraw()
        from test_support import make_application
        app = make_application(root)
        def wait_for(predicate):
            until = time.monotonic() + 10
            while time.monotonic() < until:
                root.update()
                if predicate():
                    return
                time.sleep(0.01)
            self.fail("GUI transition timed out: " + app.warning.get())
        with patch("gui.messagebox.showerror") as errors:
            try:
                app.connect()
                wait_for(lambda: app.latest is not None and app.latest.provenance.get("strategy") == "sdk_processed_preview")
                old = app.latest
                app.capture("dark")
                wait_for(lambda: app.dark is not None and not app.pending)
                app.variables["scans"].set("4")
                app.variables["boxcar"].set("3")
                app.apply()
                self.assertIsNone(app.dark)
                self.assertIsNone(app.reference)
                self.assertIsNone(app.latest)
                app.handle("preview", old)
                self.assertIsNone(app.latest)
                wait_for(lambda: app.latest is not None and app.latest.provenance.get("strategy") == "sdk_processed_preview")
                self.assertEqual(app.latest.spectrum.settings, Settings(10000, 4, 3))
                self.assertEqual(app.latest.provenance["preview_processing"], {"electric_dark": True, "nonlinearity": True})
                self.assertIn("average 4; boxcar 3", app.raw_status.get())
                self.assertIsNone(app.led_session)
                app.stop()
                wait_for(lambda: app.session is None)
                self.assertFalse(errors.called, errors.call_args_list)
            finally:
                if app.session:
                    app.session.halt()
                    app.session.join(5)
                app.export_worker.shutdown()
                app.destroy_window()


if __name__ == "__main__":
    unittest.main()
