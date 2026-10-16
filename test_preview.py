import math
import queue
import unittest
from spectrometer import Identity, OceanDirectAdapter, Settings, Spectrum, validate_spectrum
from reference import absorbance, check_reference
from worker import PreviewWorker


def spectrum(counts, settings=Settings(), serial="QEP06321"):
    return Spectrum(tuple(range(200, 200 + len(counts))), tuple(counts), settings, serial, "start", "end", 0.01)


class ReferenceTests(unittest.TestCase):
    def setUp(self):
        self.dark = spectrum([1500] * 100)
        self.blank = spectrum([10000] * 100)

    def test_blocked_reference_refused(self):
        self.assertFalse(check_reference(self.dark, spectrum([1501] * 100), 200000).usable)
        with self.assertRaises(ValueError):
            absorbance(self.blank, self.dark, self.dark, 200000)

    def test_dark_corrected_absorbance(self):
        sample = spectrum([5750] * 100)
        result = absorbance(sample, self.dark, self.blank, 200000)
        self.assertAlmostEqual(result[0], math.log10(2))

    def test_saturation_in_any_reference_pixel_refuses_capture(self):
        clipped = spectrum([200000] + [10000] * 99)
        self.assertFalse(check_reference(self.dark, clipped, 200000).usable)
        with self.assertRaises(ValueError):
            absorbance(clipped, self.dark, self.blank, 200000)

    def test_weak_pixels_and_nonpositive_sample_masked(self):
        blank = spectrum([1500] + [10000] * 99)
        sample = spectrum([5750, 1500] + [5750] * 98)
        result = absorbance(sample, self.dark, blank, 200000)
        self.assertIsNone(result[0])
        self.assertIsNone(result[1])
        self.assertTrue(math.isfinite(result[2]))

    def test_mismatched_calibration_refused(self):
        for bad in [spectrum([10000] * 100, Settings(20000)), spectrum([10000] * 100, serial="HR600429"), spectrum([10000] * 99)]:
            self.assertFalse(check_reference(self.dark, bad, 200000).usable)

    def test_nonfinite_and_array_length_errors(self):
        for waves, values in [([1, 2], [1]), ([1, 2], [1, math.nan]), ([2, 1], [1, 2])]:
            with self.assertRaises(ValueError):
                validate_spectrum(waves, values)


class FakeDevice:
    def __init__(self, mismatch=False):
        self.values = {"integration_time": 100000, "scans_to_average": 0, "boxcar_width": 0}
        self.mismatch = mismatch

    def __getattr__(self, name):
        if name.startswith("set_"):
            return lambda value: self.values.__setitem__(name[4:], value)
        if name.startswith("get_"):
            return lambda: self.values[name[4:]] + (1 if self.mismatch and name == "get_boxcar_width" else 0)
        raise AttributeError(name)


class AdapterTests(unittest.TestCase):
    def adapter(self, mismatch=False):
        adapter = OceanDirectAdapter()
        adapter.identity = Identity("QEP06321", "QE-PRO", (1, 30, 4), 10000, 1600000000, 1, 200000)
        adapter.device = FakeDevice(mismatch)
        return adapter

    def test_invalid_settings_never_reach_device(self):
        a = self.adapter()
        for value in [Settings(9999), Settings(10000, 0), Settings(10000, 1, -1), Settings(5000000, 2)]:
            with self.assertRaises(ValueError):
                a.apply(value)
        self.assertEqual(a.device.values["integration_time"], 100000)

    def test_readback_mismatch_is_error(self):
        with self.assertRaises(RuntimeError):
            self.adapter(True).apply(Settings())

    def test_restore_attempts_all_settings_despite_failure(self):
        a = self.adapter()
        def fail(value):
            raise RuntimeError("injected restoration failure")
        a.device.set_boxcar_width = fail
        a.saved = [("get_integration_time", "set_integration_time", 50000), ("get_boxcar_width", "set_boxcar_width", 2)]
        class API:
            instance = None
            def close_device(self, did):
                self.closed = True
            def shutdown(self):
                self.stopped = True
        class Instance:
            open_devices = {}
        a.api = api = API()
        api.instance = Instance()
        errors = a.close()
        self.assertEqual(len(errors), 1)
        self.assertTrue(api.closed and api.stopped)
        self.assertIsNone(a.device)


class WorkerTests(unittest.TestCase):
    def test_acquisition_error_always_closes_and_reports(self):
        events = queue.Queue()
        class FailingAdapter:
            settings = Settings()
            closed = False
            def __init__(self, *args):
                pass
            def connect(self):
                return "fake identity"
            def acquire(self):
                raise RuntimeError("injected acquisition failure")
            def close(self):
                FailingAdapter.closed = True
                return ["injected cleanup failure"]
        worker = PreviewWorker("fake", "fake", events, FailingAdapter)
        worker.start()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertTrue(FailingAdapter.closed)
        messages = []
        while not events.empty():
            messages.append(events.get()[0])
        self.assertEqual(messages, ["connected", "error", "cleanup_error", "stopped"])


if __name__ == "__main__":
    unittest.main()
