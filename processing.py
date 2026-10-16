"""Dark-corrected absorbance from SDK intensities; no host smoothing."""
from dataclasses import dataclass
import math
from reference import check_reference
from spectrometer import validate_spectrum


def average(scans):
    if not scans or len({len(s) for s in scans}) != 1:
        raise ValueError("Averaging requires equal-length nonempty scans.")
    return tuple(math.fsum(values) / len(scans) for values in zip(*scans))


def boxcar(values, half_width):
    """2b+1 centered pixels; truncated, renormalized windows at detector edges."""
    if half_width == 0:
        return tuple(values)
    return tuple(math.fsum(values[max(0, i-half_width):i+half_width+1]) /
                 len(values[max(0, i-half_width):i+half_width+1]) for i in range(len(values)))


def saturation_mask(capture, maximum):
    return tuple(any(scan[i] >= maximum for scan in capture.scans) for i in range(len(capture.spectrum.intensities)))


def usable_reference(dark, reference, maximum):
    if dark is None or reference is None:
        return check_reference(None, None, maximum)
    if any(saturation_mask(dark, maximum)) or any(saturation_mask(reference, maximum)):
        from reference import ReferenceCheck
        return ReferenceCheck(False, (), "A returned dark/reference SDK intensity is saturated. Reduce integration and recapture both.", max(20, maximum * 0.0005))
    return check_reference(dark.spectrum, reference.spectrum, maximum)


@dataclass(frozen=True)
class Processed:
    absorbance: tuple
    flags: tuple


def process(sample, dark, reference, maximum):
    check = usable_reference(dark, reference, maximum)
    if not check.usable:
        raise ValueError(check.message)
    ss, ds, rs = sample.spectrum, dark.spectrum, reference.spectrum
    if ss.settings != rs.settings or ss.serial != rs.serial or ss.wavelengths != rs.wavelengths:
        raise ValueError("Sample, dark and reference settings/device/grid do not match.")
    validate_spectrum(ss.wavelengths, ss.intensities)
    s, d, r = (x.intensities for x in (ss, ds, rs))
    clipped = saturation_mask(sample, maximum)
    values, flags = [], []
    for i, (sv, dv, rv) in enumerate(zip(s, d, r)):
        # SDK already applied boxcar; do not spread/filter its result again.
        reason = "saturated_sdk_intensity" if clipped[i] else ""
        if not reason and (not check.mask[i] or rv - dv <= check.threshold_counts):
            reason = "invalid_reference"
        if not reason and sv - dv <= 0:
            reason = "nonpositive_sample_minus_dark"
        value = None if reason else -math.log10((sv - dv) / (rv - dv))
        if value is not None and not math.isfinite(value):
            value, reason = None, "nonfinite_absorbance"
        values.append(value)
        flags.append(reason or "valid")
    return Processed(tuple(values), tuple(flags))
