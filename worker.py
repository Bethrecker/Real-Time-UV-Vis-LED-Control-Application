"""One worker owns every SDK call. The Tk thread only exchanges messages."""
import queue
import threading
from spectrometer import OceanDirectAdapter


class PreviewWorker(threading.Thread):
    def __init__(self, sdk, serial, events, factory=OceanDirectAdapter):
        super().__init__(name="OceanDirect preview", daemon=False)
        self.events = events
        self.commands = queue.Queue()
        self.stop_requested = threading.Event()
        self.sdk, self.serial, self.factory = sdk, serial, factory

    def stop(self):
        self.stop_requested.set()

    def run(self):
        adapter = self.factory(self.sdk, self.serial)
        try:
            identity = adapter.connect()
            self.events.put(("connected", (identity, adapter.settings)))
            while not self.stop_requested.is_set():
                try:
                    command, payload = self.commands.get_nowait()
                except queue.Empty:
                    command, payload = "preview", None
                if command == "settings":
                    settings = adapter.apply(payload)
                    self.events.put(("settings", settings))
                    continue
                if command not in ("preview", "dark", "reference", "absorbance"):
                    raise ValueError(f"Unknown worker command: {command}")
                spectrum = adapter.acquire()
                if self.stop_requested.is_set():
                    break
                self.events.put((command, spectrum))
                # Display pacing only; not experiment synchronization.
                self.stop_requested.wait(0.15)
        except Exception as exc:
            self.events.put(("error", str(exc)))
        finally:
            try:
                errors = adapter.close()
            except Exception as exc:
                errors = [f"Unexpected cleanup failure: {exc}"]
            if errors:
                self.events.put(("cleanup_error", "; ".join(errors)))
            self.events.put(("stopped", None))
