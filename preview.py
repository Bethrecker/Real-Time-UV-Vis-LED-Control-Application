"""Spectrometer-only desktop preview. No irradiation controls exist here."""
import argparse
import csv
from dataclasses import asdict
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import queue
import sys
import time

PROJECT = Path(__file__).resolve().parent


def configure_local_tk():
    # Optional project-local matching Tcl/Tk resources, never global settings.
    root = PROJECT / "runtime" / "tcl"
    for name, variable in [("tcl_library", "TCL_LIBRARY"), ("tk_library", "TK_LIBRARY")]:
        location = root / name
        if location.is_dir():
            os.environ[variable] = str(location)


configure_local_tk()
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from spectrometer import DEFAULT_SDK, DEFAULT_SERIAL, Settings
from reference import absorbance, check_reference, saturated
from worker import PreviewWorker


class Plot(ttk.Frame):
    """Dependency-free Tk plot with counts-preserving points and invalid gaps."""
    def __init__(self, parent):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, background="white", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.traces = []
        self.ylabel = "Raw intensity (counts)"
        self.canvas.bind("<Configure>", lambda event: self.draw())

    def show(self, traces, ylabel):
        self.traces, self.ylabel = traces, ylabel
        self.draw()

    def draw(self):
        c = self.canvas
        c.delete("all")
        width, height = c.winfo_width(), c.winfo_height()
        if width < 150 or height < 100:
            return
        left, top, right, bottom = 85, 35, width - 25, height - 55
        finite = [(x, y) for _, _, xs, ys in self.traces for x, y in zip(xs, ys) if y is not None and math.isfinite(y)]
        if not finite:
            c.create_text(width / 2, height / 2, text="Connect to start the spectrometer-only preview.", fill="#475569")
            return
        xmin, xmax = min(x for x, y in finite), max(x for x, y in finite)
        ymin, ymax = min(y for x, y in finite), max(y for x, y in finite)
        if "counts" in self.ylabel:
            ymin = min(0, ymin)
        margin = max((ymax - ymin) * 0.06, 1)
        ymin, ymax = ymin - margin, ymax + margin
        if xmax <= xmin:
            xmax = xmin + 1
        def point(x, y):
            return left + (x - xmin) / (xmax - xmin) * (right - left), bottom - (y - ymin) / (ymax - ymin) * (bottom - top)
        for n in range(6):
            y = ymin + n * (ymax - ymin) / 5
            py = point(xmin, y)[1]
            c.create_line(left, py, right, py, fill="#e2e8f0")
            c.create_text(left - 8, py, text=f"{y:.4g}", anchor="e", fill="#475569")
            x = xmin + n * (xmax - xmin) / 5
            px = point(x, ymin)[0]
            c.create_text(px, bottom + 15, text=f"{x:.1f}", fill="#475569")
        c.create_line(left, top, left, bottom, right, bottom, fill="#334155")
        c.create_text((left + right) / 2, height - 15, text="Wavelength (nm)")
        c.create_text(16, (top + bottom) / 2, text=self.ylabel, angle=90)
        legend = left
        for label, color, xs, ys in self.traces:
            c.create_text(legend, 15, text=label, fill=color, anchor="w")
            legend += max(130, len(label) * 7 + 30)
            segment = []
            def flush():
                if len(segment) >= 4:
                    c.create_line(*segment, fill=color, width=1.5)
                segment.clear()
            for x, y in zip(xs, ys):
                if y is None or not math.isfinite(y):
                    flush()
                else:
                    segment.extend(point(x, y))
            flush()


