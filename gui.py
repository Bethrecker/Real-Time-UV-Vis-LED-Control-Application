"""Tkinter/Matplotlib desktop experiment application; version 2, real hardware only."""
from bootstrap import configure
configure()
from dataclasses import asdict
from concurrent.futures import ThreadPoolExecutor
import math
import multiprocessing as mp
from pathlib import Path
import queue
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
from matplotlib import rcParams
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from acquisition import SpectrometerClient
from led import OutputApproval, validate_test_duration
from models import RunSettings, TimingBudget
from processing import usable_reference, saturation_mask
from control import validate_observed_read_budget
from session import Session
from led_session import LEDConnection
from spectrometer import DEFAULT_SDK, DEFAULT_SERIAL, Settings
from storage import recover_absorbance_excel, validate_experiment_name, DEFAULT_RUN_CACHE
from tooltip import Tooltip
from completion_sound import CompletionSound


PINK = {"background": "#FBE9EF", "surface": "#FFF5F8", "button": "#F4CFDC",
        "active": "#EDB9CD", "border": "#DEABC0", "text": "#422C36", "muted": "#927782"}
VERSION_CREDIT = "Version 2.1 Created by Elizabeth Recker & Lydia Hall, ZAP Group, UT Austin"


def apply_pink_theme(root):
    root.configure(background=PINK["background"])
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", background=PINK["background"], foreground=PINK["text"], font=("Segoe UI", 10),
                    bordercolor=PINK["border"], lightcolor=PINK["surface"], darkcolor=PINK["border"])
    style.configure("TButton", background=PINK["button"], padding=(10, 6))
    style.configure("Compact.TButton", padding=(10, 4))
    style.map("TButton", background=[("active", PINK["active"]), ("disabled", PINK["background"])],
              foreground=[("disabled", PINK["muted"])])
    style.configure("TEntry", fieldbackground=PINK["surface"], padding=4)
    style.map("TEntry", fieldbackground=[("disabled", PINK["background"])], foreground=[("disabled", PINK["muted"])])
    style.configure("TNotebook.Tab", background=PINK["button"], padding=(8, 7))
    style.map("TNotebook.Tab", background=[("selected", PINK["surface"]), ("active", PINK["active"])])
    for name in ("TCheckbutton", "TRadiobutton"):
        style.configure(name, indicatorbackground=PINK["surface"])
        style.map(name, background=[("active", PINK["background"])], foreground=[("disabled", PINK["muted"])],
                  indicatorbackground=[("selected", PINK["active"]), ("disabled", PINK["background"])])
    style.configure("Horizontal.TProgressbar", background=PINK["active"], troughcolor=PINK["surface"])
    style.configure("Description.TLabel", font=("Segoe UI", 9))
    style.configure("Header.TLabel", font=("Segoe UI", 18, "bold"))
    style.configure("Mode.TLabel", font=("Segoe UI", 11, "bold"))
    rcParams.update({"figure.facecolor": PINK["background"], "axes.facecolor": PINK["surface"],
                     "axes.edgecolor": PINK["border"], "axes.labelcolor": PINK["text"],
                     "xtick.color": PINK["text"], "ytick.color": PINK["text"], "text.color": PINK["text"]})


PARAMETER_HELP = {
    "on": "Requested duration of each finite irradiation pulse, in seconds. One pulse is issued per cycle. Host command acknowledgements do not measure optical pulse duration.",
    "off": "Requested interval after the LED Disable command acknowledges and before the next pulse. One SDK spectrum is collected in this interval. It must fit the estimated collection envelope, edge margins and lateness allowance.",
    "cycles": "Number of LED-on / LED-off pairs after the saved time-zero sample spectrum with irradiation off. Each completed cycle saves one averaged SDK spectrum and its absorbance. Total requested LED-on time is cycles multiplied by LED-on duration.",
    "integration": "Light-collection time per detector exposure, in milliseconds. Longer integration collects more light and takes longer; it can cause saturation. Update settings and recapture dark/reference after changes.",
    "scans": "Number of exposures used for each SDK-averaged spectrum. More scans increase the estimated acquisition time. OceanDirect performs averaging; Python does not average its result again. Numerical SDK averaging behavior remains unverified.",
    "boxcar": "SDK boxcar half-width b, in neighboring pixels on each side: 2b+1 pixels total. b=2 means five pixels; b=0 disables smoothing. OceanDirect performs smoothing; Python does not apply it again. Smoothing can hide clipping and narrow spectral features.",
    "readout": "Estimated detector-readout, USB-transfer and SDK overhead per scan, beyond integration time. Multiplied by scans to average. Increasing it reserves more acquisition time. This is an allowance, not a measured hardware delay.",
    "uncertainty": "Extra time allowance per scan for variation in acquisition/readout time. Multiplied by scans to average. Increasing it extends both the settling wait and the estimated collection envelope; a longer off-period may be needed.",
    "margin": "Time reserved near each end of the LED-off interval. The fit calculation reserves two margins, and acquisition must return before the trailing margin begins. Larger margins require a longer off-period. Optical LED-off timing remains unverified.",
    "lateness": "Tolerance for Windows/Python starting a scheduled action late. Exceeding this allowance stops further pulses. A larger value relaxes scheduling checks but does not allow acquisition beyond the off-period deadline or change the programmed finite pulse duration.",
    "command_budget": "Maximum wait for an experiment LED SDK command, such as preparation, On or Off, to acknowledge. Separate from scheduler lateness and pulse duration. Command overhead adds to wall-clock experiment time. A timeout stops further pulses and requests Disable.",
}


def duration(seconds):
    return f"{int(seconds)//60}:{int(seconds)%60:02d}"


