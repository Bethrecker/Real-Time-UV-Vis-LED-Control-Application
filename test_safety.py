"""Interrupted-run persistence and controller failure-path tests, no hardware."""
from concurrent.futures import Future
import json
from pathlib import Path
import queue
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from acquisition import SimulationSpectrometer
from control import ExperimentController
from led import LEDService, SimulationLED
from models import RunSettings
from session import Session
from led_session import LEDConnection
from spectrometer import Settings
from test_application import capture


ROOT = Path(__file__).resolve().parent


class MemoryStore:
    def __init__(self, fail=False):
        self.fail = fail
        self.records = []
        self.baseline = None
    def save_cycle(self, *args):
        if args[0] == 0:
            self.baseline = args
        else:
            self.records.append(args)
        result = Future()
        if self.fail:
            result.set_exception(OSError("injected disk-full failure"))
        else:
            result.set_result(None)
        return result
    def finish(self, status, error=None):
        self.status, self.error = status, error
        result = Future();result.set_result(None);return result


class FailureTests(unittest.TestCase):
    def test_storage_failure_prevents_next_pulse(self):
        adapter = SimulationLED()
        led = LEDService(adapter)
        store, stop = MemoryStore(fail=True), threading.Event()
        class Source:
            identity = SimulationSpectrometer().identity
            def capture(self, *args):
                return capture(5750)
        settings = RunSettings(on_s=0.02, off_s=0.5, cycles=2, acquisition=Settings(10000, 1, 0))
        try:
            result = ExperimentController(led, Source(), store, stop, lambda *args: None).run(settings, capture(1500), capture(10000))
            self.assertEqual(result[0], "error")
            self.assertIn("disk-full", result[1])
            self.assertEqual(sum(event == "on" for event, stamp in adapter.calls), 0)
            self.assertFalse(adapter.enabled)
        finally:
            led.close()

    def test_off_failure_is_a_shutdown_error_and_inhibits_pulses(self):
        class FailingLED(SimulationLED):
            def off(self):
                if self.enabled:
                    raise RuntimeError("injected Disable failure")
                super().off()
        adapter = FailingLED()
        led = LEDService(adapter)
        store, stop = MemoryStore(), threading.Event()
        class Source:
            identity = SimulationSpectrometer().identity
            def capture(self, *args):
                return capture(5750)
        settings = RunSettings(on_s=0.02, off_s=0.5, cycles=2, acquisition=Settings(10000, 1, 0))
        result = ExperimentController(led, Source(), store, stop, lambda *args: None).run(settings, capture(1500), capture(10000))
        self.assertEqual(result[0], "shutdown_error")
        self.assertIn("LED-off failure", result[1])
        self.assertTrue(led.inhibit.is_set())
        self.assertEqual(sum(event == "on" for event, stamp in adapter.calls), 1)
        self.assertTrue(led.close())

    def test_interrupted_session_preserves_first_cycle_and_exports(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as folder, patch("storage.DEFAULT_RUN_CACHE", Path(folder)/"recovery"):
            events = queue.Queue()
            settings = RunSettings(on_s=0.04, off_s=0.7, cycles=5, acquisition=Settings(10000, 1, 0))
            connection = LEDConnection(True, events)
            connection.start()
            self.assertTrue(connection.ready.wait(2))
            self.addCleanup(connection.join, 5)
            self.addCleanup(connection.halt)
            session = Session(True, ROOT, "unused", None, settings, events, led_connection=connection)
            session.start()
            def await_event(wanted):
                until = time.monotonic()+10
                while time.monotonic() < until:
                    kind, value = events.get(timeout=max(0.01, until-time.monotonic()))
                    if kind == "error":
                        self.fail(value)
                    if kind == wanted:
                        return value
                self.fail("Timeout: " + wanted)
            try:
                await_event("connected")
                session.commands.put(("preview", (False, "blocked")))
                session.commands.put(("dark", None));await_event("dark")
                session.commands.put(("preview", (False, "blank")))
                session.commands.put(("reference", None));await_event("reference")
                session.commands.put(("start", (folder, "interrupt", True)))
                directory = Path(await_event("run_folder"))
                await_event("cycle")
                session.halt()
                status, error, completed = await_event("run_finished")
                self.assertEqual((status, completed), ("stopped", 1))
                self.assertEqual(await_event("disconnected"), [])
                manifest = json.loads((session.store.working_directory / "manifest.json").read_text())
                self.assertEqual(manifest["status"], "stopped")
                self.assertEqual(len(list(session.store.working_directory.glob("cycle_*.json"))), 2)
                self.assertEqual([p.name for p in directory.iterdir()], ["combined_absorbance.xlsx"])
                self.assertFalse(session.led.adapter.enabled)
            finally:
                session.halt();session.join(5)


if __name__ == "__main__":
    unittest.main()
