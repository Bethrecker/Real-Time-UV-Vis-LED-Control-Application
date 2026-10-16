"""Time-zero sample must be acquired and saved before any irradiation."""
from concurrent.futures import Future
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from openpyxl import load_workbook
from acquisition import SimulationSpectrometer
from control import ExperimentController
from led import LEDService, SimulationLED
from models import RunSettings
from spectrometer import Settings
from storage import RunStore, recover_absorbance_excel
from test_application import capture
from test_safety import MemoryStore


ROOT = Path(__file__).resolve().parent


class TimeZeroTests(unittest.TestCase):
    def setUp(self):
        self.settings = RunSettings(on_s=0.02, off_s=0.5, cycles=1, acquisition=Settings(10000, 1, 0))
        self.dark, self.reference = capture(1500), capture(10000)
        self.adapter = SimulationLED()
        self.led = LEDService(self.adapter)
        self.addCleanup(self.led.close)
        self.stop = threading.Event()

    def run_controller(self, source, store, emit=lambda *args: None):
        return ExperimentController(self.led, source, store, self.stop, emit).run(self.settings, self.dark, self.reference)

    def test_baseline_saved_while_inhibited_before_first_pulse(self):
        events = []
        test = self
        class Source:
            identity = SimulationSpectrometer().identity
            def capture(self, budget, phase, cycle, deadline, interrupt):
                events.append(("capture", cycle))
                test.assertFalse(test.adapter.enabled)
                if cycle == 0:
                    test.assertTrue(test.led.inhibit.is_set())
                return capture(5750 if cycle == 0 else 4000)
        class Store(MemoryStore):
            def save_cycle(self, *args):
                events.append(("save", args[0]))
                if args[0] == 0:
                    test.assertTrue(test.led.inhibit.is_set())
                return super().save_cycle(*args)
        original_on = self.adapter.on
        def on(inhibit):
            events.append(("pulse", 1))
            original_on(inhibit)
        self.adapter.on = on
        store = Store()
        self.assertEqual(self.run_controller(Source(), store), ("completed", None, 1))
        self.assertEqual(events, [("capture", 0), ("save", 0), ("pulse", 1), ("capture", 1), ("save", 1)])
        self.assertEqual(store.baseline[3]["irradiation_elapsed_s"], 0)
        self.assertNotEqual(store.baseline[2].absorbance, store.records[0][2].absorbance)

    def test_pending_baseline_save_keeps_pulses_inhibited(self):
        entered = threading.Event()
        saved = Future()
        class Store(MemoryStore):
            def save_cycle(self, *args):
                if args[0] == 0:
                    entered.set()
                    return saved
                return super().save_cycle(*args)
        class Source:
            identity = SimulationSpectrometer().identity
            def capture(self, *args):
                return capture(5750)
        outcome = []
        thread = threading.Thread(target=lambda: outcome.append(self.run_controller(Source(), Store())))
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            self.assertTrue(self.led.inhibit.is_set())
            self.assertFalse(any(name == "on" for name, _ in self.adapter.calls))
            self.stop.set()
            saved.set_result(None)
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(outcome[0], ("stopped", None, 0))
            self.assertFalse(any(name == "on" for name, _ in self.adapter.calls))
        finally:
            self.stop.set()
            if not saved.done():
                saved.set_result(None)
            thread.join(3)

    def test_baseline_read_failure_issues_no_pulse(self):
        class Source:
            identity = SimulationSpectrometer().identity
            def capture(self, *args):
                raise RuntimeError("baseline read failed")
        result = self.run_controller(Source(), MemoryStore())
        self.assertEqual(result[0], "error")
        self.assertIn("baseline read failed", result[1])
        self.assertFalse(any(name == "on" for name, _ in self.adapter.calls))

    def test_invalid_baseline_is_preserved_and_issues_no_pulse(self):
        class Source:
            identity = SimulationSpectrometer().identity
            def capture(self, *args):
                return capture(1500)
        store = MemoryStore()
        result = self.run_controller(Source(), store)
        self.assertEqual(result[0], "error")
        self.assertIn("No valid time-zero", result[1])
        self.assertIsNotNone(store.baseline)
        self.assertEqual(store.records, [])
        self.assertFalse(any(name == "on" for name, _ in self.adapter.calls))

    def test_workbook_and_recovery_include_time_zero_before_cycles(self):
        class Source:
            identity = SimulationSpectrometer().identity
            def capture(self, *args):
                return capture(5750)
        with tempfile.TemporaryDirectory(dir=ROOT) as temp, patch("storage.DEFAULT_RUN_CACHE", Path(temp)/"cache"):
            store = RunStore(temp, "baseline", self.settings, self.dark, self.reference, {}, combined_excel_only=True)
            try:
                self.assertEqual(self.run_controller(Source(), store), ("completed", None, 1))
                store.export().result(5)
                manifest = json.loads((store.working_directory/"manifest.json").read_text())
                self.assertEqual(manifest["completed_cycles"], 1)
                self.assertTrue((store.working_directory/"cycle_000000.json").exists())
                for path in (store.directory/"combined_absorbance.xlsx", Path(recover_absorbance_excel(store.directory))):
                    book = load_workbook(path, read_only=True)
                    try:
                        self.assertEqual(next(book["Absorbance"].values), ("wavelength_nm", "time_0_no_irradiation_absorbance_AU", "cycle_000001_absorbance_AU"))
                    finally:
                        book.close()
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
