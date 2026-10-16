"""Remember one local MP3 and play it asynchronously using Windows audio."""
from concurrent.futures import ThreadPoolExecutor
import ctypes
import os
from pathlib import Path
import shutil
import uuid


DEFAULT_SOUND = Path(__file__).resolve().parent / "user-settings" / "completion.mp3"


class CompletionSound:
    def __init__(self, path=DEFAULT_SOUND):
        self.path = Path(path)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="Completion sound")
        self.alias = "uvvis_" + uuid.uuid4().hex
        self.opened = False
        self.closed = False
        self.winmm = None

    @property
    def available(self):
        return self.path.is_file()

    def _command(self, command):
        if self.winmm is None:
            self.winmm = ctypes.WinDLL("winmm")
            self.winmm.mciSendStringW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_void_p]
            self.winmm.mciSendStringW.restype = ctypes.c_uint
            self.winmm.mciGetErrorStringW.argtypes = [ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_uint]
            self.winmm.mciGetErrorStringW.restype = ctypes.c_int
        error = self.winmm.mciSendStringW(command, None, 0, None)
        if error:
            message = ctypes.create_unicode_buffer(256)
            self.winmm.mciGetErrorStringW(error, message, len(message))
            raise OSError(message.value or f"Windows audio error {error}")

    def _stop(self):
        if self.opened:
            self._command(f"close {self.alias}")
            self.opened = False

    def install(self, source):
        def copy():
            source_path = Path(source)
            if source_path.suffix.lower() != ".mp3" or not source_path.is_file():
                raise ValueError("Choose an existing MP3 audio file.")
            self._stop()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp.mp3")
            try:
                shutil.copyfile(source_path, temporary)
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)
            return source_path.name
        return self.executor.submit(copy)

    def play(self):
        def start():
            if not self.available:
                return
            self._stop()
            self._command(f'open "{self.path.resolve()}" type mpegvideo alias {self.alias}')
            self.opened = True
            try:
                # No wait flag: Windows plays once while the app stays responsive.
                self._command(f"play {self.alias} from 0")
            except Exception:
                self._stop()
                raise
        return self.executor.submit(start)

    def remove(self):
        def clear():
            self._stop()
            self.path.unlink(missing_ok=True)
        return self.executor.submit(clear)

    def close(self):
        if not self.closed:
            self.closed = True
            self.executor.submit(self._stop)
            self.executor.shutdown(wait=False)
