# Irradiation experiment desktop application — version 2

Targets: **QE-PRO QEP06321** through **OceanDirect 1.30.4** and **Mightex BLS-IO04-U/S 04-210326-002** through the supplied x64 USB SDK. This GUI uses real hardware; the simulation selector and simulated probe controls have been removed. Connections and experiments start only when requested.

This repository contains the version 2 Python source package. The version 1 snapshot is not included.

The light pink interface groups controls into four tabs on the left: **Experiment** (acquisition and dark/reference setup), **Timing Estimates**, **LED Settings** (irradiation output and timed intensity test), and **Output** (folder, name and export options). The compact controls fit without a scrollbar; the tabs and intensity/absorbance plots start directly below the header controls. Device status and messages appear beneath the graph. Start/Stop share the connection row on the right, with **New Run** directly beneath them. The footer reads **Version 2.1 Created by Elizabeth Recker & Lydia Hall, ZAP Group, UT Austin**.

## Launch

This application was developed for 64-bit Windows and tested with **Python 3.14.8 x64**, Matplotlib 3.11.2 and openpyxl 3.1.5. Create a virtual environment and install the listed Python dependencies from this repository root:

```powershell
py -3.14 -m venv .venv
& '.\.venv\Scripts\python.exe' -m pip install -r requirements.txt
```

The OceanDirect and Mightex SDKs, device drivers, and connected instruments are required for hardware operation and are not included in this repository. Obtain and install them from the vendors in accordance with their terms. Python must include working Tcl/Tk support; no Tcl/Tk runtime is bundled. Opening the GUI does not connect devices or start irradiation.

Launch from the repository root:

```powershell
.\start-app.ps1
```