class Application:
    def __init__(self, root, session_factory=None, led_factory=None, sound_factory=None):
        self.root = root
        self.session_factory = session_factory or Session
        self.led_factory = led_factory or LEDConnection
        self.events = queue.Queue()
        self.session = self.led_session = None
        self.led_pending = self.background_pending = False
        self.identity = self.applied = self.dark = self.reference = self.latest = None
        self.pending = self.running = self.closing = self.failed_shutdown = False
        self.led_testing = False
        self.completed = 0
        self.active_cycle = 0
        self.requested_cycles = None
        self.run_path = None
        self.run_cache_path = None
        self.manual_export_pending = False
        self.run_outcome = None
        self.run_error_reported = False
        self.completion_sound = (sound_factory or CompletionSound)()
        self.run_sound_played = False
        self.sound_pending = False
        self.abs_preview_line = None
        self.clock_end = None
        self.stop_time = None
        self.export_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="Manual export")
        root.title("Real-Time UV–Vis & LED Control")
        icon_path = Path(__file__).resolve().parent / "assets" / "pink-led.ico"
        if icon_path.is_file():
            root.iconbitmap(str(icon_path))
        root.geometry("1320x900")
        root.minsize(1050, 760)
        root.protocol("WM_DELETE_WINDOW", self.close)
        apply_pink_theme(root)
        outer = ttk.Frame(root, padding=8)
        outer.pack(fill="both", expand=True)
        title_row = ttk.Frame(outer)
        title_row.pack(fill="x")
        ttk.Label(title_row, text="Real-Time UV–Vis & LED Control", style="Header.TLabel").pack(side="left")
        header = ttk.Frame(outer)
        self.connection_bar = header
        header.pack(fill="x", pady=(4, 4))
        self.connect_button = ttk.Button(header, text="Connect QE-PRO", command=self.connect)
        self.connect_button.pack(side="left", padx=10)
        self.disconnect_button = ttk.Button(header, text="Disconnect QE-PRO", command=self.disconnect_spec)
        self.disconnect_button.pack(side="left")
        self.led_connect_button = ttk.Button(header, text="Connect LED", command=self.connect_led)
        self.led_connect_button.pack(side="left", padx=8)
        self.led_disconnect_button = ttk.Button(header, text="Disconnect LED", command=self.disconnect_led)
        self.led_disconnect_button.pack(side="left")
        self.experiment_controls = ttk.Frame(header)
        self.experiment_controls.pack(side="right")
        self.start_button = ttk.Button(self.experiment_controls, text="Start experiment", command=self.start)
        self.start_button.pack(side="left", padx=(0, 8))
        self.stop_button = ttk.Button(self.experiment_controls, text="STOP / LED OFF", command=self.stop)
        self.stop_button.pack(side="left")
        self.mode_status = tk.StringVar(value="REAL HARDWARE • Version 2.1")
        ttk.Label(title_row, textvariable=self.mode_status, style="Mode.TLabel").pack(side="right")
        new_run_row = ttk.Frame(outer)
        new_run_row.pack(fill="x", pady=(0, 2))
        self.new_run_button = ttk.Button(new_run_row, text="New Run", command=self.new_run)
        self.new_run_button.pack(side="right")
        self.device_status = tk.StringVar(value="Disconnected • QE-PRO QEP06321 • Mightex 04-210326-002")
        self.led_device_status = tk.StringVar(value="LED controller disconnected. Connection needs no channel or level; enabling output does.")
        self.storage_status = tk.StringVar(value="")
        self.status = tk.StringVar(value="")
        self.warning = tk.StringVar(value="")
        body = ttk.Panedwindow(outer, orient="horizontal")
        body.pack(fill="both", expand=True, pady=(2, 4))
        left, right = ttk.Frame(body, width=500), ttk.Frame(body)
        left.pack_propagate(False)
        body.add(left, weight=0)
        body.add(right, weight=1)
        self.status_panel = ttk.Frame(right)
        self.status_panel.pack(side="bottom", fill="x", pady=(4, 0))
        self.status_labels = []
        for variable in (self.device_status, self.led_device_status, self.storage_status, self.status, self.warning):
            label = ttk.Label(self.status_panel, textvariable=variable, wraplength=750, style="Description.TLabel")
            if variable is self.warning:
                label.configure(foreground="#a34805")
            label.pack(fill="x")
            self.status_labels.append(label)
        self.status_panel.bind("<Configure>", lambda event: [label.configure(wraplength=max(200, event.width-8)) for label in self.status_labels])
        self.variables = {name: tk.StringVar(value=value) for name, value in {
            "on": "1", "off": "1", "cycles": "100", "integration": "10", "scans": "3", "boxcar": "2",
            "channel": "", "level": "", "test_seconds": "1", "readout": "20", "uncertainty": "20", "margin": "20", "lateness": "200",
            "command_budget": "500", "name": "experiment", "folder": str(Path.home() / "Documents"),
        }.items()}
        self.input_widgets = []
        self.help_icons = {}
        self.tooltips = {}
        self.settings_tabs = ttk.Notebook(left)
        self.settings_tabs.pack(fill="both", expand=True)
        self.settings_pages = {}
        for title in ("Experiment", "Timing Estimates", "LED Settings", "Output"):
            panel = ttk.Frame(self.settings_tabs, padding=6)
            self.settings_tabs.add(panel, text=title)
            self.settings_pages[title] = panel
        experiment = self.settings_pages["Experiment"]
        basic = ttk.Frame(experiment)
        basic.pack(fill="x")
        timing = self.settings_pages["Timing Estimates"]
        led_settings = self.settings_pages["LED Settings"]
        file_settings = self.settings_pages["Output"]
        pulse_fields = ttk.Frame(basic)
        pulse_fields.grid(row=0, column=0, columnspan=2, sticky="ew")
        for column, (text, name) in enumerate((("LED on (s)", "on"), ("LED off (s)", "off"), ("Cycles", "cycles"))):
            self.make_entry(pulse_fields, text, name, 0, column=column*2, stacked=True)
        self.summary = tk.StringVar()
        ttk.Label(basic, textvariable=self.summary, wraplength=460, style="Description.TLabel").grid(row=3, column=0, columnspan=2, sticky="w", pady=4)
        self.make_entry(basic, "Integration (ms)", "integration", 4)
        self.make_entry(basic, "Scans to average", "scans", 5)
        self.make_entry(basic, "Boxcar half-width (pixels)", "boxcar", 6)
        ttk.Label(basic, text="OceanDirect averaging and smoothing; no repeated Python filtering.\nBoxcar half-width b: 2b+1 pixels total (b=2 means 5).", wraplength=460, style="Description.TLabel").grid(row=7, column=0, columnspan=2, sticky="w", pady=4)
        self.apply_button = ttk.Button(basic, text="Update spectrum settings", command=self.apply, style="Compact.TButton")
        self.apply_button.grid(row=8, column=0, columnspan=2, sticky="ew")
        self.make_entry(timing, "Readout budget / SDK scan (ms)", "readout", 0)
        self.make_entry(timing, "Uncertainty / SDK scan (ms)", "uncertainty", 1)
        self.make_entry(timing, "Off-edge margin (ms)", "margin", 2)
        self.make_entry(timing, "Allowed host lateness (ms)", "lateness", 3)
        self.make_entry(timing, "LED command budget (ms)", "command_budget", 4)
        ttk.Label(timing, text="These are conservative estimates, not measured optical timing. Two estimated averaging windows precede one SDK spectrum read. The full collection envelope is targeted at the middle of LED off. A missed deadline stops further pulses.", wraplength=350, justify="left").grid(row=5, column=0, columnspan=2, sticky="w", pady=10)
        self.budget_status = tk.StringVar()
        ttk.Label(timing, textvariable=self.budget_status, wraplength=350).grid(row=6, column=0, columnspan=2, sticky="w")
        output = ttk.LabelFrame(led_settings, text="Real irradiation output", padding=10)
        output.pack(fill="x", pady=8)
        self.make_entry(output, "Confirmed channel (1–4)", "channel", 0)
        self.make_entry(output, "Approved output level (%)", "level", 1)
        self.confirmed = tk.BooleanVar(value=False)
        self.confirm_button = ttk.Checkbutton(output, text="I confirm wiring, safe level, and\nno external trigger source on this channel", variable=self.confirmed, command=self.update)
        self.confirm_button.grid(row=2, column=0, columnspan=2, sticky="w", pady=6)
        ttk.Label(output, text="Percent of configured BLS-IO channel maximum voltage. It is not an LED-current or optical-power value. No maximum or safe level is guessed.", wraplength=350).grid(row=3, column=0, columnspan=2, sticky="w")
        test_output = ttk.LabelFrame(led_settings, text="LED intensity test", padding=10)
        test_output.pack(fill="x", pady=(8, 0))
        ttk.Label(test_output, text="Test duration (s):").grid(row=0, column=0, sticky="w")
        self.test_duration_entry = ttk.Entry(test_output, textvariable=self.variables["test_seconds"], width=8)
        self.test_duration_entry.grid(row=0, column=1, padx=6)
        self.led_test_button = ttk.Button(test_output, text="LED ON (timed test)", command=self.test_led)
        self.led_test_button.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 4))
        self.led_test_off_button = ttk.Button(test_output, text="LED OFF", command=self.stop_led_test)
        self.led_test_off_button.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        self.led_test_status = tk.StringVar(value="Connect LED independently, then enter channel/level for a timed test. QE-PRO is optional.")
        ttk.Label(test_output, textvariable=self.led_test_status, wraplength=420).grid(row=3, column=0, columnspan=2, sticky="w", pady=(5, 8))
        ttk.Label(test_output, text="Automatic off after 0.02-30 s; Off ends the test early. Testing invalidates dark/reference. Channel/level can be changed while the LED is idle.", wraplength=420).grid(row=4, column=0, columnspan=2, sticky="w")
        files = ttk.LabelFrame(file_settings, text="Output files", padding=10)
        files.pack(fill="x")
        self.make_entry(files, "Experiment name", "name", 0)
        self.folder_button = ttk.Button(files, text="Select folder…", command=self.select_folder)
        self.folder_button.grid(row=1, column=0, sticky="w", pady=6)
        ttk.Label(files, textvariable=self.variables["folder"], wraplength=230).grid(row=1, column=1, sticky="w")
        ttk.Label(files, text="Excel only: combined_absorbance.xlsx\nWavelength, time 0 (no irradiation), then completed cycles.", wraplength=420).grid(row=2, column=0, columnspan=2, sticky="w", pady=8)
        self.reexport_button = ttk.Button(files, text="Re-export a saved/interrupted run…", command=self.reexport)
        self.reexport_button.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        sounds = ttk.LabelFrame(file_settings, text="Run finished sound", padding=10)
        sounds.pack(fill="x", pady=(8, 0))
        self.sound_status = tk.StringVar(value="Saved MP3 ready." if self.completion_sound.available else "No MP3 selected.")
        self.sound_choose_button = ttk.Button(sounds, text="Choose MP3…", command=self.choose_completion_sound)
        self.sound_choose_button.pack(fill="x")
        sound_buttons = ttk.Frame(sounds)
        sound_buttons.pack(fill="x", pady=4)
        self.sound_preview_button = ttk.Button(sound_buttons, text="Preview sound", command=self.preview_completion_sound)
        self.sound_preview_button.pack(side="left", fill="x", expand=True)
        self.sound_remove_button = ttk.Button(sound_buttons, text="Remove sound", command=self.remove_completion_sound)
        self.sound_remove_button.pack(side="left", fill="x", expand=True, padx=(4, 0))
        ttk.Label(sounds, textvariable=self.sound_status, wraplength=420).pack(fill="x")
        ttk.Label(sounds, text="Choose once: a copy is remembered after restarting. Plays once whenever a run completes, stops, or fails.", wraplength=420, style="Description.TLabel").pack(fill="x", pady=(4, 0))
        setup = ttk.LabelFrame(experiment, text="Probe light and backgrounds", padding=6)
        setup.pack(fill="x", pady=(6, 0))
        ttk.Label(setup, text="With irradiation off: block probe light and capture dark.\nUnblock through blank; capture a usable reference before sample.", wraplength=460, style="Description.TLabel").pack(anchor="w")
        setup_row = ttk.Frame(setup)
        setup_row.pack(fill="x", pady=4)
        self.preview_enabled = tk.BooleanVar(value=True)
        self.preview_button = ttk.Checkbutton(setup_row, text="Live intensity preview", variable=self.preview_enabled, command=self.toggle_preview)
        self.preview_button.pack(anchor="w", pady=(0, 4))
        capture_row = ttk.Frame(setup_row)
        capture_row.pack(fill="x")
        self.dark_button = ttk.Button(capture_row, text="Capture dark", command=lambda: self.capture("dark"), style="Compact.TButton")
        self.dark_button.pack(side="left", padx=(0, 8))
        self.reference_button = ttk.Button(capture_row, text="Capture reference", command=lambda: self.capture("reference"), style="Compact.TButton")
        self.reference_button.pack(side="left")
        blank_row = ttk.Frame(setup)
        blank_row.pack(fill="x", pady=(0, 4))
        self.background_button = ttk.Button(blank_row, text="Background / new blank slide", command=self.background_blank, style="Compact.TButton")
        self.background_button.pack(fill="x")
        ttk.Label(blank_row, text="New sample: insert its blank slide; keep probe light on. Matching dark is retained.", wraplength=460, style="Description.TLabel").pack(anchor="w", pady=(3, 0))
        self.reference_status = tk.StringVar(value="Dark and reference not captured.")
        ttk.Label(setup, textvariable=self.reference_status, wraplength=460, style="Description.TLabel").pack(anchor="w")
        self.tabs = ttk.Notebook(right)
        self.tabs.pack(fill="both", expand=True)
        raw_frame, abs_frame = ttk.Frame(self.tabs), ttk.Frame(self.tabs)
        self.tabs.add(raw_frame, text="Live SDK intensity")
        self.tabs.add(abs_frame, text="Live / cycle absorbance")
        self.tabs.bind("<<NotebookTabChanged>>", lambda event: self.toggle_preview())
        self.raw_axes, self.raw_canvas = self.make_plot(raw_frame, "Intensity (counts)")
        self.abs_axes, self.abs_canvas = self.make_plot(abs_frame, "Absorbance (AU)")
        self.abs_lines = []
        self.abs_status = tk.StringVar(value="Capture a usable dark and blank/reference, then select this tab for live absorbance. No LED pulse is needed.")
        abs_status_label = ttk.Label(abs_frame, textvariable=self.abs_status, wraplength=800)
        abs_status_label.pack(side="bottom", fill="x", before=self.abs_canvas.get_tk_widget())
        abs_frame.bind("<Configure>", lambda event: abs_status_label.configure(wraplength=max(200, event.width-8)))
        self.raw_status = tk.StringVar(value="No spectrum acquired.")
        raw_status_label = ttk.Label(raw_frame, textvariable=self.raw_status, wraplength=750)
        raw_status_label.pack(side="bottom", fill="x", pady=(4, 0), before=self.raw_canvas.get_tk_widget())
        raw_frame.bind("<Configure>", lambda event: raw_status_label.configure(wraplength=max(200, event.width-8)))
        footer = ttk.Frame(outer)
        footer.pack(side="bottom", fill="x", before=body)
        self.progress = ttk.Progressbar(footer, mode="determinate", maximum=100)
        self.progress.pack(side="left", fill="x", expand=True, padx=10)
        self.progress_text = tk.StringVar(value="Cycle 0 • Remaining —")
        ttk.Label(footer, textvariable=self.progress_text, width=52).pack(side="left")
        self.credit_label = ttk.Label(outer, text=VERSION_CREDIT, anchor="center")
        self.credit_label.pack(side="bottom", fill="x", pady=(6, 0), before=footer)
        for variable in self.variables.values():
            variable.trace_add("write", lambda *args: self.update())
        self.update()
        self.poll_id = root.after(100, self.poll)

    def make_entry(self, frame, text, name, row, column=0, stacked=False):
        label = ttk.Frame(frame)
        label.grid(row=row, column=column, sticky="w", pady=3, padx=(0, 8) if stacked else 0)
        ttk.Label(label, text=text).pack(side="left")
        if name in PARAMETER_HELP:
            icon = ttk.Label(label, text="ⓘ", foreground="#2563eb", cursor="hand2", takefocus=True)
            icon.pack(side="left", padx=(5, 0))
            self.help_icons[name] = icon
            self.tooltips[name] = Tooltip(icon, PARAMETER_HELP[name])
        entry = ttk.Entry(frame, textvariable=self.variables[name], width=10 if stacked else 15)
        entry.grid(row=row+1 if stacked else row, column=column if stacked else column+1,
                   sticky="ew", padx=(0, 16) if stacked else (12, 0), pady=3)
        self.input_widgets.append((name, entry))

    def make_plot(self, parent, label):
        figure = Figure(figsize=(7, 4), dpi=100, layout="constrained")
        axes = figure.add_subplot()
        axes.set_xlabel("Wavelength (nm)")
        axes.set_ylabel(label)
        axes.grid(alpha=0.2)
        canvas = FigureCanvasTkAgg(figure, parent)
        canvas.get_tk_widget().configure(background=PINK["background"], highlightthickness=0)
        canvas.get_tk_widget().pack(fill="both", expand=True)
        toolbar = NavigationToolbar2Tk(canvas, parent)
        toolbar.pack_configure(side="bottom", before=canvas.get_tk_widget())
        def color_toolbar(widget):
            options = widget.keys()
            palette = {"background": PINK["background"], "foreground": PINK["text"],
                       "activebackground": PINK["button"], "activeforeground": PINK["text"],
                       "selectcolor": PINK["surface"], "highlightbackground": PINK["background"]}
            widget.configure(**{key: value for key, value in palette.items() if key in options})
            for child in widget.winfo_children():
                color_toolbar(child)
        color_toolbar(toolbar)
        toolbar.update()
        return axes, canvas

    def preview_settings(self):
        v = self.variables
        ms = float(v["integration"].get())
        if not math.isfinite(ms) or not math.isclose(ms*1000, round(ms*1000), abs_tol=1e-6, rel_tol=0):
            raise ValueError("Integration must use whole microseconds.")
        acquisition = Settings(round(ms*1000), int(v["scans"].get()), int(v["boxcar"].get()))
        if not 1 <= acquisition.scans <= 100 or not 0 <= acquisition.boxcar <= 50:
            raise ValueError("Scans must be 1-100 and boxcar half-width 0-50.")
        minimum = self.identity.minimum_us if self.identity else 10000
        maximum = min(self.identity.maximum_us, 5000000) if self.identity else 5000000
        if not minimum <= acquisition.integration_us <= maximum:
            raise ValueError(f"Integration must be {minimum/1000:g}-{maximum/1000:g} ms.")
        timing = TimingBudget(*(float(v[name].get())/1000 for name in ["readout", "uncertainty", "margin", "lateness", "command_budget"]))
        timing.validate()
        if acquisition.integration_us * acquisition.scans > 5000000:
            raise ValueError("Integration times scans must be at most 5 seconds for SDK preview.")
        return RunSettings(acquisition=acquisition, timing=timing)

    def settings(self):
        preview = self.preview_settings()
        v = self.variables
        result = RunSettings(float(v["on"].get()), float(v["off"].get()), int(v["cycles"].get()), preview.acquisition, preview.timing, preview.preview_processing)
        result.validate()
        if result.cycles > 16382:
            raise ValueError("Excel supports at most 16,382 irradiation cycles plus time zero per run.")
        return result

    def backgrounds_match(self, settings):
        return self.applied is not None and settings.acquisition == self.applied.acquisition and settings.timing == self.applied.timing and settings.preview_processing == self.applied.preview_processing

    def approval(self):
        level = float(self.variables["level"].get())
        if not math.isfinite(level) or not math.isclose(level*10, round(level*10), abs_tol=1e-7, rel_tol=0):
            raise ValueError("Output level uses 0.1% steps.")
        result = OutputApproval(int(self.variables["channel"].get()), round(level*10), self.confirmed.get())
        result.validate()
        return result

    def update(self):
        try:
            settings = self.settings()
            self.summary.set(f"Total LED-on: {settings.led_on_s:g} s; on/off total: {settings.duration_s:g} s\nEstimated duration with LED command allowances: {settings.estimated_duration_s:g} s")
            self.budget_status.set(f"Estimated collection envelope: {settings.timing.collection_window(settings.acquisition)*1000:.0f} ms\nOff-period: {settings.off_s*1000:g} ms; edge margins: {settings.timing.edge_margin_s*1000:g} ms each.")
        except (ValueError, OverflowError) as exc:
            settings = None
            self.summary.set("Experiment settings invalid: " + str(exc))
            self.budget_status.set(str(exc))
        try:
            preview_settings = self.preview_settings()
        except (ValueError, OverflowError):
            preview_settings = None
        try:
            self.approval()
            approval_ok = True
        except (ValueError, OverflowError):
            approval_ok = False
        connected = self.session is not None and self.identity is not None and self.stop_time is None
        led_connected = self.led_session is not None and self.led_session.ready.is_set() and not self.led_pending and self.stop_time is None
        ready = connected and not self.pending and not self.running and not self.led_testing
        led_ready = led_connected and not self.running and not self.pending and not self.led_testing and not self.background_pending
        matching = preview_settings is not None and self.backgrounds_match(preview_settings)
        if not matching and self.abs_preview_line is not None:
            self.invalidate_absorbance_preview()
        reference_check = usable_reference(self.dark, self.reference, self.identity.maximum_intensity if self.identity else 200000)
        self.reference_status.set(reference_check.message + (" Apply acquisition edits before capture/start." if connected and not matching else ""))
        def enable(widget, value):
            widget.configure(state="normal" if value else "disabled")
        enable(self.connect_button, self.session is None and preview_settings is not None and not self.closing and self.stop_time is None)
        enable(self.disconnect_button, self.session is not None and not self.running and not self.pending and self.stop_time is None)
        enable(self.led_connect_button, self.led_session is None and not self.closing and not self.failed_shutdown and self.stop_time is None)
        enable(self.led_disconnect_button, self.led_session is not None and not self.led_pending and not self.running and self.stop_time is None)
        enable(self.stop_button, (self.session is not None or self.led_session is not None) and self.stop_time is None)
        enable(self.start_button, ready and led_ready and settings is not None and matching and reference_check.usable and approval_ok)
        enable(self.new_run_button, self.can_new_run())
        enable(self.reexport_button, not self.running and not self.pending and not self.manual_export_pending
               and not (self.session is not None and self.session.experiment_started))
        try:
            test_duration_ok = bool(validate_test_duration(float(self.variables["test_seconds"].get())))
        except (ValueError, OverflowError):
            test_duration_ok = False
        enable(self.led_test_button, led_ready and approval_ok and test_duration_ok)
        enable(self.led_test_off_button, led_connected)
        enable(self.test_duration_entry, not self.led_testing and not self.running and not self.led_pending and self.stop_time is None)
        enable(self.apply_button, ready and preview_settings is not None)
        enable(self.dark_button, ready and matching and not self.led_pending)
        enable(self.reference_button, ready and matching and self.dark is not None and not self.led_pending)
        enable(self.background_button, ready and matching and self.dark is not None and not self.led_pending)
        enable(self.preview_button, connected and not self.pending and not self.running)
        enable(self.confirm_button, not self.running and not self.led_testing and not self.led_pending)
        enable(self.folder_button, not self.running and not self.pending)
        sound_idle = not self.running and not self.pending and not self.closing and not self.sound_pending
        enable(self.sound_choose_button, sound_idle)
        enable(self.sound_preview_button, sound_idle and self.completion_sound.available)
        enable(self.sound_remove_button, sound_idle and self.completion_sound.available)
        for name, entry in self.input_widgets:
            enabled = not (self.running or self.pending or self.stop_time or self.led_testing)
            if name in ("channel", "level"):
                enabled = not self.running and not self.led_testing and not self.led_pending and self.stop_time is None
            enable(entry, enabled)


    def connect(self):
        if self.session or self.closing:
            return
        try:
            settings = self.preview_settings()
        except (ValueError, OverflowError) as exc:
            messagebox.showerror("Invalid spectrometer settings", str(exc))
            return
        self.dark = self.reference = self.latest = None
        self.pending = True
        self.status.set("Connecting QE-PRO only; the LED connection is independent.")
        self.session = self.session_factory(False, DEFAULT_SDK, DEFAULT_SERIAL, None, settings, self.events, led_connection=self.led_session)
        self.session.start()
        self.update()

    def connect_led(self):
        if self.led_session or self.closing or self.failed_shutdown:
            return
        self.led_pending = True
        self.led_session = self.led_factory(False, self.events)
        if self.session:
            self.session.led_connection = self.led_session
        self.led_device_status.set("Connecting LED controller; Disable requested on all four outputs. No channel/level inputs are required.")
        self.led_session.start()
        self.update()

    def disconnect_spec(self):
        if self.session and not self.running:
            self.pending = True
            self.session.halt()
            self.status.set("Disconnecting QE-PRO; LED control remains independent.")
            self.update()

    def disconnect_led(self):
        if self.led_session and not self.running:
            self.led_pending = True
            self.led_session.halt()
            self.led_device_status.set("Disconnecting LED; output Disable requested.")
            self.update()

    def toggle_preview(self):
        if self.session and not self.running:
            self.session.commands.put(("preview", (self.preview_enabled.get(), None, self.tabs.index(self.tabs.select()) == 1)))

    def apply(self):
        if self.led_testing:
            return
        try:
            settings = self.preview_settings()
        except (ValueError, OverflowError) as exc:
            messagebox.showerror("Invalid settings", str(exc))
            return
        self.pending = True
        self.dark = self.reference = self.latest = None
        self.invalidate_absorbance_preview()
        self.raw_axes.clear()
        self.raw_axes.set(xlabel="Wavelength (nm)", ylabel="SDK intensity (counts)")
        self.raw_canvas.draw_idle()
        self.raw_status.set("Updating parameters; waiting for a new SDK-processed intensity spectrum.")
        self.session.commands.put(("apply", settings))
        self.status.set("Updating SDK preview settings; dark/reference invalidated. Waiting for the new spectrum.")
        self.update()

    def capture(self, kind):
        if self.led_testing:
            return
        self.pending = self.background_pending = True
        self.invalidate_absorbance_preview()
        self.session.commands.put((kind, None))
        self.status.set(f"Capturing {kind} with irradiation disabled…")
        self.update()

    def background_blank(self):
        if not self.session or not self.identity or self.pending or self.running or self.led_testing or self.led_pending or self.stop_time is not None:
            return
        try:
            settings = self.preview_settings()
            if self.dark is None or not self.backgrounds_match(settings):
                raise ValueError("Capture a matching dark first. After setting changes, click Update and recapture dark before the new blank.")
        except (ValueError, OverflowError) as exc:
            messagebox.showerror("Cannot capture new blank", str(exc))
            return
        self.capture("blank")
        self.status.set("Capturing a new blank slide with irradiation off; matching dark retained. Keep the probe light on through the blank, then load the sample after capture.")

    def test_led(self):
        if not self.led_session or self.led_pending or self.background_pending or self.running or self.led_testing or self.stop_time is not None:
            return
        try:
            approval = self.approval()
            seconds = validate_test_duration(float(self.variables["test_seconds"].get()))
            self.led_session.test_led(seconds, approval)
        except (ValueError, OverflowError) as exc:
            messagebox.showerror("Cannot test LED", str(exc))
            return
        self.led_testing = True
        self.dark = self.reference = None
        self.invalidate_absorbance_preview()
        if self.session:
            self.session.invalidate_backgrounds()
        self.led_test_status.set(f"REAL LED test preparing: {seconds:g} s at entered level.")
        self.status.set("LED test started independently of QE-PRO. Recapture backgrounds before an experiment.")
        self.update()

    def stop_led_test(self):
        if self.running:
            self.stop()
        elif self.led_session:
            self.led_session.stop_led_test()
            self.led_test_status.set("LED OFF requested independently of spectrometer acquisition; waiting for Disable acknowledgement.")

    def select_folder(self):
        folder = filedialog.askdirectory(title="Choose experiment output folder")
        if folder:
            self.variables["folder"].set(folder)

    def can_new_run(self):
        return not (self.running or self.pending or self.background_pending or self.led_pending or self.led_testing
                    or self.stop_time is not None or self.closing or self.failed_shutdown or self.manual_export_pending
                    or (self.session is not None and self.session.experiment_started))

    def sound_result(self, future, event):
        # Audio workers never touch Tk widgets or hardware control.
        try:
            value = future.result()
        except Exception as exc:
            self.events.put(("sound_error", str(exc)))
        else:
            if event:
                self.events.put((event, value))

    def choose_completion_sound(self):
        if self.running or self.pending or self.closing or self.sound_pending:
            return
        selected = filedialog.askopenfilename(title="Choose the run finished MP3", filetypes=[("MP3 audio", "*.mp3")], parent=self.root)
        if selected:
            self.sound_pending = True
            self.completion_sound.install(selected).add_done_callback(lambda result: self.sound_result(result, "sound_selected"))
            self.update()

    def preview_completion_sound(self):
        if not (self.running or self.pending or self.closing or self.sound_pending):
            self.completion_sound.play().add_done_callback(lambda result: self.sound_result(result, None))

    def remove_completion_sound(self):
        if not (self.running or self.pending or self.closing or self.sound_pending):
            self.sound_pending = True
            self.completion_sound.remove().add_done_callback(lambda result: self.sound_result(result, "sound_removed"))
            self.update()

    def notify_run_finished(self):
        if not self.run_sound_played:
            self.run_sound_played = True
            if not self.closing:
                self.completion_sound.play().add_done_callback(lambda result: self.sound_result(result, None))

    def new_run(self):
        if not self.can_new_run():
            return
        previous = self.variables["name"].get()
        proposed = previous
        while True:
            name = simpledialog.askstring("New Run", "Enter a different experiment name for the new run:",
                                          initialvalue=proposed, parent=self.root)
            if name is None:
                return
            name = name.strip()
            try:
                validate_experiment_name(name)
                if name.casefold() == previous.casefold():
                    raise ValueError("Choose a different experiment name for the new run.")
            except ValueError as exc:
                messagebox.showerror("Invalid experiment name", str(exc), parent=self.root)
                proposed = name
                continue
            break
        self.variables["name"].set(name)
        self.run_sound_played = False
        self.completed = self.active_cycle = 0
        self.requested_cycles = self.clock_end = None
        self.run_path = self.run_cache_path = self.run_outcome = None
        self.run_error_reported = False
        self.latest = None
        self.progress.configure(value=0)
        self.progress_text.set("Cycle 0 • Remaining —")
        self.abs_lines.clear()
        self.abs_preview_line = None
        for axes, canvas, label in ((self.raw_axes, self.raw_canvas, "SDK intensity (counts)"),
                                    (self.abs_axes, self.abs_canvas, "Absorbance (AU)")):
            axes.clear()
            axes.set(xlabel="Wavelength (nm)", ylabel=label)
            axes.grid(alpha=0.2)
            canvas.draw_idle()
        self.raw_status.set("Waiting for a new spectrum.")
        self.abs_status.set("New run ready. Capture matching backgrounds if needed, then start when ready.")
        self.storage_status.set("")
        self.warning.set("")
        self.status.set(f"New run: {name}. Settings retained; previous saved files are preserved.")
        self.update()

    def start(self):
        if self.led_testing:
            return
        try:
            settings = self.settings()
            if not self.backgrounds_match(settings):
                raise ValueError("Apply acquisition edits and recapture backgrounds before Start.")
            if not self.session or not self.identity or not self.led_session or not self.led_session.ready.is_set() or self.led_pending:
                raise ValueError("Connect both QE-PRO and LED before starting an experiment.")
            approval = self.approval()
            if not usable_reference(self.dark, self.reference, self.identity.maximum_intensity).usable:
                raise ValueError("Reference unusable; unblock probe light and recapture.")
            validate_observed_read_budget(settings, self.dark, self.reference)
            folder, name = self.variables["folder"].get(), self.variables["name"].get()
            if not Path(folder).is_dir():
                raise ValueError("Select an existing output folder.")
            validate_experiment_name(name)
            if settings.cycles > 16382:
                raise ValueError("Excel supports at most 16,382 irradiation cycles plus time zero per run.")
        except (ValueError, OverflowError) as exc:
            messagebox.showerror("Cannot start", str(exc))
            return
        self.running, self.pending = True, True
        self.run_sound_played = False
        self.completed = self.active_cycle = 0
        self.requested_cycles = settings.cycles
        self.run_outcome = None
        self.run_error_reported = False
        self.clock_end = time.monotonic() + settings.estimated_duration_s
        self.progress.configure(maximum=settings.cycles, value=0)
        self.abs_axes.clear()
        self.abs_axes.set(xlabel="Wavelength (nm)", ylabel="Absorbance (AU)")
        self.abs_axes.grid(alpha=0.2)
        self.abs_lines.clear()
        self.abs_preview_line = None
        self.abs_status.set("Collecting time 0 with irradiation off before the first pulse. Each saved measurement adds a trace.")
        self.abs_canvas.draw_idle()
        self.tabs.select(1)
        self.session.commands.put(("start", (folder, name, True, approval, settings)))
        self.status.set("Preparing durable run folder and finite single-pulse profile…")
        self.update()

    def stop(self):
        if (self.session or self.led_session) and self.stop_time is None:
            self.stop_time = time.monotonic()
            # LED shutdown is submitted first, independent of the spectrometer.
            if self.led_session:
                self.led_session.halt()
            if self.session:
                self.session.halt()
            self.status.set("STOP requested; further pulses inhibited; devices disconnect independently.")
            self.update()

    def close(self):
        if self.failed_shutdown:
            messagebox.showerror("Shutdown not confirmed", "LED/device shutdown failed. Check the physical LED state and reported error before exiting.")
            self.failed_shutdown = False
        self.closing = True
        if self.session or self.led_session:
            self.stop()
        else:
            self.export_worker.shutdown(wait=False)
            self.destroy_window()

    def destroy_window(self):
        self.completion_sound.close()
        for tooltip in self.tooltips.values():
            tooltip.hide()
        if self.poll_id:
            self.root.after_cancel(self.poll_id)
            self.poll_id = None
        for canvas in (self.raw_canvas, self.abs_canvas):
            callback = getattr(canvas, "_idle_draw_id", None)
            if callback:
                canvas.get_tk_widget().after_cancel(callback)
                canvas._idle_draw_id = None
        self.root.destroy()

    def reexport(self):
        if self.running or self.pending or self.manual_export_pending or (self.session is not None and self.session.experiment_started):
            return
        folder = filedialog.askdirectory(title="Select saved run or internal recovery folder",
                                          initialdir=str(DEFAULT_RUN_CACHE if DEFAULT_RUN_CACHE.is_dir() else Path(self.variables["folder"].get())))
        if not folder:
            return
        self.manual_export_pending = True
        future = self.export_worker.submit(recover_absorbance_excel, Path(folder))
        self.status.set("Combined export running in background; hardware timing is independent.")
        def completed(result):
            try:
                self.events.put(("manual_export", result.result()))
            except Exception as exc:
                self.events.put(("manual_export_error", f"Export failed: {exc}"))
        future.add_done_callback(completed)
        self.update()

    def draw_raw(self, capture):
        self.latest = capture
        processed_preview = capture.provenance.get("strategy") == "sdk_processed_preview"
        self.raw_axes.clear()
        data = [("Live SDK intensity" if processed_preview else "SDK measurement intensity", "#2563eb", capture)]
        if not processed_preview:
            data.extend([("SDK blocked dark", "#64748b", self.dark), ("SDK blank reference", "#16a34a", self.reference)])
        for label, color, spectrum in data:
            if spectrum:
                self.raw_axes.plot(spectrum.spectrum.wavelengths, spectrum.spectrum.intensities, color=color, linewidth=1.2, label=label)
        self.raw_axes.set(xlabel="Wavelength (nm)", ylabel="SDK intensity (counts)")
        self.raw_axes.grid(alpha=0.2)
        self.raw_axes.legend(loc="upper right", fontsize=8)
        self.raw_canvas.draw_idle()
        spectrum = capture.spectrum
        clipped = sum(saturation_mask(capture, self.identity.maximum_intensity))
        if processed_preview:
            p = capture.provenance
            self.raw_status.set(f"SDK preview: {spectrum.settings.integration_us/1000:g} ms; average {p['sdk_scans_to_average']}; boxcar {p['sdk_boxcar_width']}; electric dark {'on' if p['electric_dark'] else 'off'}; nonlinearity {'on' if p['nonlinearity'] else 'off'}. {min(spectrum.intensities):.1f}-{max(spectrum.intensities):.1f} counts. SDK backgrounds are separate; smoothing can hide clipping.")
        else:
            self.raw_status.set(f"SDK measurement: {spectrum.settings.integration_us/1000:g} ms; average {spectrum.settings.scans}; boxcar {spectrum.settings.boxcar}; {len(spectrum.intensities)} pixels; {min(spectrum.intensities):.1f}-{max(spectrum.intensities):.1f} counts. SDK result preserved; individual source scans unavailable; smoothing may hide clipping.")
        if clipped:
            label = "SDK preview output" if processed_preview else "SDK measurement pixels"
            self.warning.set(f"SATURATION: {clipped} {label} reached SDK maximum. Reduce integration and recapture backgrounds.")
        elif self.warning.get().startswith("SATURATION:"):
            self.warning.set("Displayed data is below SDK maximum; individual source-scan saturation cannot be verified after SDK averaging/smoothing.")

    def draw_absorbance_preview(self, capture, result):
        if self.abs_preview_line is not None:
            self.abs_preview_line.remove()
        self.abs_preview_line, = self.abs_axes.plot(capture.spectrum.wavelengths, [math.nan if v is None else v for v in result.absorbance], color="#16a34a", linestyle="--", linewidth=1.5, label="Live preview (not a saved cycle)")
        self.abs_axes.relim()
        self.abs_axes.autoscale_view()
        self.abs_axes.legend(loc="upper right", fontsize=8)
        self.abs_canvas.draw_idle()
        invalid = sum(v is None for v in result.absorbance)
        self.abs_status.set(f"Live absorbance preview; not saved as a cycle. {len(result.absorbance)-invalid} valid pixels; {invalid} invalid/saturated pixels. Experiment traces appear after each completed off-period.")

    def invalidate_absorbance_preview(self):
        if self.abs_preview_line is not None:
            self.abs_preview_line.remove()
            self.abs_preview_line = None
            if self.abs_lines:
                self.abs_axes.legend(handles=[self.abs_lines[-1]], loc="upper right", fontsize=8)
            elif self.abs_axes.get_legend() is not None:
                self.abs_axes.get_legend().remove()
            self.abs_canvas.draw_idle()
        self.abs_status.set("Live absorbance requires matching dark and usable blank/reference. Completed cycle traces are retained.")

    def handle(self, kind, value):
        if kind == "connected":
            self.identity, _, self.applied_acquisition = value
            self.applied = self.session.settings
            self.pending = False
            self.device_status.set(f"QE-PRO: {self.identity.model} / {self.identity.serial} - OceanDirect {'.'.join(map(str, self.identity.api_version))}")
            self.status.set("QE-PRO connected. Displaying OceanDirect SDK intensity with standard corrections. Compare with OceanView intensity/counts using the same settings.")
            if self.identity.buffer_policy and self.identity.buffer_policy.get("mode") == "qepro_single_slot":
                self.warning.set("QE-PRO retains one spectrum; buffer-enable query is unsupported. Capacity is rechecked before each read. Exposure freshness and optical timing remain unverified.")
            self.toggle_preview()
        elif kind == "led_connected":
            self.led_pending = False
            self.led_device_status.set(value + "; channel/level are entered when enabling output.")
            self.led_test_status.set("LED connected independently. Enter output settings for a timed test; QE-PRO is optional.")
        elif kind == "led_error":
            self.warning.set(value)
            self.led_device_status.set("LED error: " + value)
        elif kind == "led_disconnected":
            old_connection = self.led_session
            self.led_session = None
            self.led_pending = self.led_testing = False
            if self.session and self.session.led_connection is old_connection and not self.session.experiment_started:
                self.session.led_connection = None
            self.led_device_status.set("LED disconnected; Disable requested before connection closure.")
            if value:
                self.failed_shutdown = self.failed_shutdown or any("Disable" in text or "OFF NOT CONFIRMED" in text or "shutdown" in text.lower() or "CloseDevice" in text for text in value)
                self.warning.set("LED connection/shutdown errors: " + "; ".join(value))
                self.closing = False
                messagebox.showerror("LED connection/shutdown errors", "\n".join(value))
            if self.session is None:
                self.stop_time = None
        elif kind == "led_test_started":
            self.led_test_status.set(f"REAL LED ON request acknowledged; automatic Disable scheduled after {value:g} s. Optical state is not measured.")
        elif kind == "led_test_off":
            self.led_test_status.set(value)
            if "failed" in value.lower():
                self.warning.set(value)
                self.failed_shutdown = True
        elif kind == "led_test_finished":
            self.led_testing = False
            if value:
                self.led_test_status.set(value)
                self.warning.set(value)
                if "OFF NOT CONFIRMED" in value:
                    self.failed_shutdown = True
                # Preserve the gate until shutdown is complete.
                self.led_pending = True
            else:
                self.led_test_status.set("Test ended; Disable acknowledged. Recapture dark and blank/reference before the experiment.")
                if self.stop_time is None:
                    self.status.set("LED test complete. Block probe and capture dark, then capture an unblocked blank/reference.")
        elif kind == "applied":
            self.applied, self.pending = value, False
            self.latest = None
            self.raw_axes.clear()
            self.raw_axes.set(xlabel="Wavelength (nm)", ylabel="SDK intensity (counts)")
            self.raw_canvas.draw_idle()
            self.status.set("Settings updated. All spectra use the selected SDK acquisition settings and standard corrections. Recapture dark/reference before the experiment.")
        elif kind in ("preview", "dark", "reference"):
            if kind == "preview":
                if self.pending or self.applied is None:
                    return
                if value.provenance.get("preview_processing") != asdict(self.applied.preview_processing):
                    return
            if self.stop_time is not None or not self.identity or value.spectrum.settings != self.applied.acquisition:
                return
            if kind == "dark":
                self.dark, self.reference = value, None
                self.invalidate_absorbance_preview()
            elif kind == "reference":
                self.reference = value
            if kind != "preview":
                self.pending = self.background_pending = False
                off_status = "LED Disable acknowledged" if self.led_session else "LED disconnected; confirm irradiation is physically off"
                self.status.set(f"{kind.title()} captured; {off_status}.")
            self.draw_raw(value)
            if kind == "reference" and value.provenance.get("reference_role") == "new_blank_slide":
                check = usable_reference(self.dark, self.reference, self.identity.maximum_intensity)
                if check.usable:
                    self.invalidate_absorbance_preview()
                    self.abs_axes.clear()
                    self.abs_axes.set(xlabel="Wavelength (nm)", ylabel="Absorbance (AU)")
                    self.abs_axes.grid(alpha=0.2)
                    self.abs_lines.clear()
                    self.abs_canvas.draw_idle()
                    self.abs_status.set("New blank baseline captured. Waiting for a fresh live spectrum; this blank should read near zero in usable pixels.")
                    self.preview_enabled.set(True)
                    self.tabs.select(1)
                    self.toggle_preview()
                    self.status.set(f"New blank slide captured; matching dark retained; {off_status}. Check its near-zero live absorbance, then load the sample.")
                else:
                    self.warning.set("New blank unusable: " + check.message)
                    self.status.set("New blank failed reference validation. Check probe light/slide and recapture before loading the sample.")
        elif kind == "absorbance_preview":
            capture, result, dark, reference = value
            if self.running or self.pending or self.stop_time is not None or not self.identity or not self.applied:
                return
            if capture.spectrum.settings != self.applied.acquisition or dark != self.dark or reference != self.reference:
                return
            try:
                matching = self.backgrounds_match(self.preview_settings())
            except (ValueError, OverflowError):
                matching = False
            if not matching:
                self.invalidate_absorbance_preview()
                return
            self.draw_raw(capture)
            self.draw_absorbance_preview(capture, result)
        elif kind == "phase":
            cycle, phase, remaining = value
            self.active_cycle = cycle
            self.status.set(f"Cycle {cycle}: {phase}")
            self.clock_end = time.monotonic() + remaining
            self.pending = False
        elif kind in ("cycle", "baseline"):
            cycle, capture, result, timing = value
            label = "Time 0 (no irradiation)" if kind == "baseline" else f"Cycle {cycle}"
            self.completed = cycle
            self.progress.configure(value=cycle)
            for line in self.abs_lines:
                line.set(color="#94a3b8", alpha=0.35, linewidth=0.8)
            line, = self.abs_axes.plot(capture.spectrum.wavelengths, [math.nan if v is None else v for v in result.absorbance], color="#7c3aed", linewidth=2, label=label)
            self.abs_lines.append(line)
            self.abs_axes.relim()
            self.abs_axes.autoscale_view()
            self.abs_status.set(f"{label}: saved absorbance using SDK average {capture.spectrum.settings.scans}, boxcar {capture.spectrum.settings.boxcar}. LED timing is host-estimated.")
            self.abs_axes.legend(handles=[line], loc="upper right", fontsize=8)
            self.abs_canvas.draw_idle()
            invalid = sum(flag != "valid" for flag in result.flags)
            self.warning.set(f"Cycle {cycle}: {invalid} invalid absorbance pixels flagged; SDK intensities preserved." if invalid else "Host schedule within configured budgets; optical timing and detector freshness remain unverified.")
        elif kind == "storage_cache":
            cache, selected = value
            self.run_cache_path = cache
            self.storage_status.set("Output: " + str(Path(selected)/"combined_absorbance.xlsx"))
        elif kind == "run_folder":
            self.run_path = value
            self.storage_status.set("Output: " + value)
            self.status.set("Saving incrementally to " + value)
        elif kind == "run_finished":
            status, error, completed = value
            self.running = False
            self.pending = True
            self.run_outcome = (status, error, completed)
            self.clock_end = None
            self.abs_status.set(f"Run {status}: {completed} completed spectra. " + (error or "Completed cycle traces remain displayed."))
            self.status.set(f"Run {status}; {completed} completed cycles. LED off; finalizing saved data.")
            if error:
                self.warning.set(error)
            self.notify_run_finished()
        elif kind == "sound_selected":
            self.sound_pending = False
            self.sound_status.set(f"Saved MP3: {value}")
        elif kind == "sound_removed":
            self.sound_pending = False
            self.sound_status.set("No MP3 selected.")
        elif kind == "sound_error":
            self.sound_pending = False
            self.sound_status.set("Sound unavailable: " + value)
        elif kind == "led_off":
            if "failed" in value.lower():
                self.warning.set(value)
                self.failed_shutdown = True
        elif kind == "error":
            self.warning.set(value)
            self.status.set("QE-PRO/experiment error; spectrometer shutdown requested. LED has separate connection status.")
        elif kind == "exporting":
            self.status.set("LED off; combined absorbance Excel export running in background…")
        elif kind == "run_ready":
            self.pending = False
            self.status.set(f"Run complete. Saved: {self.run_path}. QE-PRO and LED remain connected; LED off. Select New Run for the next slide.")
        elif kind in ("exported", "manual_export"):
            if kind == "manual_export":
                self.manual_export_pending = False
            if kind == "exported" and self.run_outcome:
                status, error, completed = self.run_outcome
                self.status.set(f"Run {status}; {completed} completed cycles. Saved: {value}")
                if error:
                    self.warning.set(error)
            else:
                self.status.set("Saved: " + value)
        elif kind == "manual_export_error":
            self.manual_export_pending = False
            self.warning.set(value)
            self.status.set("Excel re-export failed. Recovery data is retained.")
            messagebox.showerror("Excel re-export failed", value, parent=self.root)
        elif kind == "disconnected":
            if self.requested_cycles is not None:
                self.notify_run_finished()
            self.session = self.identity = self.applied = None
            self.pending = self.running = self.background_pending = False
            self.dark = self.reference = None
            self.clock_end = None
            if self.led_session is None:
                self.stop_time = None
            self.device_status.set("QE-PRO disconnected. LED connection is managed separately.")
            if value:
                self.failed_shutdown = self.failed_shutdown or any("LED OFF NOT CONFIRMED" in text or "LED Disable" in text or "LED shutdown" in text or "Mightex CloseDevice" in text for text in value)
                self.warning.set("QE-PRO/experiment errors: " + "; ".join(value))
                self.closing = False
                messagebox.showerror("QE-PRO/experiment errors", "\n".join(value))
            elif not self.run_path:
                self.status.set("QE-PRO disconnected; independent LED controls remain available when connected.")
        if kind == "disconnected" and self.run_outcome and self.run_outcome[1] and not self.run_error_reported:
            self.run_error_reported = True
            messagebox.showerror("Experiment stopped", f"{self.run_outcome[2]} completed cycles.\n{self.run_outcome[1]}\nSaved run: {self.run_path}")
        self.update()

    def poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                self.handle(kind, value)
        except queue.Empty:
            pass
        remaining = max(0, self.clock_end-time.monotonic()) if self.clock_end else None
        self.progress_text.set(f"Cycle {self.active_cycle}/{self.requested_cycles or '-'}; completed {self.completed}; est. remaining {duration(remaining) if remaining is not None else '-'}")
        if self.stop_time and time.monotonic()-self.stop_time > 3:
            self.status.set("Shutdown/export still pending. LED-off failures will be reported; spectrometer cleanup may need a timeout.")
        if self.closing and self.session is None and self.led_session is None:
            self.export_worker.shutdown(wait=False)
            self.destroy_window()
            return
        self.poll_id = self.root.after(100, self.poll)


def main():
    mp.freeze_support()
    root = tk.Tk()
    Application(root)
    root.mainloop()


if __name__ == "__main__":
    main()
