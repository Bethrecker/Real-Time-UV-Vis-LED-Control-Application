"""OceanDirect-only adapter. No light-source, GPIO, strobe or Mightex calls."""
from dataclasses import dataclass
from datetime import datetime, timezone
import importlib
import math
import os
from pathlib import Path
import struct
import sys
import time

DEFAULT_SDK = Path(r"C:\Program Files\Ocean Insight\OceanDirect SDK")
DEFAULT_SERIAL = "QEP06321"


@dataclass(frozen=True)
class Settings:
    integration_us: int = 10000
    scans: int = 1
    boxcar: int = 0


@dataclass(frozen=True)
class Identity:
    serial: str
    model: str
    api_version: tuple
    minimum_us: int
    maximum_us: int
    increment_us: int
    maximum_intensity: float
    buffer_policy: dict | None = None


@dataclass(frozen=True)
class Spectrum:
    wavelengths: tuple
    intensities: tuple
    settings: Settings
    serial: str
    started_utc: str
    finished_utc: str
    host_call_seconds: float


def validate_spectrum(wavelengths, intensities):
    if len(wavelengths) != len(intensities) or len(wavelengths) < 2:
        raise ValueError("Wavelength and intensity arrays must have matching, nonzero lengths.")
    if not all(math.isfinite(v) for v in (*wavelengths, *intensities)):
        raise ValueError("Spectrum contains nonfinite wavelengths or intensities.")
    if not all(b > a for a, b in zip(wavelengths, wavelengths[1:])):
        raise ValueError("Wavelengths are not strictly increasing.")