To create a Desktop shortcut that opens the GUI without a console window, run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\create-desktop-shortcut.ps1
```

Or launch the GUI directly:

```powershell
& '.\.venv\Scripts\python.exe' -B '.\gui.py'
```

The optional preview utility can be started with `.\start-preview.ps1`. The application uses local settings and run-cache folders at runtime; these are not part of the published source package.

## Setting up the spectrometer

1. Close other software that owns the QE-PRO connection, including OceanView. Click **Connect QE-PRO**. This connects only the spectrometer and starts live intensity preview. LED inputs and an LED connection are unnecessary.
2. Enter **integration time (ms)**, **scans to average**, and **boxcar half-width (pixels)**; click **Update settings**. Wait for a new spectrum. The display reports the applied settings.
3. With irradiation off, block the probe light and click **Capture dark**.
4. Unblock the probe light and insert the blank slide/reference, without the sample. The signal must rise above the blocked-light level. Click **Capture reference**. A weak, blocked or saturated reference keeps absorbance acquisition disabled.
5. Load the sample. Select **Absorbance** to see live sample absorbance before starting an experiment. A usable matching dark/reference pair is required.
6. For another blank slide, keep irradiation off, insert the new blank and click **Background: new blank slide**. This replaces the reference while retaining the matching dark. The same blank should then read near zero within noise and drift; no arbitrary baseline subtraction is performed. Load the next sample afterward.

Changing acquisition settings invalidates the backgrounds. Apply changes and capture dark/reference again. A mismatch between requested settings and SDK readback stops acquisition instead of accepting a spectrum with different settings.

## What the three acquisition settings do

**Integration time** controls how long the detector collects light. **Scans to average** controls temporal averaging of multiple exposures in the vendor SDK/driver. **Boxcar half-width `b`** controls averaging over neighboring detector pixels: **`2b+1` pixels**, so `b=2` means five pixels and `b=0` disables smoothing. Averaging and boxcar are mathematical processing operations, even when performed by the vendor acquisition software. See the [official Ocean Optics explanation](https://www.oceanoptics.com/faq/software-faqs/).

Version 2 uses the documented OceanDirect calls:

```text
set_integration_time(integration_us)
set_scans_to_average(N)
set_boxcar_width(b)
get_integration_time(), get_scans_to_average(), get_boxcar_width()
get_formatted_spectrum()
```

The selected values apply to **live intensity, dark, reference, live absorbance, and every experiment spectrum**. The application verifies readback before each formatted read. It preserves that returned intensity array exactly: there is no additional Python averaging or boxcar filtering before absorbance calculation. SDK edge behavior is retained without imposing Python edge rules.

There are no electric-dark or nonlinearity checkboxes. Preview, dark/reference, live absorbance and experiment samples all use **electric-dark and nonlinearity corrections enabled inside OceanDirect**, the standard defaults documented in the [official OceanDirect manual](https://www.oceanoptics.com/wp-content/uploads/2024/05/MNL-1025-OceanDirect-User-Manual-060822.pdf). Correction state is read back before each spectrum; unexpected changes stop acquisition. These vendor corrections are distinct from the captured optical dark used in absorbance calculation. No additional Python correction, averaging or smoothing is applied to the returned intensity.

For an OceanView comparison, select **intensity/counts**, match integration time, scans and boxcar half-width, enable electric-dark and nonlinearity there, and use the same probe/optical setup. Compare without an extra OceanView stored-dark subtraction, baseline adjustment, stray-light correction, irradiance conversion or other processing. The app cannot read OceanView's session configuration automatically. Ordinary measurement noise/drift can also prevent exact equality; agreement still requires a hardware comparison.

A formatted SDK spectrum is already averaged/smoothed when those settings are selected. Its individual unaveraged, unsmoothed detector exposures are unavailable through this call. Settings acceptance and readback establish the requested configuration; they do not verify the SDK's numerical averaging behavior, acquisition freshness, or agreement with OceanView.

## LED connection and intensity test

**Connect LED** opens the Mightex controller independently of QE-PRO and requests Disable on all four outputs. Connection requires no channel or output level. Before enabling output, enter the confirmed **channel 1–4**, an **approved output level**, and confirm the channel/level and absence of external triggering. The app never guesses these values. The level uses 0.1% steps and represents a percentage of the configured control-voltage maximum, not measured optical power.

**LED ON (timed test)** issues the documented finite pulse at that level for the entered duration, from 0.02 to 30 seconds. **LED OFF** ends it early and keeps connections open. **Disconnect LED** cancels the test and closes LED control. QE-PRO is unnecessary for LED-only tests. Every LED test invalidates backgrounds; recapture them with irradiation disabled. Background capture, settings changes and experiment Start are unavailable during a test.

Successful experiment completion inhibits further pulses and explicitly requests LED Disable, then finishes exporting while keeping QE-PRO and LED connected. The LED remains off between runs. **STOP / LED OFF**, experiment errors and window close inhibit pulses and request LED Disable before closing the experiment's device connections. Explicit device Disconnect controls still close the selected connection. Failures appear visibly. LED commands run independently of the spectrometer process, so a blocked spectrum read cannot hold up the LED-off request. An already executing Mightex SDK call cannot be safely interrupted.

## Experiment settings and timing

Defaults remain adjustable: **LED on 1 s**, **LED off 1 s**, **100 cycles**, **integration 10 ms**, **3 scans**, **boxcar half-width 2**. The GUI shows total LED-on time and an estimated duration including command allowances. Start/Stop are on the connection row at the top right.

Before the first irradiation pulse, the controller requests LED Disable and acquires the actual sample's **time-zero UV–Vis spectrum with no irradiation** using the selected SDK settings and matching dark/reference. It saves this measurement durably before allowing the first pulse. A read failure, timeout, saturated/invalid baseline, failed save, or Stop prevents irradiation from starting. This is a separate sample measurement, not the blank/reference and not an extra strobe cycle.

One cycle is a finite on-pulse followed by an off-period and one SDK-produced averaged spectrum. The controller uses the documented single-pulse profile with repeat count 1 and 20-microsecond zero-output leading/trailing stages. It calls SoftStart once per cycle. Disable acknowledgement anchors the requested off-period. Host SDK overhead adds run time; host acknowledgements are not measured optical edges. No device reset or nonvolatile setting writes are used. The estimated run duration includes the initial time-zero acquisition and LED-off command allowance.

Timing estimates account for the selected averaging settings:

```text
SDK read budget = N × (integration + per-scan readout allowance + per-scan uncertainty)
settling wait = 2 × SDK read budget
collection envelope = settling wait + SDK read budget
required off duration > collection envelope + 2 × edge margin + scheduler lateness
```

The full estimated collection envelope is centered in the acknowledged off-period. Defaults use 20 ms readout and 20 ms uncertainty per scan, 20 ms edge margins and 200 ms allowed scheduler lateness. With 10 ms integration and three scans, the envelope is **450 ms** and the off-period must exceed **690 ms**. LED command timeout is a separate 500 ms budget. Longer integrations or more scans may require a longer off-period.

Hover over the small **ⓘ** beside an experiment or timing parameter for an explanation of its units and effect. The icons also show help when reached with keyboard Tab focus. Timing allowances remain adjustable estimates, not measured optical timing.

QE-PRO returns its latest completed spectrum. Waiting two estimated averaging windows is a conservative settling policy, not proof that all constituent exposures occurred inside the off-period. Free-running trigger mode 0 is required; the app does not change trigger mode. Before each read, buffering must report disabled or satisfy the previously inspected **QE-PRO/OceanDirect 1.30.4** fallback: the enable getter returns vendor error 24, capacity is exactly one, and stored count is zero or one. Single-slot buffering prevents a retained FIFO history but does not establish spectrum age. No buffer clears or setters are sent.

Observed slow background reads are rejected before experiment Start. Missed command, acquisition, scheduling or durable-save deadlines stop further pulses. Completed measurements remain saved. The new SDK averaging behavior and physical cross-device timing still require hardware validation; no synchronized optical timing is claimed.

## Absorbance and saving

### Run finished sound

Open **Output → Run finished sound → Choose MP3…** and select a short audio clip from your computer. The app saves its own copy under **`python-app/user-settings/completion.mp3`** and remembers it across New Run and app restarts. Moving or deleting the original clip does not affect the saved copy. Use **Preview sound** to check playback and volume, **Choose MP3…** to replace it, or **Remove sound** to disable the notification.

The selected clip plays once whenever an experiment completes, is stopped, or fails, after the controller ends the run; Excel export may still be finishing. Idle device disconnections and manual re-exports do not play it. Playback runs separately from acquisition and does not block the GUI. Audio failures appear in the sound panel and do not change the experiment outcome. No notification sound is configured until you choose a clip. Playback uses the computer's audio output and volume.

The Windows playback wrapper uses the documented [MCI play command](https://learn.microsoft.com/en-us/windows/win32/multimedia/play) without waiting for the clip to finish. Actual audible playback should be checked with Preview sound on the experiment computer.

### Absorbance output

Absorbance is calculated directly from the matching SDK intensity arrays:

```text
A = -log10[(sample-dark)/(reference-dark)]
```

Weak/nonpositive corrected references, nonpositive corrected samples and saturation are flagged; invalid absorbance cells are blank. Negative absorbance is retained. Saturation checks inspect the SDK-returned pixels only: averaging or smoothing can hide source-exposure clipping. A saturated sample or a sample with no valid absorbance is saved with its intensities/flags, then stops the run.

Select an existing output folder and experiment name. Each run creates a unique folder containing only **`combined_absorbance.xlsx`**. Its single **Absorbance** sheet has wavelength (nm) in the first column, **`time_0_no_irradiation_absorbance_AU`** in the second, then one absorbance column per completed irradiation cycle. No CSV, individual-cycle files, raw-data sheets or metadata sheets are placed in the selected output folder. Excel limits a run to **16,382 irradiation cycles plus time zero**.

The workbook updates incrementally on a separate worker. Completed measurements first commit durably to the private **`python-app/run-cache/<run name>`** folder; Excel generation and destination writes do not block timing control. Recovery records there preserve settings, dark/reference, SDK sample intensities, absorbance, flags, cycle numbers and host timestamps. **`cycle_000000.json`** stores the time-zero sample, with zero irradiation elapsed; cycles 1 onward store measurements following pulses. Time zero does not increment the completed irradiation-cycle count. Re-export includes time zero when present; older runs retain their existing measurements. They remain available after completion or interruption. Existing saved experiment folders are preserved.

**New Run** prompts for a different experiment name, then clears displayed spectra, progress, cycle counts and the previous output location. Acquisition, timing, LED and folder settings are retained. QE-PRO and LED remain connected after successful runs, with LED output disabled. Matching backgrounds remain available: insert the next blank slide and select **Background: new blank slide**, then load its sample and Start. The next experiment takes a fresh time-zero sample before irradiation. Cancel leaves the current display unchanged. New Run is unavailable during acquisition, LED testing, shutdown or export, and never starts hardware or deletes saved experiments.

Keep the output workbook closed in Excel while the experiment is updating it. A destination write error or backlog of 16 pending exports stops further pulses and reports the internal recovery location. Local recovery data remains intact. **Re-export a saved/interrupted run** accepts the selected run folder or its internal recovery folder and rebuilds the combined Excel workbook after the destination is available. Older JSON-based runs can also be re-exported without modifying their original data files.

Excel-only cycle saving commits the recovery JSON without writing an unused per-cycle CSV. Durable-save deadlines still apply: a slow local save stops further pulses with LED output disabled. Cleanup may finish saving the timed-out measurement, so the final workbook can include one more cycle than the displayed completed count. Increasing LED off duration provides more saving time; changing the Excel output folder does not move the private local recovery cache.

Destination workbook writes retry access-denied errors up to five attempts with 1.5 seconds of total waiting on the export worker, allowing brief Box sync locks to clear. Persistent locks report the workbook paths and recovery steps. Close the workbook in Excel before re-exporting; retries cannot release a workbook held open by Excel.

Private manifests retain **format version 2** and SDK processing provenance. JSON's `scans` array holds one SDK-produced result, not individual raw exposures; `raw_source_scans_available` is false. Timestamp provenance is host UTC/monotonic, never measured optical timing.

## Code and validation

`spectrometer.py`: documented OceanDirect adapter; `acquisition.py`: isolated SDK acquisition; `led.py` and `led_session.py`: documented Mightex control; `models.py`, `control.py`, `session.py`: validation/scheduling/lifecycle; `processing.py`, `reference.py`: absorbance; `storage.py`: saving/export; `gui.py`, `bootstrap.py`: desktop/runtime. Test-only fake devices remain available internally for regression testing; the shipped GUI cannot select them.

From `python-app`, run the regression suite:

```powershell
& '.\.venv\Scripts\python.exe' -B -m unittest -q test_preview test_application test_safety test_led_test test_buffer_policy test_connections test_spectrum_update test_run_timing test_new_blank test_storage_cache test_version2 test_version21 test_time_zero test_completion_sound
```

Latest validation: **124 tests passed**. Tests use explicit fake devices and never load vendor hardware libraries. They cover settings/correction propagation and readback, rejection of unexpected correction changes, unchanged SDK intensities, no duplicate averaging/smoothing, complete sessions, GUI connection gates, invalid references, absorbance math, deadline rejection, independent LED-off, interrupted-run recovery, new blanks and legacy Box-cache behavior. Version 2.1 checks also cover the exact footer, control placement, New Run preserving settings/backgrounds and saved files, cancel/unchanged-name handling, operation gates, incremental Excel-only output, slow exports, copy failures, temporary-lock retries, recovery, bounded backlog and Excel's column limit. Time-zero checks verify sample acquisition and durable saving before any pulse, inhibition while saving, Stop/read/invalid-baseline failure paths, workbook/recovery order, and a baseline graph trace that does not count as an irradiation cycle.

Hardware checks still needed: numerical agreement with OceanView, actual averaging/smoothing/correction behavior, fresh exposures within the off-period, physical LED intensity/pulse duration/count/Disable effect, and measured readout/USB/OS margins. The separate physical strobe test remains deferred. **No hardware was operated while implementing these version 2 changes.**

SDK/model compatibility for future HR6 support remains separate from QE-PRO operation. SDK directory and serial are adapter/process parameters; adding HR6 requires a compatible SDK and validated HR6 acquisition policy.

References: [OceanDirect](https://www.oceanoptics.com/software/oceandirect/), [Ocean Optics acquisition settings](https://www.oceanoptics.com/faq/software-faqs/), [QE-PRO manual](https://www.oceanoptics.com/wp-content/uploads/2025/04/OceanQEPro_Manual_June2025.pdf). Mightex bindings follow the preserved local x64 SDK header and manufacturer manuals. Prior read-only diagnoses are retained under `diagnostics`; version 1's README retains the earlier implementation history.