class PreviewApp:
    def __init__(self, root, sdk, serial):
        self.root, self.sdk, self.serial = root, sdk, serial
        self.worker = None
        self.events = queue.Queue()
        self.identity = self.settings = self.latest = self.dark = self.reference = self.abs_sample = None
        self.abs_values = None
        self.pending = None
        self.closing = False
        self.stop_started = None
        self.cleanup_failed = False
        root.title(f"Spectrometer preview — {serial}")
        root.geometry("1150x850")
        root.minsize(850, 680)
        root.protocol("WM_DELETE_WINDOW", self.close)
        outer = ttk.Frame(root, padding=14)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Spectrometer-only preview", font=("Segoe UI", 18, "bold")).pack(anchor="w")
        ttk.Label(outer, text="Mightex disabled • Irradiation LED is not controlled • Timing and averaging behavior are not verified").pack(anchor="w", pady=(2, 10))
        row = ttk.Frame(outer)
        row.pack(fill="x")
        self.connect_button = ttk.Button(row, text="Connect / Start preview", command=self.connect)
        self.connect_button.pack(side="left")
        self.stop_button = ttk.Button(row, text="Stop / Disconnect", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        self.status = tk.StringVar(value=f"Disconnected. Target: {serial}")
        ttk.Label(row, textvariable=self.status).pack(side="left", padx=8)
        controls = ttk.LabelFrame(outer, text="Acquisition settings", padding=10)
        controls.pack(fill="x", pady=10)
        self.integration = tk.StringVar(value="10")
        self.scans = tk.StringVar(value="1")
        self.boxcar = tk.StringVar(value="0")
        self.entries = []
        for column, (label, variable) in enumerate([("Integration (ms)", self.integration), ("Scans to average", self.scans), ("Boxcar half-width", self.boxcar)]):
            ttk.Label(controls, text=label).grid(row=0, column=column * 2, padx=(0, 8))
            entry = ttk.Entry(controls, textvariable=variable, width=10)
            entry.grid(row=0, column=column * 2 + 1, padx=(0, 20))
            self.entries.append(entry)
            variable.trace_add("write", self.input_changed)
        self.apply_button = ttk.Button(controls, text="Apply settings", command=self.apply_settings, state="disabled")
        self.apply_button.grid(row=0, column=6)
        ttk.Label(controls, text="Boxcar b uses 2b + 1 pixels (b=2 → 5 pixels; b=0 → disabled). Readback verifies acceptance only.").grid(row=1, column=0, columnspan=7, sticky="w", pady=(8, 0))
        self.limits = tk.StringVar(value="Preview limits: integration × scans ≤ 5 s; scans 1–100; boxcar 0–50.")
        ttk.Label(controls, textvariable=self.limits).grid(row=2, column=0, columnspan=7, sticky="w", pady=(4, 0))
        steps = ttk.LabelFrame(outer, text="Blocked versus unblocked probe-light check", padding=10)
        steps.pack(fill="x")
        ttk.Label(steps, text="1. Keep irradiation disabled. Block the probe light manually; watch counts, then capture dark.\n2. Remove the sample, unblock the probe light, keep the blank in the optical path; capture reference.\n3. Check that reference rises above dark. Load the sample only after the reference is usable.", justify="left").pack(anchor="w")
        buttons = ttk.Frame(steps)
        buttons.pack(fill="x", pady=(8, 0))
        self.dark_button = ttk.Button(buttons, text="Capture dark (probe blocked)", command=lambda: self.capture("dark"), state="disabled")
        self.dark_button.pack(side="left")
        self.reference_button = ttk.Button(buttons, text="Capture blank/reference (unblocked)", command=lambda: self.capture("reference"), state="disabled")
        self.reference_button.pack(side="left", padx=8)
        self.abs_button = ttk.Button(buttons, text="Acquire absorbance", command=lambda: self.capture("absorbance"), state="disabled")
        self.abs_button.pack(side="left")
        self.calibration_status = tk.StringVar(value="Capture dark and reference with matching settings.")
        ttk.Label(steps, textvariable=self.calibration_status, wraplength=1050).pack(anchor="w", pady=(8, 0))
        self.warning = tk.StringVar(value="")
        ttk.Label(outer, textvariable=self.warning, foreground="#b45309", wraplength=1050).pack(anchor="w", pady=8)
        display = ttk.Frame(outer)
        display.pack(fill="x")
        self.display = tk.StringVar(value="raw")
        ttk.Radiobutton(display, text="Live intensity + dark/reference", variable=self.display, value="raw", command=self.redraw).pack(side="left")
        ttk.Radiobutton(display, text="Last absorbance", variable=self.display, value="absorbance", command=self.redraw).pack(side="left", padx=10)
        self.save_button = ttk.Button(display, text="Save preview / calibration CSV", command=self.save, state="disabled")
        self.save_button.pack(side="right")
        self.plot = Plot(outer)
        self.plot.pack(fill="both", expand=True, pady=(8, 0))
        self.detail = tk.StringVar(value="Timestamps describe host calls, not the detector integration window.")
        ttk.Label(outer, textvariable=self.detail).pack(anchor="w", pady=(6, 0))
        root.after(100, self.poll)

    def read_inputs(self):
        ms = float(self.integration.get())
        if not math.isfinite(ms) or ms <= 0 or not math.isclose(ms * 1000, round(ms * 1000), abs_tol=1e-6, rel_tol=0):
            raise ValueError("Integration must be a positive number in whole microseconds.")
        return Settings(round(ms * 1000), int(self.scans.get()), int(self.boxcar.get()))

    def inputs_match(self):
        try:
            return self.read_inputs() == self.settings
        except ValueError:
            return False

    def input_changed(self, *args):
        if hasattr(self, "abs_button"):
            self.update_buttons()

    def update_buttons(self):
        active = self.worker is not None and self.identity is not None and self.stop_started is None
        ready = active and self.pending is None and self.inputs_match()
        check = check_reference(self.dark, self.reference, self.identity.maximum_intensity if self.identity else 200000)
        def enable(button, value):
            button.configure(state="normal" if value else "disabled")
        enable(self.connect_button, self.worker is None and not self.closing and not self.cleanup_failed)
        enable(self.stop_button, self.worker is not None and self.stop_started is None)
        enable(self.apply_button, active and self.pending is None)
        enable(self.dark_button, ready)
        enable(self.reference_button, ready and self.dark is not None)
        enable(self.abs_button, ready and check.usable)
        enable(self.save_button, self.latest is not None and self.pending is None and self.stop_started is None)
        for entry in self.entries:
            entry.configure(state="disabled" if self.pending or self.stop_started else "normal")
        self.calibration_status.set(check.message + (" Apply edited settings before capture." if active and not self.inputs_match() else ""))

    def connect(self):
        self.identity = self.settings = self.latest = self.dark = self.reference = self.abs_sample = None
        self.abs_values = None
        self.warning.set("")
        self.status.set("Connecting to target spectrometer…")
        self.worker = PreviewWorker(self.sdk, self.serial, self.events)
        self.worker.start()
        self.update_buttons()

    def apply_settings(self):
        try:
            settings = self.read_inputs()
            i = self.identity
            if not i.minimum_us <= settings.integration_us <= min(i.maximum_us, 5_000_000):
                raise ValueError("Integration is outside the displayed preview limits.")
            if settings.scans < 1 or settings.scans > 100 or settings.boxcar < 0 or settings.boxcar > 50:
                raise ValueError("Scans must be 1–100; boxcar must be 0–50.")
            if settings.integration_us * settings.scans > 5_000_000:
                raise ValueError("Integration × scans exceeds the 5-second preview limit.")
            if (settings.integration_us - i.minimum_us) % max(1, i.increment_us):
                raise ValueError(f"Integration increment is {i.increment_us} µs.")
        except (ValueError, OverflowError) as exc:
            messagebox.showerror("Invalid settings", str(exc))
            return
        self.pending = "settings"
        self.dark = self.reference = self.abs_sample = None
        self.abs_values = None
        self.warning.set("Applying settings; previous dark/reference invalidated. Readback does not establish averaging behavior.")
        self.worker.commands.put(("settings", settings))
        self.update_buttons()

    def capture(self, kind):
        if not self.inputs_match() or self.pending:
            return
        if kind == "reference" and self.dark is None:
            return
        if kind == "absorbance" and not check_reference(self.dark, self.reference, self.identity.maximum_intensity).usable:
            return
        self.pending = kind
        self.worker.commands.put((kind, None))
        self.status.set(f"Capturing {kind} on the next acquisition…")
        self.update_buttons()

    def stop(self):
        if self.worker:
            self.stop_started = time.monotonic()
            self.worker.stop()
            self.status.set("Stopping after the current SDK call; restoring settings and closing…")
            self.update_buttons()

    def close(self):
        self.closing = True
        if self.worker:
            if self.stop_started is None:
                self.stop()
        else:
            self.root.destroy()

    def poll(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                self.handle(kind, payload)
        except queue.Empty:
            pass
        if self.closing and self.worker is None:
            self.root.destroy()
            return
        if self.stop_started and time.monotonic() - self.stop_started > 8:
            self.warning.set("SDK call has not returned. GUI remains responsive; cleanup has not completed. Do not assume device closure.")
        self.root.after(100, self.poll)

    def handle(self, kind, payload):
        if kind == "connected":
            self.identity, self.settings = payload
            self.integration.set(f"{self.settings.integration_us / 1000:g}")
            self.scans.set(str(self.settings.scans))
            self.boxcar.set(str(self.settings.boxcar))
            i = self.identity
            self.status.set(f"Connected: {i.model} / {i.serial} • SDK {'.'.join(map(str, i.api_version))}")
            self.limits.set(f"Device integration: {i.minimum_us / 1000:g}–{i.maximum_us / 1000:g} ms; increment {i.increment_us} µs. Preview capped at 5 s × scans; scans 1–100, boxcar 0–50.")
        elif kind == "settings":
            self.settings = payload
            self.latest = None
            self.pending = None
            self.status.set("Settings accepted and read back. Capture new dark and reference.")
        elif kind in ("preview", "dark", "reference", "absorbance"):
            if self.stop_started is not None:
                return
            if payload.settings != self.settings:
                return
            self.latest = payload
            max_count = self.identity.maximum_intensity
            clipped = saturated(payload, max_count)
            self.warning.set("SATURATION: counts reached the SDK maximum. Reduce integration and recapture dark/reference." if clipped else "")
            if kind != "preview":
                self.pending = None
                self.status.set(f"Captured {kind}.")
            if kind == "dark":
                self.dark = payload
                self.reference = self.abs_sample = self.abs_values = None
            elif kind == "reference":
                self.reference = payload
                self.abs_sample = self.abs_values = None
            elif kind == "absorbance":
                try:
                    self.abs_values = absorbance(payload, self.dark, self.reference, max_count)
                    self.abs_sample = payload
                    self.display.set("absorbance")
                    invalid = sum(v is None for v in self.abs_values)
                    if invalid:
                        self.warning.set(f"{invalid} invalid absorbance pixels excluded (weak reference or nonpositive sample minus dark).")
                except ValueError as exc:
                    self.abs_sample = self.abs_values = None
                    self.warning.set(str(exc))
            self.detail.set(f"{len(payload.intensities)} pixels • counts {min(payload.intensities):g}–{max(payload.intensities):g} • host call {payload.host_call_seconds * 1000:.1f} ms • completed {payload.finished_utc}")
            self.redraw()
        elif kind in ("error", "cleanup_error"):
            if kind == "cleanup_error":
                self.cleanup_failed = True
            self.warning.set(f"{kind.replace('_', ' ').title()}: {payload}")
            messagebox.showerror("Spectrometer error", payload)
        elif kind == "stopped":
            # SDK cleanup occurs before this event. Do not allow reconnect until
            # the thread has exited and every owner has relinquished its device.
            if self.worker:
                self.worker.join(timeout=0.1)
            self.worker = None
            self.identity = self.settings = None
            self.dark = self.reference = self.abs_sample = self.abs_values = None
            self.pending = self.stop_started = None
            self.status.set("Cleanup failed: restart the preview before reconnecting." if self.cleanup_failed else "Disconnected; settings restored and SDK closed.")
        self.update_buttons()

    def redraw(self):
        if self.display.get() == "absorbance" and self.abs_values is not None:
            self.plot.show([("Absorbance", "#7c3aed", self.abs_sample.wavelengths, self.abs_values)], "Absorbance (AU)")
        else:
            traces = [(label, color, spectrum.wavelengths, spectrum.intensities) for label, color, spectrum in [
                ("Live counts", "#2563eb", self.latest), ("Blocked dark", "#64748b", self.dark), ("Blank/reference", "#16a34a", self.reference)
            ] if spectrum is not None]
            self.plot.show(traces, "Raw intensity (counts)")

    def save(self):
        folder = filedialog.askdirectory(title="Save preview snapshot and calibration")
        if not folder:
            return
        try:
            destination = Path(folder) / ("preview_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ"))
            destination.mkdir()
            metadata = {"target_serial": self.serial, "sdk": str(self.sdk), "identity": asdict(self.identity) if self.identity else None,
                        "boxcar_convention": "half-width b; 2b+1 pixels", "timing_verified": False, "averaging_behavior_verified": False,
                        "irradiation_control": "disabled", "spectra": {}}
            for name, spectrum in [("latest", self.latest), ("dark", self.dark), ("reference", self.reference), ("absorbance_sample", self.abs_sample)]:
                if spectrum is None:
                    continue
                data = asdict(spectrum)
                data.pop("wavelengths")
                data.pop("intensities")
                metadata["spectra"][name] = data
                with (destination / f"{name}.csv").open("w", newline="", encoding="utf-8") as stream:
                    writer = csv.writer(stream)
                    writer.writerow(["wavelength_nm", "raw_intensity_counts"] + (["absorbance_AU"] if name == "absorbance_sample" else []))
                    for index, (wavelength, intensity) in enumerate(zip(spectrum.wavelengths, spectrum.intensities)):
                        writer.writerow([wavelength, intensity] + ([self.abs_values[index]] if name == "absorbance_sample" else []))
            (destination / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
            self.status.set(f"Saved {destination}")
        except OSError as exc:
            messagebox.showerror("Save failed", str(exc))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path, default=DEFAULT_SDK)
    parser.add_argument("--serial", default=DEFAULT_SERIAL)
    args = parser.parse_args()
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print("Tkinter could not start. Matching Tcl/Tk runtime resources are missing.\n"
              "See README.md; no device was accessed.\n" + str(exc), file=sys.stderr)
        return 1
    PreviewApp(root, args.sdk, args.serial)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
