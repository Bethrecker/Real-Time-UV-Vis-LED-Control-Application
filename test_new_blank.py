"""New-blank re-referencing tests; simulation only, no device libraries."""
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch
from storage import RunStore
import test_run_timing as gui_fixture


class NewBlankTests(unittest.TestCase):
    setUp = gui_fixture.LiveAbsorbanceGUITests.setUp
    tearDown = gui_fixture.LiveAbsorbanceGUITests.tearDown
    wait_for = gui_fixture.LiveAbsorbanceGUITests.wait_for

    def prepare(self):
        app = self.app
        app.variables["scans"].set("1")
        app.variables["boxcar"].set("0")
        app.connect()
        self.wait_for(lambda: app.identity is not None and not app.pending)
        app.capture("dark")
        self.wait_for(lambda: app.dark is not None and not app.pending)
        app.probe_phase.set("blank")
        app.toggle_preview()
        app.capture("reference")
        self.wait_for(lambda: app.reference is not None and not app.pending)

    def test_new_slide_replaces_reference_keeps_dark_zeros_blank_and_is_saved(self):
        self.prepare()
        app = self.app
        old_dark, old_reference = app.dark, app.reference
        app.tabs.select(1)
        # Simulated attenuation stands in for a different blank slide.
        app.probe_phase.set("sample")
        app.toggle_preview()
        self.wait_for(lambda: app.abs_preview_line is not None and max(app.abs_preview_line.get_ydata()) > 0.14)
        app.connect_led()
        self.wait_for(lambda: app.led_session.ready.is_set() and not app.led_pending)
        adapter = app.led_session.led.adapter
        previous_offs = sum(name == "off" for name, stamp in adapter.calls)
        app.background_button.invoke()
        self.wait_for(lambda: not app.pending and app.reference is not old_reference)
        self.wait_for(lambda: app.abs_preview_line is not None and max(abs(v) for v in app.abs_preview_line.get_ydata()) < 1e-9)
        self.assertIs(app.dark, old_dark)
        self.assertNotEqual(app.reference.spectrum.intensities, old_reference.spectrum.intensities)
        self.assertEqual(app.reference.provenance["reference_role"], "new_blank_slide")
        self.assertFalse(adapter.enabled)
        self.assertGreater(sum(name == "off" for name, stamp in adapter.calls), previous_offs)
        self.assertFalse(any(name == "on" for name, stamp in adapter.calls))
        self.assertEqual(app.tabs.index(app.tabs.select()), 1)
        # A different subsequent sample is not forced to zero.
        app.probe_phase.set("blank")
        app.toggle_preview()
        self.wait_for(lambda: min(app.abs_preview_line.get_ydata()) < -0.14)
        self.assertFalse(app.running)
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as folder:
            store = RunStore(folder, "new_blank", app.settings(), app.dark, app.reference, {"simulation": True})
            try:
                saved = json.loads((store.directory / "reference.json").read_text())
                self.assertEqual(saved["provenance"]["reference_role"], "new_blank_slide")
                self.assertEqual(saved["spectrum"]["finished_utc"], app.reference.spectrum.finished_utc)
                self.assertEqual(saved["scans"], [list(scan) for scan in app.reference.scans])
            finally:
                store.close()

    def test_blocked_new_blank_is_rejected_and_never_forced_to_zero(self):
        self.prepare()
        app = self.app
        old = app.reference
        app.probe_phase.set("blocked")
        app.toggle_preview()
        app.background_button.invoke()
        self.wait_for(lambda: not app.pending and app.reference is not old)
        self.assertIn("New blank unusable", app.warning.get())
        self.assertIsNone(app.abs_preview_line)
        self.assertEqual(str(app.start_button["state"]), "disabled")
        self.assertEqual(app.reference.provenance["reference_role"], "new_blank_slide")

    def test_new_blank_gates_missing_dark_changed_settings_and_active_operation(self):
        app = self.app
        self.assertEqual(str(app.background_button["state"]), "disabled")
        self.prepare()
        self.assertEqual(str(app.background_button["state"]), "normal")
        app.variables["integration"].set("20")
        self.assertEqual(str(app.background_button["state"]), "disabled")
        with patch.object(app, "capture") as capture:
            app.background_blank()
            capture.assert_not_called()
            self.errors.assert_called_once()
            app.variables["integration"].set("10")
            for field in ["running", "led_testing", "pending"]:
                setattr(app, field, True)
                app.update()
                self.assertEqual(str(app.background_button["state"]), "disabled")
                app.background_blank()
                capture.assert_not_called()
                setattr(app, field, False)
            app.update()


if __name__ == "__main__":
    unittest.main()