class OceanDirectAdapter:
    """All methods must run on one owner thread; SDK settings are restored on close.

    HR6 can later use the same interface with a different serial and compatible SDK.
    Device detection never falls back silently to another serial.
    """

    def __init__(self, sdk=DEFAULT_SDK, serial=DEFAULT_SERIAL):
        self.sdk = Path(sdk).resolve()
        self.serial = serial
        self.api = self.device = self.device_id = self.identity = None
        self.settings = None
        self.saved = []
        self.dll_directory = None

    def connect(self):
        if struct.calcsize("P") != 8:
            raise RuntimeError("OceanDirect requires this application's 64-bit Python interpreter.")
        package = self.sdk / "Python" / "oceandirect"
        dll = package / "lib" / "OceanDirect.dll"
        if not dll.is_file():
            raise FileNotFoundError(f"Matching vendor Python wrapper/DLL not found: {dll}")
        # Check PE architecture without loading a mismatched DLL.
        with dll.open("rb") as stream:
            stream.seek(0x3C)
            offset = int.from_bytes(stream.read(4), "little")
            stream.seek(offset)
            header = stream.read(6)
        if header[:4] != b"PE\0\0" or header[4:] != b"\x64\x86":
            raise RuntimeError("The vendor OceanDirect DLL is not Windows AMD64.")
        sys.path.insert(0, str(package.parent))
        self.dll_directory = os.add_dll_directory(str(dll.parent))
        # The installed vendor wrapper uses a relative DLL path. Override that
        # module variable in memory with an absolute path; vendor files stay intact.
        properties = importlib.import_module("oceandirect.sdk_properties")
        properties.oceandirect_dll = str(dll)
        wrapper = importlib.import_module("oceandirect.OceanDirectAPI")
        wrapper.oceandirect_dll = str(dll)
        if wrapper.OceanDirectAPI.instance is not None:
            raise RuntimeError("An OceanDirect session already exists in this process.")
        try:
            self.api = wrapper.OceanDirectAPI()
            version = self.api.get_api_version_numbers()
            self.api.find_usb_devices()
            ids = self.api.get_device_ids()
            discovered = []
            for device_id in ids:
                try:
                    candidate = self.api.open_device(device_id)
                    serial = candidate.get_serial_number()
                    discovered.append(serial)
                    if serial == self.serial:
                        self.device, self.device_id = candidate, device_id
                        break
                finally:
                    if device_id != self.device_id:
                        self.api.close_device(device_id)
            if self.device is None:
                raise RuntimeError(f"Target {self.serial} not discovered. Found: {discovered or 'no devices'}.")
            d = self.device
            self.identity = Identity(
                d.get_serial_number(), d.get_model(), version,
                d.get_minimum_integration_time(), d.get_maximum_integration_time(),
                d.get_integration_time_increment(), d.get_max_intensity(),
            )
            if (self.identity.minimum_us <= 0 or self.identity.maximum_us < self.identity.minimum_us
                    or not math.isfinite(self.identity.maximum_intensity) or self.identity.maximum_intensity <= 0):
                raise RuntimeError("SDK returned invalid integration limits or maximum intensity.")
            for getter, setter in [
                ("get_integration_time", "set_integration_time"),
                ("get_scans_to_average", "set_scans_to_average"),
                ("get_boxcar_width", "set_boxcar_width"),
                ("get_electric_dark_correction_usage", "set_electric_dark_correction_usage"),
                ("get_nonlinearity_correction_usage", "set_nonlinearity_correction_usage"),
            ]:
                self.saved.append((getter, setter, getattr(d, getter)()))
            mode = d.get_trigger_mode()
            if mode != 0:
                raise RuntimeError(f"Trigger mode is {mode}, not the tested free-running mode 0. No trigger mode was changed.")
            self.wavelengths = tuple(d.get_wavelengths())
            # The base adapter starts with pixel-aligned baseline counts.
            # The experiment adapter's apply() selects its standard SDK
            # corrections before acquiring any preview/background/sample.
            d.set_nonlinearity_correction_usage(False)
            d.set_electric_dark_correction_usage(False)
            self.apply(Settings())
            return self.identity
        except Exception as exc:
            errors = self.close()
            if errors:
                raise RuntimeError(f"Connection failed: {exc}; cleanup also failed: " + "; ".join(errors)) from exc
            raise

    def apply(self, settings):
        i = self.identity
        if any(type(v) is not int for v in (settings.integration_us, settings.scans, settings.boxcar)):
            raise ValueError("Acquisition settings must be integers in SDK units.")
        if not i.minimum_us <= settings.integration_us <= min(i.maximum_us, 5_000_000):
            raise ValueError(f"Preview integration must be {i.minimum_us / 1000:g}–{min(i.maximum_us, 5_000_000) / 1000:g} ms.")
        if (settings.integration_us - i.minimum_us) % max(1, i.increment_us):
            raise ValueError(f"Integration increment is {i.increment_us} microseconds.")
        if not 1 <= settings.scans <= 100 or not 0 <= settings.boxcar <= 50:
            raise ValueError("Preview limits: scans 1–100; boxcar half-width 0–50.")
        if settings.integration_us * settings.scans > 5_000_000:
            raise ValueError("Preview integration × scans must be at most 5 seconds (an estimate, not verified acquisition timing).")
        self.device.set_scans_to_average(settings.scans)
        self.device.set_boxcar_width(settings.boxcar)
        self.device.set_integration_time(settings.integration_us)
        actual = Settings(self.device.get_integration_time(), self.device.get_scans_to_average(), self.device.get_boxcar_width())
        if actual != settings:
            raise RuntimeError(f"SDK setting readback differs: requested {settings}, returned {actual}.")
        self.settings = actual
        return actual

    def acquire(self):
        start = datetime.now(timezone.utc).isoformat()
        clock = time.perf_counter()
        counts = tuple(self.device.get_formatted_spectrum())
        duration = time.perf_counter() - clock
        end = datetime.now(timezone.utc).isoformat()
        validate_spectrum(self.wavelengths, counts)
        return Spectrum(self.wavelengths, counts, self.settings, self.serial, start, end, duration)

    def close(self):
        errors = []
        if self.device is not None:
            restore = list(reversed(self.saved))
            # Restore electric dark before enabling saved nonlinearity, which
            # OceanView documents as dependent on electric dark correction.
            dark = next((entry for entry in restore if entry[1] == "set_electric_dark_correction_usage"), None)
            nonlinear = next((entry for entry in restore if entry[1] == "set_nonlinearity_correction_usage"), None)
            if dark and nonlinear and nonlinear[2]:
                restore.remove(dark)
                restore.insert(restore.index(nonlinear), dark)
            for getter, setter, value in restore:
                try:
                    getattr(self.device, setter)(value)
                    if getattr(self.device, getter)() != value:
                        raise RuntimeError("restored value did not read back")
                except Exception as exc:
                    errors.append(f"Restore {setter}: {exc}")
            try:
                self.api.close_device(self.device_id)
            except Exception as exc:
                errors.append(f"Close device: {exc}")
        if self.api is not None:
            try:
                self.api.shutdown()
            except Exception as exc:
                errors.append(f"SDK shutdown: {exc}")
            # Vendor singleton survives shutdown. Clear it only after explicit
            # cleanup, without letting its destructor issue a second shutdown.
            singleton = self.api.instance
            singleton.open_devices.clear()
            singleton.oceandirect = _ClosedLibrary()
            type(self.api).instance = None
        if self.dll_directory is not None:
            self.dll_directory.close()
            self.dll_directory = None
        self.api = self.device = self.device_id = None
        self.saved.clear()
        return errors


class _ClosedLibrary:
    def odapi_shutdown(self):
        pass
