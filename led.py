"""Mightex x64 cdecl bindings, finite pulse, and independent off-request path."""
from concurrent.futures import ThreadPoolExecutor, Future
import ctypes as ct
from dataclasses import dataclass
import os
import math
from pathlib import Path
import threading
import time
from acquisition import utc

DEFAULT_MIGHTEX = Path(__file__).resolve().parent.parent / "mightex-files" / "SDK" / "LIB" / "x64"
MIGHTEX_SERIAL = "04-210326-002"


def validate_test_duration(seconds):
    """Bound manual tests and retain the documented 20 us pulse resolution."""
    if not math.isfinite(seconds) or not 0.02 <= seconds <= 30:
        raise ValueError("LED test duration must be from 0.02 to 30 seconds.")
    microseconds = round(seconds * 1e6)
    if microseconds % 20 or not math.isclose(seconds, microseconds / 1e6, abs_tol=1e-9, rel_tol=0):
        raise ValueError("LED test duration must use 20 microsecond steps.")
    return seconds


@dataclass(frozen=True)
class OutputApproval:
    channel: int
    level_tenths_percent: int
    confirmed: bool

    def validate(self):
        if not self.confirmed:
            raise ValueError("Confirm channel wiring, safe output level, and absence of an external trigger source.")
        if type(self.channel) is not int or not 1 <= self.channel <= 4:
            raise ValueError("BLS-IO04 channel must be entered explicitly (1Ã¢â‚¬â€œ4).")
        if type(self.level_tenths_percent) is not int or not 1 <= self.level_tenths_percent <= 1000:
            raise ValueError("Enter approved output level: 1Ã¢â‚¬â€œ1000 SDK units (0.1%Ã¢â‚¬â€œ100.0% of configured channel maximum voltage).")


class MightexAdapter:
    def __init__(self, approval=None, directory=DEFAULT_MIGHTEX):
        if approval is not None:
            approval.validate()
        self.approval, self.directory = approval, Path(directory)
        self.dll = self.handle = self.search_path = None
        self.channels = 0

    def _call(self, name, *args):
        result = getattr(self.dll, "MTUSB_BLSDriver" + name)(*args)
        # SDK manual: setters/SoftStart fail for -1 (invalid) or 1 (device error).
        if result in (-1, 1):
            raise RuntimeError(f"Mightex {name} failed, SDK return {result}; arguments={args}.")
        return result

    def connect(self):
        if ct.sizeof(ct.c_void_p) != 8:
            raise RuntimeError("Mightex x64 SDK requires 64-bit Python.")
        for filename in ["Mightex_BLSDriver_SDK.dll", "HIDDLL.dll"]:
            p = self.directory / filename
            with p.open("rb") as stream:
                stream.seek(0x3C)
                offset = int.from_bytes(stream.read(4), "little")
                stream.seek(offset)
                if stream.read(6) != b"PE\0\0\x64\x86":
                    raise RuntimeError(f"Not an AMD64 PE library: {p}")
        self.search_path = os.add_dll_directory(str(self.directory.resolve()))
        self.dll = ct.CDLL(str((self.directory / "Mightex_BLSDriver_SDK.dll").resolve()))
        declarations = {"InitDevices": [], "OpenDevice": [ct.c_int], "CloseDevice": [ct.c_int],
                        "GetSerialNo": [ct.c_int, ct.c_void_p, ct.c_int], "GetModuleType": [ct.c_int],
                        "GetChannels": [ct.c_int], "SetMode": [ct.c_int]*3,
                        "SetPulseProfile": [ct.c_int]*5, "SetPulseDetail": [ct.c_int]*9,
                        "SoftStart": [ct.c_int]*2}
        for name, args in declarations.items():
            fun = getattr(self.dll, "MTUSB_BLSDriver" + name)
            fun.argtypes, fun.restype = args, ct.c_int
        count = self.dll.MTUSB_BLSDriverInitDevices()
        if count <= 0:
            raise RuntimeError(f"Mightex USB SDK found no devices (return {count}).")
        found = []
        for index in range(count):
            handle = self.dll.MTUSB_BLSDriverOpenDevice(index)
            if handle < 0:
                continue
            buffer = ct.create_string_buffer(64)
            result = self.dll.MTUSB_BLSDriverGetSerialNo(handle, buffer, len(buffer))
            if result == -1:
                self.dll.MTUSB_BLSDriverCloseDevice(handle)
                raise RuntimeError("Mightex serial query failed.")
            serial = buffer.value.decode("ascii")
            found.append(serial)
            if serial == MIGHTEX_SERIAL:
                self.handle = handle
                break
            if self.dll.MTUSB_BLSDriverCloseDevice(handle) == -1:
                raise RuntimeError("Could not close an unrelated Mightex device.")
        if self.handle is None:
            raise RuntimeError(f"Mightex target {MIGHTEX_SERIAL} not found; discovered {found}.")
        if self.dll.MTUSB_BLSDriverGetModuleType(self.handle) != 14:
            raise RuntimeError("Target is not the documented IO_MODULE (type 14).")
        channels = self.dll.MTUSB_BLSDriverGetChannels(self.handle)
        if channels != 4 or (self.approval is not None and self.approval.channel > channels):
            raise RuntimeError(f"Unexpected Mightex channel count: {channels}.")
        self.channels = channels
        self.off()
        selected = f"channel {self.approval.channel}" if self.approval else "all 4 outputs disabled; no channel selected"
        return f"BLS-IO04 {MIGHTEX_SERIAL}, {selected}; Disable acknowledged"

    def configure(self, approval):
        approval.validate()
        if self.handle is None or self.channels != 4:
            raise RuntimeError("Connect the Mightex controller before enabling output.")
        self.off()
        self.approval = approval

    def prepare(self, on_s):
        if self.approval is None:
            raise ValueError("Enter channel and approved level before enabling LED output.")
        self.approval.validate()
        self.off()
        duration = round(on_s * 1e6)
        if duration <= 0 or duration % 20 or duration > 2147483647:
            raise ValueError("Pulse duration must be positive, in 20 Ã‚Âµs steps and within SDK int range.")
        h, ch = self.handle, self.approval.channel
        # One pulse and repeat count 1 (= no repetition, user manual p17).
        # User manual p19: every stage is at least 20 us. Both outer stages
        # hold zero output; the middle stage is the requested on duration.
        self._call("SetPulseProfile", h, ch, 0, 1, 1)
        self._call("SetPulseDetail", h, ch, 0, 20, duration, 20, 0, self.approval.level_tenths_percent, 0)

    def on(self, inhibit):
        if self.approval is None or self.handle is None:
            raise ValueError("Connect LED and enter channel/output level before enabling it.")
        if inhibit.is_set():
            raise RuntimeError("Further pulses are inhibited.")
        self._call("SetMode", self.handle, self.approval.channel, 3)
        if inhibit.is_set():
            self.off()
            raise RuntimeError("Pulse cancelled before SoftStart.")
        self._call("SoftStart", self.handle, self.approval.channel)

    def off(self):
        if self.handle is not None:
            channels = [self.approval.channel] if self.approval else range(1, self.channels + 1)
            if self.approval is None and self.channels != 4:
                raise RuntimeError("Channel count is not verified; LED Disable cannot be confirmed.")
            errors = []
            for channel in channels:
                try:
                    self._call("SetMode", self.handle, channel, 0)
                except Exception as exc:
                    errors.append(f"Channel {channel}: {exc}")
            if errors:
                raise RuntimeError("; ".join(errors))

    def close(self):
        # Never close before explicitly requesting Disable, even after an error.
        errors = []
        try:
            self.off()
        except Exception as exc:
            errors.append(f"LED Disable failed: {exc}")
        if self.handle is not None:
            if self.dll.MTUSB_BLSDriverCloseDevice(self.handle) == -1:
                errors.append("Mightex CloseDevice failed.")
            self.handle = None
        if self.search_path:
            self.search_path.close()
            self.search_path = None
        return errors


