"""QE-PRO buffer-query regression tests using SDK-shaped fakes only."""
from dataclasses import replace
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from acquisition import FreshOceanSpectrometer, acquire_capture
from models import TimingBudget
from spectrometer import Identity, OceanDirectAdapter, Settings


class VendorError(Exception):
    def __init__(self, code):
        super().__init__("Command not supported" if code == 24 else "Injected SDK error")
        self.code = code
    def get_error_details(self):
        return self.code, str(self)


class BufferPolicyTests(unittest.TestCase):
    def adapter(self, enable=None, capacity=1, elements=1, error=24):
        advanced = SimpleNamespace(
            get_data_buffer_enable=Mock(return_value=enable, side_effect=VendorError(error) if enable is None else None),
            get_data_buffer_capacity=Mock(return_value=capacity),
            get_data_buffer_number_of_elements=Mock(return_value=elements))
        adapter = FreshOceanSpectrometer()
        adapter.identity = Identity("QEP06321", "QE-PRO", (1, 30, 4), 10000, 1600000000, 1, 200000)
        from test_spectrum_update import VendorDevice
        adapter.device = VendorDevice()
        adapter.device.Advanced = advanced
        adapter.device.get_formatted_spectrum = Mock(return_value=[2000, 2100])
        adapter.wavelengths = (200, 201)
        adapter.settings = Settings(10000, 1, 0)
        return adapter

    def connect(self, adapter):
        with patch.object(OceanDirectAdapter, "connect", return_value=adapter.identity):
            return adapter.connect()

    def test_actual_qepro_error_24_uses_supported_single_slot_queries(self):
        adapter = self.adapter()
        identity = self.connect(adapter)
        policy = identity.buffer_policy
        self.assertEqual(policy["mode"], "qepro_single_slot")
        self.assertEqual(policy["capacity"], 1)
        self.assertEqual(policy["stored_at_query"], 1)
        self.assertEqual(policy["enable_flag"], "unsupported_command_24")
        self.assertFalse(policy["freshness_verified"])
        self.assertEqual(adapter.single(), (2000, 2100))
        adapter.device.Advanced.get_data_buffer_capacity.assert_called()
        # Unsupported enable query is not retried before every source read.
        adapter.device.Advanced.get_data_buffer_enable.assert_called_once()
        self.assertEqual(adapter.device.Advanced.get_data_buffer_capacity.call_count, 2)

    def test_successful_disabled_query_needs_no_fallback(self):
        adapter = self.adapter(enable=False)
        self.assertEqual(self.connect(adapter).buffer_policy["mode"], "disabled_enable_flag")
        adapter.device.Advanced.get_data_buffer_capacity.assert_not_called()
        adapter.single()
        self.assertEqual(adapter.device.Advanced.get_data_buffer_enable.call_count, 2)

    def test_enabled_flag_still_blocks_connection_and_closes(self):
        adapter = self.adapter(enable=True)
        with patch.object(adapter, "close", return_value=[]) as close:
            with self.assertRaisesRegex(RuntimeError, "buffering is enabled"):
                self.connect(adapter)
            close.assert_called_once()

    def test_nonunsupported_errors_not_suppressed(self):
        for code in (5, 10):
            adapter = self.adapter(error=code)
            with patch.object(adapter, "close", return_value=[]) as close:
                with self.assertRaisesRegex(RuntimeError, f"get_data_buffer_enable failed.*{code}"):
                    self.connect(adapter)
                close.assert_called_once()
            adapter.device.Advanced.get_data_buffer_capacity.assert_not_called()

    def test_other_model_or_sdk_has_no_unverified_fallback(self):
        for model, version in (("HR6", (1, 30, 4)), ("QE-PRO", (2, 0, 0))):
            adapter = self.adapter()
            adapter.identity = replace(adapter.identity, model=model, api_version=version)
            with patch.object(adapter, "close", return_value=[]):
                with self.assertRaisesRegex(RuntimeError, "get_data_buffer_enable failed"):
                    self.connect(adapter)
            adapter.device.Advanced.get_data_buffer_capacity.assert_not_called()

    def test_multislot_unknown_or_inconsistent_buffers_are_rejected(self):
        for capacity, elements in ((2, 1), (0, 0), (1, 2), (1, -1), (True, 1), (1, None)):
            adapter = self.adapter(capacity=capacity, elements=elements)
            with patch.object(adapter, "close", return_value=[]):
                with self.assertRaisesRegex(RuntimeError, "requires a single-slot buffer"):
                    self.connect(adapter)
            adapter.device.get_formatted_spectrum.assert_not_called()

    def test_capacity_query_errors_name_the_call_and_close(self):
        adapter = self.adapter()
        adapter.device.Advanced.get_data_buffer_capacity.side_effect = VendorError(24)
        with patch.object(adapter, "close", return_value=[]) as close:
            with self.assertRaisesRegex(RuntimeError, "get_data_buffer_capacity failed"):
                self.connect(adapter)
            close.assert_called_once()

    def test_changed_capacity_stops_before_read(self):
        adapter = self.adapter()
        self.connect(adapter)
        adapter.device.Advanced.get_data_buffer_capacity.return_value = 2
        with self.assertRaisesRegex(RuntimeError, "requires a single-slot buffer"):
            adapter.single()
        adapter.device.get_formatted_spectrum.assert_not_called()

    def test_capture_preserves_per_read_buffer_evidence_and_uncertainty(self):
        adapter = self.adapter()
        self.connect(adapter)
        capture = acquire_capture(adapter, TimingBudget(), threading.Event())
        evidence = capture.provenance["scan_calls"][0]["buffer_policy"]
        self.assertEqual(evidence["mode"], "qepro_single_slot")
        self.assertEqual(evidence["capacity"], 1)
        self.assertFalse(capture.provenance["freshness_verified"])
        self.assertFalse(capture.provenance["optical_timing_verified"])
        self.assertIn("buffer_policy_at_capture_end", capture.provenance)


if __name__ == "__main__":
    unittest.main()
