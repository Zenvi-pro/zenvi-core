"""Speaking a chat prompt: the microphone through ffmpeg, words from the captions recogniser, or Wispr Flow."""

import json
import os
import threading
import time
import wave

import pytest

from classes import dictation
from classes import hyperframes_local as hf


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(hf, "home", lambda: str(tmp_path / "hyperframes"))
    return tmp_path


# -- microphone -------------------------------------------------------------

def test_mic_input_per_platform(monkeypatch):
    assert dictation.mic_args("darwin") == ["-f", "avfoundation", "-i", ":default"]
    assert dictation.mic_args("linux") == ["-f", "pulse", "-i", "default"]


@pytest.mark.parametrize("listing", [
    # ffmpeg 5+: one line per device, tagged with its kind
    '[dshow @ 0x1] "Integrated Camera" (video)\n'
    '[dshow @ 0x1]   Alternative name "@device_pnp_x"\n'
    '[dshow @ 0x1] "Microphone Array (Realtek(R) Audio)" (audio)\n'
    '[dshow @ 0x1]   Alternative name "@device_pnp_mic"\n',
    # ffmpeg 4: devices grouped under headings
    '[dshow @ 0x1] DirectShow video devices\n[dshow @ 0x1]  "Integrated Camera"\n'
    '[dshow @ 0x1] DirectShow audio devices\n[dshow @ 0x1]  "Microphone Array (Realtek(R) Audio)"\n'
    '[dshow @ 0x1]     Alternative name "@device_cm_x"\n',
])
def test_windows_uses_the_first_microphone_directshow_lists(monkeypatch, listing):
    class Done:
        stderr = listing
    monkeypatch.setattr(dictation.subprocess, "run", lambda *a, **k: Done())
    # its ASCII alternative name: the friendly one can lose characters such as the (R) sign
    assert dictation.mic_args("win32", "ffmpeg") == ["-f", "dshow", "-i", "audio=" + (
        "@device_cm_x" if "DirectShow audio" in listing else "@device_pnp_mic")]


def test_windows_falls_back_to_the_friendly_name(monkeypatch):
    class Done:
        stderr = '[dshow @ 0x1] "USB Mic" (audio)\n'
    monkeypatch.setattr(dictation.subprocess, "run", lambda *a, **k: Done())
    assert dictation.mic_args("win32", "ffmpeg") == ["-f", "dshow", "-i", "audio=USB Mic"]


def test_windows_without_a_microphone_says_so(monkeypatch):
    class Done:
        stderr = '[dshow @ 0x1] "Integrated Camera" (video)\n'
    monkeypatch.setattr(dictation.subprocess, "run", lambda *a, **k: Done())
    with pytest.raises(dictation.DictationError, match="microphone"):
        dictation.mic_args("win32", "ffmpeg")


# -- recognising ------------------------------------------------------------

def test_whistle_hears_a_16k_mono_wav(home, monkeypatch):
    heard = {}

    def transcribe(wav, timeout=0):
        with wave.open(wav) as w:
            heard.update(rate=w.getframerate(), channels=w.getnchannels(), width=w.getsampwidth(),
                         frames=w.getnframes())
        return [{"text": "cut", "start": 0, "end": 0.2}, {"text": " the ", "start": 0.2, "end": 0.4},
                {"text": "intro.", "start": 0.4, "end": 0.9}]

    monkeypatch.setattr(hf, "recogniser", lambda: "whistle")
    monkeypatch.setattr(hf, "speech_ready", lambda fresh=False: True)
    monkeypatch.setattr(hf, "transcribe", transcribe)
    assert dictation.transcribe(b"\x00\x30" *16000) == "cut the intro."
    assert heard == {"rate": 16000, "channels": 1, "width": 2, "frames": 16000}


def test_without_whistle_or_parakeet_it_says_where_to_get_one(home, monkeypatch):
    monkeypatch.setattr(hf, "recogniser", lambda: "parakeet")      # Intel Macs: Whistle has no engine
    monkeypatch.setattr(hf, "status", lambda: {"parakeet": False})
    monkeypatch.setattr(hf, "speech_supported", lambda *a: False)
    with pytest.raises(dictation.DictationError, match="Captions"):
        dictation.transcribe(b"\x00\x30" * 1600)


