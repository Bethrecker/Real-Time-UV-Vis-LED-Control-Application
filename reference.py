"""Conservative application checks; thresholds are not a measured SNR."""
from dataclasses import dataclass
import math
from spectrometer import validate_spectrum


@dataclass(frozen=True)
class ReferenceCheck:
    usable: bool
    mask: tuple
    message: str
    threshold_counts: float


def saturated(spectrum, maximum):
    return any(v >= maximum for v in spectrum.intensities)


def check_reference(dark, reference, maximum):
    threshold = max(20.0, maximum * 0.0005)
    if dark is None or reference is None:
        return ReferenceCheck(False, (), "Capture blocked-probe dark, then unblocked blank/reference.", threshold)
    if dark.settings != reference.settings or dark.serial != reference.serial or dark.wavelengths != reference.wavelengths:
        return ReferenceCheck(False, (), "Dark and reference must use the same device, wavelength grid and settings.", threshold)
    try:
        validate_spectrum(dark.wavelengths, dark.intensities)
        validate_spectrum(reference.wavelengths, reference.intensities)
    except ValueError as exc:
        return ReferenceCheck(False, (), str(exc), threshold)
    if saturated(dark, maximum) or saturated(reference, maximum):
        return ReferenceCheck(False, (), "Dark or reference is saturated. Reduce integration and recapture both.", threshold)
    corrected = tuple(r - d for r, d in zip(reference.intensities, dark.intensities))
    mask = tuple(v > threshold for v in corrected)
    required = max(10, math.ceil(0.1 * len(mask)))
    count = sum(mask)
    usable = count >= required
    message = f"{count}/{len(mask)} reference pixels exceed dark by {threshold:g} counts; {required} required."
    if not usable:
        message += " Reference unusable: unblock probe light, remove sample, check optical path, then recapture."
    elif count != len(mask):
        message += " Weak/nonpositive reference pixels will be excluded from absorbance."
    else:
        message += " Reference usable."
    return ReferenceCheck(usable, mask, message, threshold)


def absorbance(sample, dark, reference, maximum):
    check = check_reference(dark, reference, maximum)
    if not check.usable:
        raise ValueError(check.message)
    validate_spectrum(sample.wavelengths, sample.intensities)
    if sample.settings != reference.settings or sample.serial != reference.serial or sample.wavelengths != reference.wavelengths:
        raise ValueError("Sample does not match reference settings/device/grid. Recapture dark and reference.")
    if saturated(sample, maximum):
        raise ValueError("Sample is saturated; absorbance acquisition refused.")
    values = []
    for valid, s, d, r in zip(check.mask, sample.intensities, dark.intensities, reference.intensities):
        numerator = s - d
        values.append(-math.log10(numerator / (r - d)) if valid and numerator > 0 else None)
    if not any(v is not None for v in values):
        raise ValueError("No positive dark-corrected sample signal in usable reference pixels.")
    return tuple(values)
