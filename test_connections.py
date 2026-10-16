"""Independent device connection regression tests. Real mode uses fakes only."""
from dataclasses import replace
import queue
import ctypes
import threading
import time
import unittest
from unittest.mock import patch
from acquisition import SimulationSpectrometer
from led import MightexAdapter, SimulationLED, OutputApproval
from models import Stopped
from session import Session
from test_application import capture
from test_support import SilentSound


class FakeSpectrometer:
    def __init__(self, *args):
        self.identity = replace(SimulationSpectrometer().identity, serial="QEP06321", model="QE-PRO")
        self.cancel = threading.Event()
    def apply(self, settings):
        self.settings = settings
    def configure_preview(self, processing):
        self.preview_processing = processing
        return {"sdk_scans_to_average": self.settings.scans, "sdk_boxcar_width": self.settings.boxcar,
                "electric_dark": processing.electric_dark, "nonlinearity": processing.nonlinearity}
    def capture(self, budget, phase="blank", **kwargs):
        if self.cancel.is_set():
            raise Stopped()
        return capture(1500 if phase == "blocked" else 10000, self.settings, "QEP06321")
    def close(self):
        return []


class IndependentConnectionsTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        from gui import Application
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = Application(self.root, sound_factory=SilentSound)
        self.app.preview_enabled.set(False)
        self.app.update()
        self.spec_patch = patch("session.SpectrometerClient", side_effect=FakeSpectrometer)
        self.spec_factory = self.spec_patch.start()
        self.led_patch = patch("led_session.MightexAdapter", side_effect=SimulationLED)
        self.led_factory = self.led_patch.start()
        self.dialog_patch = patch("gui.messagebox.showerror")
        self.dialogs = self.dialog_patch.start()

    def tearDown(self):
        if self.app.led_session:
            self.app.led_session.halt()
            self.app.led_session.join(5)
        if self.app.session:
            self.app.session.halt()
            self.app.session.join(5)
        self.app.export_worker.shutdown()
        self.app.destroy_window()
        self.dialog_patch.stop()
        self.led_patch.stop()
        self.spec_patch.stop()

    def wait_for(self, predicate):
        until = time.monotonic() + 5
        while time.monotonic() < until:
            self.root.update()
            if predicate():
                return
            time.sleep(0.005)
        self.fail("GUI transition timed out: " + self.app.warning.get())

    def connect_spec(self):
        self.app.connect()
        self.wait_for(lambda: self.app.identity is not None and not self.app.pending)

    def connect_led(self):
        self.app.connect_led()
        self.wait_for(lambda: self.app.led_session is not None and self.app.led_session.ready.is_set() and not self.app.led_pending)

    def approve(self, channel="2", level="10"):
        self.app.variables["channel"].set(channel)
        self.app.variables["level"].set(level)
        self.app.confirmed.set(True)
        self.app.update()

    def test_qe_first_with_blank_led_inputs_never_accesses_mightex(self):
        self.assertEqual(str(self.app.connect_button["state"]), "normal")
        self.connect_spec()
        self.led_factory.assert_not_called()
        self.assertIsNone(self.app.led_session)
        self.assertEqual(self.app.identity.serial, "QEP06321")
        self.app.capture("dark")
        self.wait_for(lambda: self.app.dark is not None and not self.app.pending)
        self.assertEqual(str(self.app.start_button["state"]), "disabled")

    def test_led_connects_without_settings_and_tests_without_qe(self):
        self.connect_led()
        self.spec_factory.assert_not_called()
        self.assertIsNone(self.app.identity)
        self.assertEqual(str(self.app.led_test_button["state"]), "disabled")
        self.approve()
        self.app.variables["test_seconds"].set("30")
        self.app.test_led()
        self.wait_for(lambda: self.app.led_session.led.adapter.enabled)
        self.assertEqual(self.app.led_session.led.adapter.approval, OutputApproval(2, 100, True))
        self.app.stop_led_test()
        self.wait_for(lambda: not self.app.led_testing)
        self.assertFalse(self.app.led_session.led.adapter.enabled)
        self.assertIsNone(self.app.session)
        self.assertEqual(str(self.app.start_button["state"]), "disabled")

    def test_led_inputs_can_change_while_connected_and_idle(self):
        self.connect_led()
        self.approve()
        self.app.variables["test_seconds"].set("0.02")
        self.app.test_led()
        self.wait_for(lambda: not self.app.led_testing)
        self.approve("3", "35")
        self.assertEqual(str(dict(self.app.input_widgets)["level"]["state"]), "normal")
        self.app.test_led()
        self.wait_for(lambda: not self.app.led_testing)
        self.assertEqual(self.app.led_session.led.adapter.approval, OutputApproval(3, 350, True))

    def test_missing_qe_does_not_close_connected_led(self):
        self.connect_led()
        connection = self.app.led_session
        self.spec_factory.side_effect = RuntimeError("injected missing QE-PRO")
        self.app.connect()
        self.wait_for(lambda: self.app.session is None)
        self.assertIs(self.app.led_session, connection)
        self.assertTrue(connection.ready.is_set())
        self.approve()
        self.app.variables["test_seconds"].set("0.02")
        self.app.test_led()
        self.wait_for(lambda: not self.app.led_testing)
        self.assertTrue(connection.ready.is_set())

    def test_qe_timeout_does_not_block_independent_led_connection(self):
        self.spec_factory.side_effect = RuntimeError("Spectrometer request timed out. LED control remains independent; no more pulses are allowed.")
        self.app.connect()
        self.wait_for(lambda: self.app.session is None)
        self.assertFalse(self.app.failed_shutdown)
        self.assertEqual(str(self.app.led_connect_button["state"]), "normal")
        self.connect_led()
        self.assertTrue(self.app.led_session.ready.is_set())

    def test_missing_led_does_not_close_qe_or_prevent_retry(self):
        self.connect_spec()
        identity = self.app.identity
        self.led_factory.side_effect = RuntimeError("injected missing LED")
        self.app.connect_led()
        self.wait_for(lambda: self.app.led_session is None)
        self.assertIs(self.app.identity, identity)
        self.assertEqual(str(self.app.led_connect_button["state"]), "normal")
        self.assertEqual(str(self.app.dark_button["state"]), "normal")

    def test_qe_disconnect_during_led_test_keeps_led_independent(self):
        self.connect_spec()
        self.connect_led()
        self.approve()
        self.app.variables["test_seconds"].set("30")
        self.app.test_led()
        self.wait_for(lambda: self.app.led_session.led.adapter.enabled)
        connection = self.app.led_session
        self.app.disconnect_spec()
        self.wait_for(lambda: self.app.session is None)
        self.assertIs(self.app.led_session, connection)
        self.assertTrue(connection.led.adapter.enabled)
        self.app.stop_led_test()
        self.wait_for(lambda: not self.app.led_testing)
        self.assertFalse(connection.led.adapter.enabled)

    def test_led_disconnect_keeps_qe_connected(self):
        self.connect_spec()
        self.connect_led()
        session = self.app.session
        self.app.disconnect_led()
        self.wait_for(lambda: self.app.led_session is None)
        self.assertIs(self.app.session, session)
        self.assertIsNone(session.led_connection)
        self.assertIsNotNone(self.app.identity)

    def test_invalid_experiment_timing_does_not_block_device_connections(self):
        self.app.variables["on"].set("not set")
        self.app.variables["off"].set("0.001")
        self.assertEqual(str(self.app.connect_button["state"]), "normal")
        self.connect_spec()
        self.connect_led()
        self.assertEqual(str(self.app.start_button["state"]), "disabled")

    def test_stop_shuts_down_both_and_turns_led_off(self):
        self.connect_spec()
        self.connect_led()
        self.approve()
        self.app.variables["test_seconds"].set("30")
        self.app.test_led()
        self.wait_for(lambda: self.app.led_session.led.adapter.enabled)
        connection = self.app.led_session
        self.app.stop()
        self.wait_for(lambda: self.app.session is None and self.app.led_session is None)
        self.assertFalse(connection.led.adapter.enabled)
        self.assertEqual(self.dialogs.call_count, 0)


    def test_window_close_with_led_only_explicitly_turns_it_off(self):
        self.connect_led()
        self.approve()
        self.app.variables["test_seconds"].set("30")
        self.app.test_led()
        self.wait_for(lambda: self.app.led_session.led.adapter.enabled)
        connection = self.app.led_session
        with patch.object(self.app, "destroy_window") as destroy:
            self.app.close()
            self.wait_for(lambda: destroy.called)
        self.assertFalse(connection.led.adapter.enabled)
        self.assertIsNone(self.app.led_session)
        self.spec_factory.assert_not_called()

    def test_window_close_with_qe_only_never_accesses_led(self):
        self.connect_spec()
        with patch.object(self.app, "destroy_window") as destroy:
            self.app.close()
            self.wait_for(lambda: destroy.called)
        self.assertIsNone(self.app.session)
        self.led_factory.assert_not_called()


