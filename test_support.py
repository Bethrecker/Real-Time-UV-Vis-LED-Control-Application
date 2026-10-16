"""Explicit test-only device injection. The shipped GUI has no simulation mode."""
from gui import Application
from session import Session
from led_session import LEDConnection
from concurrent.futures import Future


class SilentSound:
    """Tests never play a user's saved audio clip."""
    available = False

    def play(self):
        result = Future()
        result.set_result(None)
        return result

    def close(self):
        pass


def fake_session(_real, *args, **kwargs):
    session = Session(True, *args, **kwargs)
    session.phase = "blocked"
    return session


def fake_led(_real, *args, **kwargs):
    return LEDConnection(True, *args, **kwargs)


def make_application(root):
    app = Application(root, session_factory=fake_session, led_factory=fake_led, sound_factory=SilentSound)
    # Approved dummy values apply only to injected fake devices.
    app.variables["channel"].set("1")
    app.variables["level"].set("10")
    app.confirmed.set(True)
    app.update()
    class Probe:
        def set(self, phase):
            if app.session:
                app.session.commands.put(("preview", (app.preview_enabled.get(), phase, app.tabs.index(app.tabs.select()) == 1)))
    app.probe_phase = Probe()  # test driver only, absent from the real GUI
    return app
