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
        self._closing = threading.Lock()
        if fail:
            os.close(self._write)
            self.returncode = 1

    def _tone(self, seconds, levels):
        for i in range(int(10 * seconds)):            # 100 ms at a time, cycling through *levels*
            level = levels[i % len(levels)]
            os.write(self._write, (level.to_bytes(2, "little") + (-level).to_bytes(2, "little", signed=True)) * 800)

    def speak(self, seconds, level=3000):           # syllables and the gaps between them
        self._tone(seconds, [level, level // 10])

    def hum(self, seconds, level):                  # a room with nobody talking: the same level throughout
        self._tone(seconds, [level])

    def poll(self):
        return self.returncode

    def terminate(self):                            # stop and the recording thread can both call it at once
        with self._closing:
            if self.returncode is None:
                self.returncode = 0
                os.close(self._write)

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
    monkeypatch.setattr(dictation, "transcribe", lambda pcm, parakeet=None: "%.1f seconds" % (len(pcm) / 32000.0))

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


@pytest.mark.parametrize("level", [430, 1500])    # a quiet laptop room; a fan right by the mic
def test_a_room_with_nobody_talking_is_no_words_not_a_recogniser_hallucination(monkeypatch, level):
    mic = FakeMic()
    session, texts, errors, done = _session(monkeypatch, mic)
    heard = []
    monkeypatch.setattr(dictation, "transcribe", lambda pcm, parakeet=None: heard.append(pcm) or "Thank you.")
    session.start()
    mic.hum(1.0, level)
    threading.Event().wait(0.3)
    session.stop()
    assert done.wait(5)
    assert texts == [("", True)] and heard == []


def test_a_quiet_microphone_still_hears_speech(monkeypatch):
    """A laptop array mic: the room ~80 RMS, a spoken "hello" only ~500 (measured, 2026-10-16)."""
    mic = FakeMic()
    session, texts, errors, done = _session(monkeypatch, mic, live=False)
    session.start()
    mic.hum(0.5, 80)
    mic.speak(1.0, level=500)
    threading.Event().wait(0.3)
    session.stop()
    assert done.wait(5)
    assert texts == [("1.5 seconds", True)]


def test_digital_silence_while_the_mic_wakes_up_is_not_a_noise_floor(monkeypatch):
    mic = FakeMic()
    session, texts, errors, done = _session(monkeypatch, mic, live=False)
    monkeypatch.setattr(dictation, "transcribe", lambda pcm, parakeet=None: "Thank you.")
    session.start()
    mic.hum(0.5, 0)                                   # dshow hands over zeros before the first samples
    mic.hum(1.0, 430)
    threading.Event().wait(0.3)
    session.stop()
    assert done.wait(5)
    assert texts == [("", True)]


def test_faint_speech_with_no_level_over_a_few_hundred_is_still_speech():
    # a video talking near the same laptop mic (Whistle heard full sentences): no fixed level fits both
    faint = [0, 0, 0, 0, 6, 8, 98, 111, 83, 40, 7, 24, 0, 8, 100, 8, 1, 52, 34, 88, 110, 38, 76, 32, 93, 50,
             56, 89, 97, 141, 20, 161, 79, 91, 27, 127, 33, 11, 119, 62, 43, 34, 11, 20, 3, 0, 0, 13, 95, 17]
    assert dictation.heard_speech(faint) is True


def test_a_mic_that_only_hisses_is_not_speech():
    assert dictation.heard_speech([0, 0, 6, 9, 12, 7, 15, 11, 8, 14, 10, 6, 13, 9]) is False


@pytest.mark.parametrize("phrase", ["Thank you.", "thank you", "Thanks for watching!", "you", "."])
def test_what_the_recogniser_makes_up_for_silence_is_dropped(monkeypatch, phrase):
    mic = FakeMic()
    session, texts, errors, done = _session(monkeypatch, mic, live=False)
    monkeypatch.setattr(dictation, "transcribe", lambda pcm, parakeet=None: phrase)
    session.start()
    mic.speak(1.0)
    threading.Event().wait(0.3)
    session.stop()
    assert done.wait(5)
    assert texts == [("", True)]


def test_a_real_thank_you_sentence_is_kept(monkeypatch):
    mic = FakeMic()
    session, texts, errors, done = _session(monkeypatch, mic, live=False)
    monkeypatch.setattr(dictation, "transcribe", lambda pcm, parakeet=None: "Thank you, now cut the intro.")
    session.start()
    mic.speak(1.0)
    threading.Event().wait(0.3)
    session.stop()
    assert done.wait(5)
    assert texts == [("Thank you, now cut the intro.", True)]


def test_hello_on_that_mic_is_speech():
    hello = [0, 0, 10, 47, 65, 70, 7, 3, 0, 48, 1077, 968, 1249, 1038, 535, 388, 563, 505, 656, 410, 476, 381,
             404, 398, 471, 460, 404, 476, 291, 330, 395, 189, 125, 80, 328, 517, 493, 328, 366, 306]
    assert dictation.heard_speech(hello) is True


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

    def slow(pcm, parakeet=None):                    # Parakeet takes ~15 s a pass
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
        self._dictation_take = 0
        self._dictation_open = False
        self.dictation_heard = MagicMock()
        self.dictation_failed = MagicMock()
        self.installed = threading.Event()
        self.dictation_installed = MagicMock(emit=lambda *a: (self._on_dictation_installed(*a), self.installed.set()))
        self._dictation_download = ""
        self.confirmed = []
        self.answer = True
        for name in ("toggle_dictation", "_on_dictation_heard", "_on_dictation_failed",
                     "pick_dictation_engine", "_on_dictation_installed", "_dictation_note",
                     "_abandon_dictation"):
            setattr(self, name, types.MethodType(getattr(window_cls, name), self))

    def _confirm_dictation_download(self, label):
        self.confirmed.append(label)
        return self.answer

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
    monkeypatch.setattr(app, "get_app", lambda: MagicMock(
        get_settings=lambda: MagicMock(get=settings.get, set=settings.__setitem__)))
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


# -- picking the recogniser next to the mic ----------------------------------

@pytest.fixture
def models(home, monkeypatch):
    """Whistle installed, Parakeet not, HyperFrames set up; the Captions dock's choice is Whistle."""
    state = {"recogniser": "whistle", "installs": []}

    def status():          # hashes the speech files under the install lock: never on the GUI thread
        raise AssertionError("hf.status() from the mic menu")

    def put(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").close()

    monkeypatch.setattr(hf, "status", status)
    monkeypatch.setattr(hf, "recogniser", lambda: state["recogniser"])
    monkeypatch.setattr(hf, "choose_recogniser", lambda name: state.update(recogniser=name))
    monkeypatch.setattr(hf, "speech_supported", lambda *a: True)
    put(hf.speech_model())
    put(hf.cli_script())

    def installer(key, path):
        def install():
            state["installs"].append(key)
            put(path())
        return install

    monkeypatch.setattr(hf, "install_speech", installer("speech", hf.speech_model))
    monkeypatch.setattr(hf, "install_parakeet", installer("parakeet", hf._parakeet_model))
    return state


def test_the_menu_lists_three_engines_with_what_each_needs(models):
    rows = {e["id"]: e for e in dictation.engines("zenvi", False)}
    assert list(rows) == ["whistle", "parakeet", "wispr"]
    assert rows["whistle"]["note"] == "installed" and rows["whistle"]["checked"]
    assert rows["parakeet"]["note"] == "download about 900 MB" and not rows["parakeet"]["checked"]
    assert rows["wispr"]["note"] == "get it at wisprflow.ai" and not rows["wispr"]["checked"]
    rows = {e["id"]: e for e in dictation.engines("auto", True)}
    assert rows["wispr"]["checked"] and rows["wispr"]["note"] == "installed" and not rows["whistle"]["checked"]


def test_whistle_is_not_offered_where_it_has_no_engine(models, monkeypatch):
    monkeypatch.setattr(hf, "speech_supported", lambda *a: False)       # Intel Macs
    whistle = dictation.engines("zenvi", False)[0]
    assert whistle["enabled"] is False


def test_picking_an_installed_recogniser_uses_it_at_once(dock, models):
    dock.pick_dictation_engine("whistle")
    assert models["recogniser"] == "whistle" and dock.settings["dictation-engine"] == "zenvi"
    assert dock.confirmed == [] and models["installs"] == []


def test_picking_parakeet_asks_then_downloads_then_uses_it(dock, models):
    dock.pick_dictation_engine("parakeet")
    assert dock.installed.wait(5)
    assert dock.confirmed and "900 MB" in dock.confirmed[0]
    assert models["installs"] == ["parakeet"] and models["recogniser"] == "parakeet"
    assert dock.settings["dictation-engine"] == "zenvi"
    assert "Parakeet" in dock.js[-1]                                   # says what it now dictates with


def test_declining_the_download_changes_nothing(dock, models):
    dock.answer = False
    dock.pick_dictation_engine("parakeet")
    assert models["installs"] == [] and models["recogniser"] == "whistle"
    assert dock.settings["dictation-engine"] == "auto"


def test_a_failed_download_is_reported_and_keeps_the_old_recogniser(dock, models, monkeypatch):
    def broken():
        raise hf.HyperframesError("HyperFrames is not set up on this computer yet")

    monkeypatch.setattr(hf, "install_parakeet", broken)
    dock.pick_dictation_engine("parakeet")
    assert dock.installed.wait(5)
    assert models["recogniser"] == "whistle" and "not set up" in dock.js[-1]


def test_picking_wispr_flow_without_it_opens_their_website(dock, models, monkeypatch):
    monkeypatch.setattr(dictation, "wispr_flow_installed", lambda *a: False)
    dock.pick_dictation_engine("wispr")
    assert dock.opened == [dictation.WISPR_FLOW_SITE]
    assert dock.settings["dictation-engine"] == "auto"


def test_picking_wispr_flow_when_installed_uses_it(dock, models, monkeypatch):
    monkeypatch.setattr(dictation, "wispr_flow_installed", lambda *a: True)
    dock.pick_dictation_engine("wispr")
    assert dock.settings["dictation-engine"] == "wispr" and dock.opened == []


def test_parakeet_needs_hyperframes_set_up_first(models):
    os.remove(hf.cli_script())
    parakeet = dictation.engines("zenvi", False)[1]
    assert parakeet["enabled"] is False and "HyperFrames" in parakeet["note"]


def test_a_take_keeps_the_recogniser_it_started_with(monkeypatch):
    """Picking Parakeet (or changing it in the Captions dock) mid-take must not move this take to it."""
    mic = FakeMic()
    session, texts, errors, done = _session(monkeypatch, mic)
    monkeypatch.setattr(dictation, "live_engine", lambda: True)
    engines_used = []
    monkeypatch.setattr(dictation, "transcribe", lambda pcm, parakeet=None: engines_used.append(parakeet) or "w")
    monkeypatch.setattr(dictation, "_parakeet", lambda: len(engines_used) > 0)   # switched after the first pass
    session.start()
    mic.speak(1.0)
    for _ in range(100):
        if len(engines_used) >= 2:
            break
        threading.Event().wait(0.02)
    session.stop()
    assert done.wait(5)
    assert set(engines_used) == {False}


def test_downloading_note_stays_until_the_download_ends(dock, models):
    dock.pick_dictation_engine("parakeet")
    assert dock.installed.wait(5)
    downloading = next(js for js in dock.js if "Downloading" in js)
    assert downloading.rstrip(";").endswith("true)")                  # sticky: no 6 s timeout


# -- the prompt box while the words are being worked out (chat.js under node) --

_MIC_HARNESS = r"""
function classList() { var s = {}; return {toggle: function (c, on) { if (on) s[c] = 1; else delete s[c]; },
                                            has: function (c) { return !!s[c]; }}; }
var micBtn = {classList: classList(), title: '', setAttribute: function () {}};
var inputEl = {value: '', placeholder: 'Ask or edit directly', focus: function () {}};
var window = {};
function setTimeout() {}
function adjustTextareaHeight() {}
function hideOverlay() {}
var dictationState = 'off', dictationPrefix = '', dictationSaved = '', dictationUncommitted = false, modePlaceholder = '';
%s
%s
var out = {};
window.setDictationState('listening');
out.listening = inputEl.placeholder;
window.setDictationState('hearing');
out.hearing = inputEl.placeholder;
out.hearingBusy = micBtn.classList.has('hearing');
window.setDictationText('cut the intro', true);
out.done = inputEl.placeholder;
out.text = inputEl.value;
inputEl.value = '';
window.setDictationState('listening');
window.setDictationState('hearing');
window.setDictationText('', true);
out.nothing = inputEl.placeholder;
console.log(JSON.stringify(out));
"""


def test_the_prompt_says_the_words_are_coming_after_stop():
    """Parakeet takes ~10 s after stop: the box must say it is working, not keep saying Listening."""
    import shutil
    import subprocess
    from pathlib import Path
    node = shutil.which("node")
    if not node:
        pytest.skip("node not on PATH")
    src = (Path(__file__).resolve().parents[1] / "src" / "chat_ui" / "chat.js").read_text(encoding="utf-8")

    def cut(start, end):
        i = src.index(start)
        return src[i:src.index(end, i) + len(end)]

    proc = subprocess.run([node, "-e", _MIC_HARNESS % (
        cut("window.setDictationState = function", "\n    };"),
        cut("window.setDictationText = function", "\n    };"))], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["listening"] == "Listening..."
    assert out["hearing"] not in ("Listening...", "Ask or edit directly") and "words" in out["hearing"].lower()
    assert out["hearingBusy"] is True
    assert out["done"] == "Ask or edit directly" and out["text"] == "cut the intro"
    assert "no speech" in out["nothing"].lower()          # an empty take says so instead of just resetting


def test_switching_chat_tabs_stops_the_take_and_drops_its_words(dock, monkeypatch):
    # The composer is shared by every tab: words spoken for one chat must not land in another.
    takes = []

    class FakeSession:
        def __init__(self, on_text, on_error):
            self.on_text, self.on_error, self.stopped = on_text, on_error, False
            takes.append(self)

        def start(self):
            pass

        def stop(self):
            self.stopped = True

    def deliver(signal):                          # Qt runs the queued slot later, on the GUI thread
        return lambda *args: signal(*args)

    monkeypatch.setattr(dictation, "wispr_flow_installed", lambda *a: False)
    monkeypatch.setattr(dictation, "Dictation", FakeSession)
    dock.dictation_heard.emit.side_effect = deliver(dock._on_dictation_heard)
    dock.dictation_failed.emit.side_effect = deliver(dock._on_dictation_failed)
    dock.toggle_dictation()                       # speaking in chat A
    dock._abandon_dictation()                     # another chat is about to show
    assert takes[0].stopped and dock._dictation is None
    assert "cancelDictation" in dock.js[-1]      # its partial words leave the shared box
    dock.toggle_dictation()                       # a new take in chat B
    written = len(dock.js)
    takes[0].on_text("words for chat A", True)    # chat A's final, queued before the switch, lands now
    takes[0].on_error("microphone closed")
    assert not dock.js[written:] and dock._dictation is takes[1]     # nothing written, B still running
    takes[1].on_text("words for chat B", True)
    assert 'setDictationText("words for chat B", true)' in dock.js[-1] and dock._dictation is None


def test_switching_while_the_words_are_finishing_still_drops_them(dock, monkeypatch):
    # The final clears the recorder before the page paints, so a tab switch in that gap must still cancel.
    takes = []

    class FakeSession:
        def __init__(self, on_text, on_error):
            self.on_text, self.on_error, self.stopped = on_text, on_error, False
            takes.append(self)

        def start(self):
            pass

        def stop(self):
            self.stopped = True

    monkeypatch.setattr(dictation, "wispr_flow_installed", lambda *a: False)
    monkeypatch.setattr(dictation, "Dictation", FakeSession)
    dock.dictation_heard.emit.side_effect = lambda *args: dock._on_dictation_heard(*args)
    dock.toggle_dictation()
    takes[0].on_text("words for chat A", True)
    assert dock._dictation is None
    dock._abandon_dictation()
    assert "cancelDictation" in dock.js[-1]


def test_a_switch_that_does_not_change_the_chat_keeps_the_take(dock):
    import types

    from windows.ai_chat_ui import AIChatWindow

    calls = []
    dock._abandon_dictation = lambda: calls.append(1)
    dock._sessions = {"a": {}}
    dock._active_sid = "a"
    dock._current_project_path = r"C:\film.zvn"
    dock._history_key = "k"
    for name in ("reload_for_project", "restore_draft", "_reopen_closed_session", "_switch_session"):
        setattr(dock, name, types.MethodType(getattr(AIChatWindow, name), dock))
    dock.reload_for_project(r"C:\film.zvn")
    dock.restore_draft("not-a-draft")
    dock._reopen_closed_session("")
    dock._reopen_closed_session("a")
    assert calls == []


def test_cancelling_a_take_puts_back_the_exact_text(dock):
    """Listening inserts a trailing space so words join on; cancel must not keep that space."""
    import shutil
    import subprocess
    from pathlib import Path
    node = shutil.which("node")
    if not node:
        pytest.skip("node not on PATH")
    src = (Path(__file__).resolve().parents[1] / "src" / "chat_ui" / "chat.js").read_text(encoding="utf-8")

    def cut(start, end):
        i = src.index(start)
        return src[i:src.index(end, i) + len(end)]

    script = _MIC_HARNESS.split("var out")[0] % (
        cut("window.setDictationState = function", "\n    };"),
        cut("window.setDictationText = function", "\n    };"),
    )
    script += cut("window.cancelDictation = function", "\n    };")
    script += r"""
var out = {};
inputEl.value = 'hello';
window.setDictationState('listening');
window.cancelDictation();
out.mid = inputEl.value;
inputEl.value = 'hello';
window.setDictationState('listening');
window.setDictationText('there', true);
window.cancelDictation();
out.after = inputEl.value;
console.log(JSON.stringify(out));
"""
    proc = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["mid"] == "hello" and out["after"] == "hello"