class UnconfiguredMightexTests(unittest.TestCase):
    def test_connect_without_inputs_opens_and_disables_all_outputs(self):
        calls = []
        class DLL:
            def __getattr__(self, name):
                def call(*args):
                    calls.append((name, args))
                    if name.endswith("InitDevices"):
                        return 1
                    if name.endswith("GetModuleType"):
                        return 14
                    if name.endswith("GetChannels"):
                        return 4
                    if name.endswith("GetSerialNo"):
                        serial = b"04-210326-002\0"
                        ctypes.memmove(args[1], serial, len(serial))
                    return 0
                return call
        adapter = MightexAdapter()
        with patch("led.ct.CDLL", return_value=DLL()) as load, patch("led.os.add_dll_directory"):
            info = adapter.connect()
            self.assertIn("all 4 outputs disabled", info)
            self.assertIsNone(adapter.approval)
            self.assertEqual(adapter.close(), [])
            load.assert_called_once()
        modes = [args for name, args in calls if name.endswith("SetMode")]
        self.assertEqual(modes, [(0, ch, 0) for ch in range(1, 5)] * 2)
        self.assertFalse(any(name.endswith("SoftStart") for name, args in calls))

    def test_no_inputs_disable_all_verified_channels_before_close(self):
        class DLL:
            def __init__(self):
                self.calls = []
            def MTUSB_BLSDriverSetMode(self, handle, channel, mode):
                self.calls.append(("mode", handle, channel, mode))
                return 0
            def MTUSB_BLSDriverCloseDevice(self, handle):
                self.calls.append(("close", handle))
                return 0
        adapter = MightexAdapter()
        adapter.handle, adapter.channels, adapter.dll = 0, 4, DLL()
        with self.assertRaises(ValueError):
            adapter.prepare(1)
        with self.assertRaises(ValueError):
            adapter.on(threading.Event())
        self.assertEqual(adapter.close(), [])
        self.assertEqual(adapter.dll.calls[:-1], [("mode", 0, channel, 0) for channel in range(1, 5)])
        self.assertEqual(adapter.dll.calls[-1], ("close", 0))

    def test_off_attempts_every_channel_despite_one_failure(self):
        channels = []
        class DLL:
            def MTUSB_BLSDriverSetMode(self, handle, channel, mode):
                channels.append(channel)
                return 1 if channel == 2 else 0
        adapter = MightexAdapter()
        adapter.handle, adapter.channels, adapter.dll = 0, 4, DLL()
        with self.assertRaisesRegex(RuntimeError, "Channel 2"):
            adapter.off()
        self.assertEqual(channels, [1, 2, 3, 4])


if __name__ == "__main__":
    unittest.main()