def test_whistle_is_downloaded_on_first_use(home, monkeypatch):
    installed = []
    monkeypatch.setattr(hf, "recogniser", lambda: "whistle")
    monkeypatch.setattr(hf, "speech_ready", lambda fresh=False: bool(installed))
    monkeypatch.setattr(hf, "install_speech", lambda: installed.append(1))
    monkeypatch.setattr(hf, "transcribe", lambda wav, timeout=0: [{"text": "hi", "start": 0, "end": 1}])
    assert dictation.transcribe(b"\x00\x30" *1600) == "hi"
    assert installed == [1]


def test_parakeet_runs_through_the_cli_when_the_user_picked_it(home, monkeypatch):
    calls = []

    def run(project, program, args, timeout=0, inputs=()):
        calls.append((project, program, args))
        with open(os.path.join(hf.project_dir(project), "transcript.json"), "w") as fh:
            json.dump([{"text": "add", "start": 0, "end": 1}, {"text": "music", "start": 1, "end": 2}], fh)
        return 0, ""

    monkeypatch.setattr(hf, "recogniser", lambda: "parakeet")
    monkeypatch.setattr(hf, "status", lambda: {"parakeet": True})
    monkeypatch.setattr(hf, "run", run)
    assert dictation.transcribe(b"\x00\x30" *1600) == "add music"
    assert calls == [("dictation", "hyperframes", ["transcribe", "speech.wav", "--engine", "parakeet", "--json"])]


def test_parakeet_not_downloaded_falls_back_to_whistle(home, monkeypatch):
    monkeypatch.setattr(hf, "recogniser", lambda: "parakeet")
    monkeypatch.setattr(hf, "status", lambda: {"parakeet": False})
    monkeypatch.setattr(hf, "speech_supported", lambda *a: True)
    monkeypatch.setattr(hf, "speech_ready", lambda fresh=False: True)
    monkeypatch.setattr(hf, "transcribe", lambda wav, timeout=0: [{"text": "ok", "start": 0, "end": 1}])
    assert dictation.transcribe(b"\x00\x30" *1600) == "ok"
    assert dictation.live_engine() is True


# -- a dictation session ----------------------------------------------------

class FakeMic:
    """ffmpeg reading the microphone: PCM on stdout until it is stopped."""

    def __init__(self, fail=""):
        read, self._write = os.pipe()
        self.stdout = os.fdopen(read, "rb")
        self.fail = fail
        self.returncode = None
        if fail:
            os.close(self._write)
            self.returncode = 1

    def speak(self, seconds, level=3000):           # speech RMS; a quiet laptop room measured ~430
        os.write(self._write, (level.to_bytes(2, "little") + (-level).to_bytes(2, "little", signed=True))
                 * int(8000 * seconds))

    def poll(self):
        return self.returncode

    def terminate(self):
        if self.returncode is None:
            os.close(self._write)
            self.returncode = 0

    kill = terminate

    def wait(self, timeout=None):
        return self.returncode


def _session(monkeypatch, mic, live=True):
    texts, errors, done = [], [], threading.Event()
    monkeypatch.setattr(dictation, "PARTIAL_EVERY", 0.05)
    monkeypatch.setattr(dictation, "find_ffmpeg", lambda name="ffmpeg": "ffmpeg")
    monkeypatch.setattr(dictation, "mic_args", lambda *a: ["-f", "fake", "-i", "mic"])
    def popen(argv, **kwargs):
        kwargs["stderr"].write(mic.fail.encode())     # what ffmpeg says when the mic will not open
        return mic

    monkeypatch.setattr(dictation.subprocess, "Popen", popen)
    monkeypatch.setattr(dictation, "live_engine", lambda: live)
    monkeypatch.setattr(dictation, "transcribe", lambda pcm: "%.1f seconds" % (len(pcm) / 32000.0))

    def on_text(text, final):
        texts.append((text, final))
        if final:
            done.set()

    def on_error(message):
        errors.append(message)
        done.set()

    return dictation.Dictation(on_text, on_error), texts, errors, done