class SimulationLED:
    def __init__(self):
        self.enabled = False
        self.calls = []

    def connect(self):
        self.off()
        return "SIMULATED Mightex / irradiation output off"

    def configure(self, approval):
        self.off()
        self.approval = approval

    def prepare(self, on_s):
        self.off()

    def on(self, inhibit):
        if inhibit.is_set():
            raise RuntimeError("Pulses inhibited")
        self.enabled = True
        self.calls.append(("on", time.monotonic()))

    def off(self):
        self.enabled = False
        self.calls.append(("off", time.monotonic()))

    def close(self):
        self.off()
        return []


class LEDService:
    """Serialized LED calls independent of the spectrometer process/controller.

    Stop first sets inhibit, then queues Disable. Pending on commands check
    inhibit on execution. Calls already inside the Mightex DLL cannot be safely
    interrupted; their timeout is reported and the finite pulse is a backstop.
    """
    def __init__(self, adapter, log=lambda record: None):
        self.adapter, self.log = adapter, log
        self.inhibit = threading.Event()
        self.inhibit.set()
        self.closed = False
        self.close_errors = []
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="LED safety")

    def submit(self, operation, *args):
        def invoke():
            start = time.monotonic()
            stamp = utc()
            self.log({"event": "led_"+operation+"_attempt", "host_call_start_utc": stamp, "host_call_start_monotonic_s": start, "provenance": "host request; acknowledgement/optical state not yet known"})
            try:
                result = getattr(self.adapter, operation)(*args)
                self.log({"event": "led_"+operation, "host_call_start_utc": stamp, "host_call_end_utc": utc(),
                          "host_call_start_monotonic_s": start, "host_call_end_monotonic_s": time.monotonic(),
                          "provenance": "host SDK call; optical state/timing not measured", "success": True})
                return result
            except Exception as exc:
                self.log({"event": "led_"+operation, "host_call_start_utc": stamp, "host_call_end_utc": utc(), "success": False, "error": str(exc)})
                raise
        return self.executor.submit(invoke)

    def arm(self):
        self.inhibit.clear()

    def on(self):
        return self.submit("on", self.inhibit)

    def stop(self):
        self.inhibit.set()
        try:
            if not self.closed:
                return self.submit("off")
        except RuntimeError:
            if not self.closed:
                raise
        # Stop remains idempotent while the session exports already-closed data.
        result = Future()
        if self.close_errors:
            result.set_exception(RuntimeError("; ".join(self.close_errors)))
        else:
            result.set_result(None)
        return result

    def close(self, timeout=2):
        if self.closed:
            return list(self.close_errors)
        self.inhibit.set()
        try:
            self.close_errors = self.submit("close").result(timeout)
        except Exception as exc:
            self.close_errors = [f"LED shutdown not confirmed: {exc or type(exc).__name__}"]
        finally:
            self.closed = True
            self.executor.shutdown(wait=False)
        return list(self.close_errors)
