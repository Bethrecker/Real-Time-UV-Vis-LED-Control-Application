"""Cloud-copy latency and local durability regressions; no hardware."""
from concurrent.futures import Future
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from acquisition import SimulationSpectrometer
from control import ExperimentController
from led import LEDService, SimulationLED
from models import RunSettings, TimingOverrun
from processing import process
from spectrometer import Settings
from storage import RunStore, is_box_output
from test_application import capture
from test_safety import MemoryStore


ROOT = Path(__file__).resolve().parent


def full_capture(counts, settings):
    c = capture(counts, settings)
    values = (counts,) * 1044
    return replace(c, spectrum=replace(c.spectrum, wavelengths=tuple(200+i*0.5 for i in range(1044)), intensities=values), scans=(values,)*settings.scans)


def done():
    result = Future()
    result.set_result(None)
    return result


class CacheTests(unittest.TestCase):
    def test_only_box_mount_uses_cache_automatically(self):
        self.assertTrue(is_box_output(Path.home()/"Box"/"ZAP Group Box"/"UV-Vis"))
        self.assertFalse(is_box_output(Path.home()/"Documents"/"UV-Vis"))
        self.assertFalse(is_box_output(Path.home()/"Box-other"/"UV-Vis"))

    def test_ten_cycles_complete_while_box_copy_is_blocked_then_files_match(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            base = Path(temp)
            output = base/"output"
            output.mkdir()
            gate, entered = threading.Event(), threading.Event()
            original_open = Path.open
            def blocked_open(path, mode="r", *args, **kwargs):
                if path.is_relative_to(output) and mode == "wb":
                    entered.set()
                    if not gate.wait(10):
                        raise OSError("test copy gate did not release")
                return original_open(path, mode, *args, **kwargs)
            settings = RunSettings(cycles=10, acquisition=Settings(25000, 3, 3))
            dark, reference, sample = (full_capture(v, settings.acquisition) for v in (1500,10000,5750))
            clock = [10.0]
            stop = threading.Event()
            pulses = []
            class LED:
                enabled = False
                def submit(self, operation, *args):
                    if operation == "off":
                        clock[0] += 0.048
                        self.enabled = False
                    return done()
                def arm(self):
                    pass
                def on(self):
                    clock[0] += 0.095
                    self.enabled = True
                    pulses.append(clock[0])
                    return done()
                def stop(self):
                    return self.submit("off")
            class Source:
                identity = SimulationSpectrometer().identity
                def capture(self, budget, *args):
                    clock[0] += budget.collection_window(settings.acquisition)
                    return sample
            class Controller(ExperimentController):
                def wait_until(self, target):
                    clock[0] = max(clock[0], target)
            with patch("storage.is_box_output", return_value=True), patch("storage.DEFAULT_RUN_CACHE", base/"cache"), patch.object(Path, "open", blocked_open):
                store = RunStore(output, "latency", settings, dark, reference, {})
                try:
                    self.assertTrue(entered.wait(2))
                    led = LED()
                    result = Controller(led, Source(), store, stop, lambda *args: None, now=lambda: clock[0]).run(settings, dark, reference)
                    self.assertEqual(result, ("completed", None, 10))
                    self.assertEqual(len(pulses), 10)
                    self.assertFalse(led.enabled)
                    self.assertEqual(len(list(store.working_directory.glob("cycle_*.json"))), 11)
                    self.assertEqual(list(store.directory.glob("cycle_*.json")), [])
                    self.assertEqual(json.loads((store.working_directory/"manifest.json").read_text())["status"], "completed")
                    gate.set()
                    store.export(True).result(10)
                    for file in store.working_directory.iterdir():
                        if file.is_file():
                            self.assertEqual(file.read_bytes(), (store.directory/file.name).read_bytes())
                    self.assertTrue((store.directory/"combined.xlsx").is_file())
                    self.assertEqual(json.loads((store.working_directory/"manifest.json").read_text())["storage"]["copy_status"],"complete")
                finally:
                    gate.set()
                    store.close()

    def test_copy_failure_signals_stop_and_preserves_local_recovery(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            base = Path(temp)
            output = base/"output"
            output.mkdir()
            gate, entered, halted = threading.Event(), threading.Event(), threading.Event()
            original_open = Path.open
            def failed_open(path, mode="r", *args, **kwargs):
                if path.is_relative_to(output) and mode == "wb":
                    entered.set()
                    gate.wait(5)
                    raise OSError("injected Box unavailable")
                return original_open(path, mode, *args, **kwargs)
            settings = RunSettings(acquisition=Settings(10000,1,0))
            dark, reference, sample = (capture(v, settings.acquisition) for v in (1500,10000,5750))
            with patch("storage.is_box_output", return_value=True), patch("storage.DEFAULT_RUN_CACHE", base/"cache"), patch.object(Path, "open", failed_open):
                store = RunStore(output, "failure", settings, dark, reference, {}, on_error=lambda message: halted.set())
                try:
                    self.assertTrue(entered.wait(2))
                    store.save_cycle(1,sample,process(sample,dark,reference,200000),{}).result(1)
                    gate.set()
                    self.assertTrue(halted.wait(2))
                    with self.assertRaisesRegex(OSError,"Box unavailable"):
                        store.check_health()
                    with self.assertRaisesRegex(OSError,"Local run data remains"):
                        store.save_cycle(2,sample,process(sample,dark,reference,200000),{}).result(1)
                    store.finish("stopped", "copy failure").result(1)
                    with self.assertRaisesRegex(OSError,"Box copying failed"):
                        store.export(False).result(3)
                    self.assertTrue((store.working_directory/"cycle_000001.json").is_file())
                    self.assertTrue((store.working_directory/"combined_absorbance.csv").is_file())
                    self.assertFalse((store.working_directory/"cycle_000002.json").exists())
                    final=json.loads((store.working_directory/"manifest.json").read_text())
                    self.assertEqual(final["status"],"stopped")
                    self.assertEqual(final["storage"]["copy_status"],"error")
                finally:
                    gate.set()
                    store.close()

    def test_copy_backlog_is_bounded_and_reports_recovery_path(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            base=Path(temp)
            output=base/"output"
            output.mkdir()
            gate,entered=threading.Event(),threading.Event()
            original_open=Path.open
            def blocked_open(path, mode="r", *args, **kwargs):
                if path.is_relative_to(output) and mode=="wb":
                    entered.set()
                    gate.wait(10)
                return original_open(path,mode,*args,**kwargs)
            settings=RunSettings(acquisition=Settings(10000,1,0))
            dark,reference,sample=(capture(v,settings.acquisition) for v in (1500,10000,5750))
            with patch("storage.is_box_output",return_value=True),patch("storage.DEFAULT_RUN_CACHE",base/"cache"),patch.object(Path,"open",blocked_open):
                store=RunStore(output,"backlog",settings,dark,reference,{})
                try:
                    self.assertTrue(entered.wait(2))
                    for cycle in range(1,16):
                        store.save_cycle(cycle,sample,process(sample,dark,reference,200000),{}).result(1)
                    with self.assertRaisesRegex(OSError,"fell behind by 16 batches"):
                        store.save_cycle(16,sample,process(sample,dark,reference,200000),{}).result(1)
                    self.assertEqual(len(store.mirror_futures),16)
                    self.assertTrue((store.working_directory/"cycle_000016.json").exists())
                finally:
                    gate.set()
                    store.close()

    def test_cached_save_timeout_explains_local_cache_and_cleanup_count(self):
        store = MemoryStore()
        store.cache_enabled = True
        store.working_directory = "private-recovery-cache"
        class TimedOutSave:
            def result(self, timeout):
                raise TimeoutError()
        controller = ExperimentController(None, None, store, threading.Event(), lambda *args: None, now=lambda: 10)
        with self.assertRaises(TimingOverrun) as raised:
            controller.wait_for_save(TimedOutSave(), 10.343, 33)
        message = str(raised.exception)
        self.assertIn("343.0 ms", message)
        self.assertIn("already uses the local recovery cache", message)
        self.assertIn("displayed completed count excludes it", message)
        self.assertIn("private-recovery-cache", message)

    def test_local_save_timeout_is_specific_and_prevents_next_pulse(self):
        class SlowStore(MemoryStore):
            def save_cycle(self,*args):
                self.records.append(args)
                return Future()
        store=SlowStore()
        adapter=SimulationLED()
        led=LEDService(adapter)
        settings=RunSettings(on_s=0.02,off_s=0.5,cycles=2,acquisition=Settings(10000,1,0))
        class Source:
            identity=SimulationSpectrometer().identity
            def capture(self,*args):
                return capture(5750,settings.acquisition)
        try:
            result=ExperimentController(led,Source(),store,threading.Event(),lambda *args:None).run(settings,capture(1500),capture(10000))
            self.assertEqual(result[0],"error")
            self.assertIn("Cycle 0 durable file save",result[1])
            self.assertIn("Recovery location",result[1])
            self.assertEqual(sum(name=="on" for name,stamp in adapter.calls),0)
            self.assertFalse(adapter.enabled)
        finally:
            led.close()


if __name__=="__main__":
    unittest.main()