def test_words_appear_while_speaking_and_once_more_at_the_end(monkeypatch):
    mic = FakeMic()
    session, texts, errors, done = _session(monkeypatch, mic)
    session.start()
    mic.speak(1.0)
    deadline = threading.Event()
    for _ in range(100):
        if texts:
            break
        deadline.wait(0.02)
    mic.speak(0.5)
    session.stop()
    assert done.wait(5)
    assert errors == []
    assert texts[0] == ("1.0 seconds", False)
    assert texts[-1] == ("1.5 seconds", True)
    assert [final for _t, final in texts].count(True) == 1


def test_no_live_words_when_the_recogniser_is_too_slow_for_it(monkeypatch):
    mic = FakeMic()
    session, texts, errors, done = _session(monkeypatch, mic, live=False)
    session.start()
    mic.speak(1.0)
    threading.Event().wait(0.3)
    session.stop()
    assert done.wait(5)
    assert texts == [("1.0 seconds", True)]


def test_a_quiet_room_is_no_words_not_a_recogniser_hallucination(monkeypatch):
    mic = FakeMic()
    session, texts, errors, done = _session(monkeypatch, mic)
    heard = []
    monkeypatch.setattr(dictation, "transcribe", lambda pcm: heard.append(pcm) or "Thank you.")
    session.start()
    mic.speak(1.0, level=430)
    threading.Event().wait(0.3)
    session.stop()
    assert done.wait(5)
    assert texts == [("", True)] and heard == []


def test_a_microphone_that_cannot_open_is_an_error_not_silence(monkeypatch):
    mic = FakeMic(fail="Could not find audio only device with name [default]")
    session, texts, errors, done = _session(monkeypatch, mic)
    session.start()
    assert done.wait(5)
    assert texts == []
    assert "Could not find audio" in errors[0]


def test_stop_lets_go_of_the_microphone_before_the_words_are_heard(monkeypatch):
    mic = FakeMic()
    session, texts, errors, done = _session(monkeypatch, mic)
    hearing, release = threading.Event(), threading.Event()

    def slow(pcm):                                    # Parakeet takes ~15 s a pass
        hearing.set()
        release.wait(5)
        return "words"

    monkeypatch.setattr(dictation, "transcribe", slow)
    session.start()
    mic.speak(1.0)
    assert hearing.wait(5)
    session.stop()
    assert mic.returncode is not None                 # the mic is free while the words are still coming
    release.set()
    assert done.wait(5) and texts[-1] == ("words", True)


def test_listing_microphones_does_not_hold_up_the_caller(monkeypatch):
    mic = FakeMic()
    session, texts, errors, done = _session(monkeypatch, mic)
    listed = threading.Event()

    def slow_listing(*a):                             # DirectShow enumeration can take seconds
        listed.wait(5)
        return ["-f", "fake", "-i", "mic"]

    monkeypatch.setattr(dictation, "mic_args", slow_listing)
    began = time.monotonic()
    session.start()                                   # returns at once: the GUI thread calls it
    assert time.monotonic() - began < 1
    listed.set()
    mic.speak(0.5)
    session.stop()
    assert done.wait(5) and texts[-1][1] is True


def test_a_microphone_listing_error_is_reported_not_raised(monkeypatch):
    mic = FakeMic()
    session, texts, errors, done = _session(monkeypatch, mic)

    def no_mic(*a):
        raise dictation.DictationError("no microphone was found on this computer")

    monkeypatch.setattr(dictation, "mic_args", no_mic)
    session.start()
    assert done.wait(5)
    assert errors == ["no microphone was found on this computer"]


def test_no_ffmpeg_is_an_error(monkeypatch):
    monkeypatch.setattr(dictation, "find_ffmpeg", lambda name="ffmpeg": None)
    with pytest.raises(dictation.DictationError, match="ffmpeg"):
        dictation.Dictation(lambda *a: None, lambda m: None).start()


# -- Wispr Flow -------------------------------------------------------------

@pytest.mark.parametrize("choice,installed,expected", [
    ("auto", True, True), ("auto", False, False),
    ("wispr", True, True), ("wispr", False, False),     # picked but not installed: Zenvi's own
    ("zenvi", True, False),
])
def test_wispr_flow_is_used_when_installed_unless_the_user_picked_zenvi(choice, installed, expected):
    assert dictation.use_wispr_flow(choice, installed) is expected


