"""Experiment settings, estimated timing budget, and capture records."""
from dataclasses import dataclass, asdict
import math
from spectrometer import Settings, Spectrum


@dataclass(frozen=True)
class PreviewProcessing:
    """SDK correction flags; existing type name retained for compatibility."""
    electric_dark: bool = False
    nonlinearity: bool = False

    def validate(self):
        if type(self.electric_dark) is not bool or type(self.nonlinearity) is not bool:
            raise ValueError("SDK correction flags must be Boolean values.")
        if self.nonlinearity and not self.electric_dark:
            raise ValueError("Enable electric-dark correction when matching OceanView nonlinearity correction.")


# Fixed application policy: documented OceanDirect standard corrections.
# Keep the value type for saved metadata and adapter readback/restoration.
SDK_PROCESSING = PreviewProcessing(electric_dark=True, nonlinearity=True)


@dataclass(frozen=True)
class TimingBudget:
    readout_s: float = 0.020
    uncertainty_s: float = 0.020
    edge_margin_s: float = 0.020
    lateness_s: float = 0.200
    command_timeout_s: float = 0.5

    def validate(self):
        for value in asdict(self).values():
            if not math.isfinite(value) or value <= 0:
                raise ValueError("Timing budgets must be positive finite values.")

    def sdk_read_budget(self, settings):
        # SDK may accumulate N detector exposures inside one formatted read.
        return settings.scans * (settings.integration_us/1e6 + self.readout_s + self.uncertainty_s)

    def scan_period(self, settings):
        # Two estimated complete averaging windows, not a freshness proof.
        return 2 * self.sdk_read_budget(settings)

    def collection_window(self, settings):
        return self.scan_period(settings) + self.sdk_read_budget(settings)


@dataclass(frozen=True)
class RunSettings:
    on_s: float = 1.0
    off_s: float = 1.0
    cycles: int = 100
    acquisition: Settings = Settings(10000, 3, 2)
    timing: TimingBudget = TimingBudget()
    preview_processing: PreviewProcessing = SDK_PROCESSING

    def validate(self):
        self.timing.validate()
        self.preview_processing.validate()
        if not all(math.isfinite(v) and v > 0 for v in (self.on_s, self.off_s)):
            raise ValueError("LED on/off times must be positive finite seconds.")
        if type(self.cycles) is not int or not 1 <= self.cycles <= 100000:
            raise ValueError("Cycles must be an integer from 1 to 100000.")
        us = round(self.on_s * 1e6)
        if not math.isclose(us / 1e6, self.on_s, abs_tol=1e-9, rel_tol=0) or us % 20:
            raise ValueError("LED-on time must be a multiple of 20 microseconds (Mightex pulse resolution).")
        if us > 2147483647:
            raise ValueError("LED-on time exceeds the documented SDK integer time range.")
        s = self.acquisition
        if any(type(v) is not int for v in (s.integration_us, s.scans, s.boxcar)):
            raise ValueError("Integration, scans and boxcar must be integers in SDK units.")
        if s.integration_us <= 0 or not 1 <= s.scans <= 100 or not 0 <= s.boxcar <= 50:
            raise ValueError("Scans must be 1–100, boxcar half-width 0–50, integration positive.")
        required = self.timing.collection_window(s) + 2 * self.timing.edge_margin_s + self.timing.lateness_s
        if required >= self.off_s:
            raise ValueError(f"Off-period too short: need more than {required:.3f} s; provided {self.off_s:.3f} s.")

    @property
    def duration_s(self):
        return self.cycles * (self.on_s + self.off_s)

    @property
    def estimated_cycle_s(self):
        # Additional host command allowance, not measured optical timing.
        return self.on_s + self.off_s + 2*self.timing.command_timeout_s + 2*self.timing.lateness_s + 20e-6

    @property
    def estimated_duration_s(self):
        return self.timing.collection_window(self.acquisition) + self.timing.command_timeout_s + self.cycles*self.estimated_cycle_s

    @property
    def led_on_s(self):
        return self.cycles * self.on_s


@dataclass(frozen=True)
class Capture:
    spectrum: Spectrum
    scans: tuple
    provenance: dict


class Stopped(Exception):
    pass


class TimingOverrun(RuntimeError):
    pass
