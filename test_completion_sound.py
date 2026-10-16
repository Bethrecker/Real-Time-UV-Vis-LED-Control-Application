"""Persistent MP3 selection and run-end notifications; no real audio/hardware."""
from concurrent.futures import Future
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from completion_sound import CompletionSound
import test_run_timing as gui_fixture


ROOT = Path(__file__).resolve().parent


class SoundStorageTests(unittest.TestCase):
    def test_uploaded_copy_survives_source_deletion_and_new_player(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            source = Path(folder)/"my short clip.mp3"
            source.write_bytes(b"test MP3 content")
            saved = Path(folder)/"settings"/"completion.mp3"
            player = CompletionSound(saved)
            try:
                self.assertEqual(player.install(source).result(2), source.name)
                source.unlink()
                self.assertEqual(saved.read_bytes(), b"test MP3 content")
            finally:
                player.close()
            restored = CompletionSound(saved)
            try:
                self.assertTrue(restored.available)
                with patch.object(restored, "_command") as command:
                    restored.play().result(2)
                    restored.play().result(2)
                    commands = [call.args[0] for call in command.call_args_list]
                    self.assertEqual(commands.count(f"play {restored.alias} from 0"), 2)
                    self.assertFalse(any("wait" in value for value in commands))
                    restored.remove().result(2)
                    self.assertFalse(saved.exists())
                    self.assertFalse(restored.opened)
            finally:
                restored.close()

    def test_invalid_selection_preserves_previously_saved_clip(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            saved = Path(folder)/"completion.mp3"
            saved.write_bytes(b"previous audio")
            player = CompletionSound(saved)
            try:
                with self.assertRaises(ValueError):
                    player.install(Path(folder)/"missing.wav").result(2)
                self.assertEqual(saved.read_bytes(), b"previous audio")
            finally:
                player.close()


class SoundGUITests(unittest.TestCase):
    setUp = gui_fixture.LiveAbsorbanceGUITests.setUp
    tearDown = gui_fixture.LiveAbsorbanceGUITests.tearDown
    wait_for = gui_fixture.LiveAbsorbanceGUITests.wait_for

    def test_one_notification_for_completed_stopped_and_failed_runs(self):
        app = self.app
        with patch.object(app.completion_sound, "play", wraps=app.completion_sound.play) as play:
            for outcome in (("completed", None, 3), ("stopped", None, 1), ("error", "read failed", 1)):
                app.run_sound_played = False
                app.requested_cycles = 3
                app.handle("run_finished", outcome)
                app.handle("exported", "saved-run")
                app.handle("run_ready", None)
                app.handle("disconnected", [])
            self.assertEqual(play.call_count, 3)

    def test_disconnect_after_start_failure_notifies_once_and_idle_disconnect_does_not(self):
        app = self.app
        with patch.object(app.completion_sound, "play", wraps=app.completion_sound.play) as play:
            app.handle("disconnected", [])
            play.assert_not_called()
            app.requested_cycles = 3
            app.handle("error", "failed to prepare experiment")
            app.handle("disconnected", [])
            app.handle("disconnected", [])
            play.assert_called_once()

    def test_choose_cancel_and_audio_error_leave_run_state_intact(self):
        app = self.app
        with patch("gui.filedialog.askopenfilename", return_value=""):
            app.choose_completion_sound()
        self.assertFalse(app.sound_pending)
        app.run_outcome = ("completed", None, 3)
        app.handle("sound_error", "unsupported audio")
        self.assertEqual(app.run_outcome, ("completed", None, 3))
        self.assertIn("unsupported audio", app.sound_status.get())

    def test_upload_preview_remove_and_new_run_keeps_selection(self):
        app = self.app
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            source = Path(folder)/"clip.mp3"
            source.write_bytes(b"test MP3 content")
            app.completion_sound = CompletionSound(Path(folder)/"settings"/"completion.mp3")
            with patch("gui.filedialog.askopenfilename", return_value=str(source)):
                app.choose_completion_sound()
            self.wait_for(lambda: not app.sound_pending)
            self.assertIn("clip.mp3", app.sound_status.get())
            with patch.object(app.completion_sound, "play", return_value=Future()) as play:
                app.preview_completion_sound()
                play.assert_called_once()
            with patch("gui.simpledialog.askstring", return_value="next-sample"):
                app.new_run()
            self.assertTrue(app.completion_sound.available)
            app.remove_completion_sound()
            self.wait_for(lambda: not app.sound_pending)
            self.assertFalse(app.completion_sound.available)


if __name__ == "__main__":
    unittest.main()