def test_wispr_flow_found_on_macos(tmp_path):
    (tmp_path / "Wispr Flow.app").mkdir()
    assert dictation.wispr_flow_installed("darwin", [str(tmp_path)]) is True
    assert dictation.wispr_flow_installed("darwin", [str(tmp_path / "none")]) is False
    assert dictation.wispr_flow_installed("linux") is False


# -- the chat dock's mic button --------------------------------------------

class _Dock:
    """Just enough of AIChatWindow for the mic button."""

    def __init__(self, window_cls):
        import types
        from unittest.mock import MagicMock
        self.js, self.pending = [], []
        self._dictation = None
        self.dictation_heard = MagicMock()
        self.dictation_failed = MagicMock()
        for name in ("toggle_dictation", "_on_dictation_heard", "_on_dictation_failed"):
            setattr(self, name, types.MethodType(getattr(window_cls, name), self))

    def _run_js(self, code, callback=None):
        self.js.append(code)
        if callback:
            self.pending.append(callback)            # WebEngine answers later, from its own turn


@pytest.fixture
def dock(monkeypatch):
    import sys
    from unittest.mock import MagicMock
    for mod in ("PyQt5.QtWebEngineWidgets", "PyQt5.QtWebKitWidgets", "PyQt5.QtWebKit"):
        sys.modules.setdefault(mod, MagicMock())
    from windows.ai_chat_ui import AIChatWindow
    from classes import app
    settings = {"dictation-engine": "auto"}
    monkeypatch.setattr(app, "get_app", lambda: MagicMock(get_settings=lambda: MagicMock(get=settings.get)))
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url) or settings.get("opens", True))
    d = _Dock(AIChatWindow)
    d.settings, d.opened = settings, opened
    return d


def test_mic_button_hands_off_to_wispr_flow_when_installed(dock, monkeypatch):
    monkeypatch.setattr(dictation, "wispr_flow_installed", lambda *a: True)
    dock.toggle_dictation()
    assert "chat-input" in dock.js[-1] and dock.opened == []   # Wispr Flow types into the focused box:
    dock.pending.pop()(True)                                  # it starts only once the prompt has focus
    assert dock.opened == [dictation.WISPR_FLOW_URL]
    assert dock._dictation is None


def test_wispr_flow_that_will_not_open_is_reported(dock, monkeypatch):
    monkeypatch.setattr(dictation, "wispr_flow_installed", lambda *a: True)
    dock.settings["opens"] = False
    dock.toggle_dictation()
    dock.pending.pop()(True)
    assert "'off'" in dock.js[-1] and "Wispr Flow could not be opened" in dock.js[-1]


def test_mic_button_records_with_zenvi_and_fills_the_prompt(dock, monkeypatch):
    started, stopped = [], []

    class FakeSession:
        def __init__(self, on_text, on_error):
            pass

        def start(self):
            started.append(1)

        def stop(self):
            stopped.append(1)

    monkeypatch.setattr(dictation, "wispr_flow_installed", lambda *a: True)
    monkeypatch.setattr(dictation, "Dictation", FakeSession)
    dock.settings["dictation-engine"] = "zenvi"
    dock.toggle_dictation()
    assert started == [1] and dock.opened == []
    assert "'listening'" in dock.js[-1]
    dock.toggle_dictation()                       # second click: stop, final words still to come
    assert stopped == [1]
    dock._on_dictation_heard("cut the intro", True)
    assert dock._dictation is None
    assert 'setDictationText("cut the intro", true)' in dock.js[-1]


def test_mic_button_reports_a_microphone_that_will_not_start(dock, monkeypatch):
    class Broken:
        def __init__(self, *a):
            pass

        def start(self):
            raise dictation.DictationError("no microphone was found on this computer")

    monkeypatch.setattr(dictation, "wispr_flow_installed", lambda *a: False)
    monkeypatch.setattr(dictation, "Dictation", Broken)
    dock.toggle_dictation()
    assert dock._dictation is None
    assert "'off'" in dock.js[-1] and "no microphone" in dock.js[-1]
