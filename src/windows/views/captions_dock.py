"""The Captions dock: pick a HyperFrames caption style for the selected clip.

The dock is a front for the same ``add_captions_tool`` / ``remove_captions_tool``
the assistant uses, so there is one captioning path. The words shown are the
clip's own transcript; editing them fixes what the recogniser got wrong.
"""

from __future__ import annotations

import os
import threading

from qt_api import (
    pyqtSignal,
    QTimer,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from classes import hyperframes_captions as cap
from classes import hyperframes_local as hf
from classes.app import get_app
from classes.logger import log



def _models_on_disk() -> dict:
    """What the pickers show, from the files being there. Not hf.status(): that hashes models and waits on a
    running transcription or download, and the dock asks on the GUI thread (installs and runs verify hashes)."""
    def there(path):                              # a link is never trusted, as the hash check refuses it too
        return os.path.isfile(path) and not os.path.islink(path)

    return {"speech": there(hf.speech_engine()) and there(hf.speech_model()),
            "parakeet": there(hf._parakeet_model()),
            "rvm": there(hf.rvm_model()) and hf._ort_installed(),
            "u2net": there(hf._u2net_model()),
            "recogniser": hf.recogniser(), "matting": hf.matting()}


def _confirm_download(parent, label) -> bool:
    from qt_api import QMessageBox

    _ = get_app()._tr
    return QMessageBox.question(
        parent, _("Download speech recogniser"),
        _("%s is not on this computer yet. Download it now? It is a one-time download and is "
          "used for caption word timing from then on.") % label,
        QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes) == QMessageBox.Yes


def status_line(out: str) -> str:
    """What to show for a tool result: the receipt's summary, not the receipt."""
    from classes.agent_tools.receipt import parse_receipt

    receipt = parse_receipt(out)
    return str(receipt["summary"] if receipt else out).split("\n", 1)[0]


class CaptionsDock(QWidget):
    """Style picker, editable transcript and Generate / Remove for the selected clip."""

    finished = pyqtSignal(str)                 # status line of a finished tool call
    wordsLoaded = pyqtSignal(str, str, str)    # clip id, words, status line
    subjectChecked = pyqtSignal(str, bool)     # clip id, whether there is a speaker to put captions behind
    progress = pyqtSignal(str)                 # the stage a running caption job has reached
    modelInstalled = pyqtSignal(str, str, str)  # picker, model, error ('' when it installed)

    def __init__(self, parent=None):
        super().__init__(parent)
        _ = get_app()._tr
        self._clip_id = ""
        self._busy = False
        self._subjects = {}                    # clip id -> has a speaker in shot
        self._words_for = ""                   # the clip whose words were last read
        self._stage, self._seconds = "", 0     # what the running job is doing, and for how long
        self._clock = QTimer(self)
        self._clock.setInterval(1000)

        self.clipLabel = QLabel(_("Select a clip with speech on the timeline."))
        self.clipLabel.setWordWrap(True)
        self.styleCombo = QComboBox()
        for key, style in cap.STYLES.items():
            self.styleCombo.addItem(_(style["label"]), key)
        self.styleCombo.setCurrentIndex(self.styleCombo.findData(cap.DEFAULT_STYLE))
        self.styleHint = QLabel()
        self.styleHint.setWordWrap(True)
        self.behindSubject = QCheckBox(_("Behind the speaker (slower)"))
        self.behindSubject.setToolTip(_("Cuts the speaker out and places them on a track above the captions."))
        self.wordsEdit = QPlainTextEdit()
        self.wordsEdit.setPlaceholderText(_("The clip's words appear here. Edit them to fix the captions."))
        self.loadButton = QPushButton(_("Load words"))
        self.generateButton = QPushButton(_("Generate captions"))
        self.removeButton = QPushButton(_("Remove"))
        self.projectButton = QPushButton(_("Caption whole project"))
        self.projectButton.setToolTip(_("Captions every clip on the timeline that has speech, in this style."))
        self.statusLabel = QLabel()
        self.statusLabel.setWordWrap(True)
        self.recogniserLabel = QLabel(_("Speech recogniser"))
        self.recogniserCombo = QComboBox()
        self.recogniserCombo.setToolTip(_("Times each spoken word for the captions. Both run on this computer."))
        self.mattingLabel = QLabel(_("Cut-out model"))
        self.mattingCombo = QComboBox()
        self.mattingCombo.setToolTip(_("Cuts the speaker out of the clip. Both run on this computer."))
        self._installed = {}
        self._fill_picker("recogniser")
        self._fill_picker("matting")
        self._show_matting()
        if not hf.speech_supported():                   # Intel Macs: Parakeet is the only one there is
            self.recogniserLabel.hide()
            self.recogniserCombo.hide()

        buttons = QHBoxLayout()
        for button in (self.loadButton, self.generateButton, self.removeButton):
            buttons.addWidget(button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        for widget in (self.clipLabel, self.styleCombo, self.styleHint, self.behindSubject, self.mattingLabel,
                       self.mattingCombo, self.recogniserLabel, self.recogniserCombo, self.wordsEdit):
            layout.addWidget(widget)
        layout.addLayout(buttons)
        layout.addWidget(self.projectButton)
        layout.addWidget(self.statusLabel)

        self.styleCombo.currentIndexChanged.connect(self._show_style_hint)
        # activated: what the user picks, including the entry already shown
        self.recogniserCombo.activated.connect(lambda *_a: self._picker_picked("recogniser"))
        self.mattingCombo.activated.connect(lambda *_a: self._picker_picked("matting"))
        self.modelInstalled.connect(self._on_model_installed)
        self.behindSubject.toggled.connect(self._show_matting)
        self.loadButton.clicked.connect(self.load_words)
        self.generateButton.clicked.connect(self.generate)
        self.removeButton.clicked.connect(self.remove)
        self.projectButton.clicked.connect(self.generate_project)
        self.progress.connect(self._on_progress)
        self._clock.timeout.connect(self._tick)
        from classes.editor_tools import titles_text_captions as tool
        tool.on_progress = self.progress.emit           # emitted from the worker thread, delivered here
        self.finished.connect(self._on_finished)
        self.wordsLoaded.connect(self._on_words_loaded)
        self.subjectChecked.connect(self._on_subject_checked)
        get_app().window.SelectionChanged.connect(self.selection_changed)
        self._show_style_hint()
        self.selection_changed()

    # -- model pickers: speech recogniser and speaker cut-out ----------------

    def _pickers(self) -> dict:
        """picker -> its combo, and per model: label, what a first use downloads, how to fetch it (None:
        HyperFrames fetches it itself on first use), and how status() says it is installed. Translated."""
        _ = get_app()._tr
        return {
            "recogniser": {"combo": self.recogniserCombo, "choose": hf.choose_recogniser, "current": "recogniser",
                           "what": _("speech recogniser"), "models": {
                "whistle": (_("Whistle: fast, small"), _("download 18 MB"), hf.install_speech, "speech"),
                "parakeet": (_("Parakeet: more accurate, slower on short clips"), _("download about 900 MB"),
                             hf.install_parakeet, "parakeet")}},
            "matting": {"combo": self.mattingCombo, "choose": hf.choose_matting, "current": "matting",
                        "what": _("cut-out model"), "models": {
                "rvm": (_("RVM: fast, steady, uses the GPU"), _("download 15 MB + ONNX runtime"),
                        hf.install_rvm, "rvm"),
                "u2net": (_("HyperFrames built-in (U2-Net): slower"), _("downloads 176 MB on first use"),
                          None, "u2net")}},
        }

    def _fill_recognisers(self):
        self._fill_picker("recogniser")

    def _fill_picker(self, kind):
        """One entry per model, saying whether it is installed or what using it first downloads."""
        _ = get_app()._tr
        spec = self._pickers()[kind]
        st = _models_on_disk()                          # a deleted file shows as a download
        installed = {name: bool(st.get(key)) for name, (_l, _s, _i, key) in spec["models"].items()}
        combo = spec["combo"]
        combo.blockSignals(True)
        combo.clear()
        for name, (label, size, _install, _key) in spec["models"].items():
            combo.addItem("%s (%s)" % (label, _("installed") if installed[name] else size), name)
        combo.setCurrentIndex(max(0, combo.findData(st.get(spec["current"]))))
        combo.blockSignals(False)
        self._installed[kind] = installed

    def _show_matting(self, *_args):
        """The cut-out model only matters with the speaker cut out."""
        shown = self.behindSubject.isChecked()
        self.mattingLabel.setVisible(shown)
        self.mattingCombo.setVisible(shown)
        if shown:
            self._fill_picker("matting")

    def _picker_picked(self, kind):
        spec = self._pickers()[kind]
        combo = spec["combo"]
        name = combo.currentData()
        self._fill_picker(kind)                         # what is installed now, not when the dock was built
        if not name or self._busy:                      # one job at a time: the picker stays as it was
            return
        combo.setCurrentIndex(combo.findData(name))
        label, size, install, _key = spec["models"][name]
        if self._installed[kind].get(name) or install is None:
            spec["choose"](name)
            return
        _ = get_app()._tr
        self._busy = True                               # before the dialog: it runs its own event loop
        self._refresh_enabled()
        if not _confirm_download(self, "%s (%s)" % (label, size)):
            self._busy = False
            self._refresh_enabled()
            self._fill_picker(kind)                     # declined: back to the one in use
            return
        self._begin(_("Downloading %s (%s)...") % (label, size))

        def work():
            try:
                install()
                error = ""
            except Exception as exc:
                log.warning("Captions dock: could not install %s", name, exc_info=True)
                error = str(exc) or exc.__class__.__name__
            self.modelInstalled.emit(kind, name, error)

        threading.Thread(target=work, name="zenvi-captions-dock", daemon=True).start()

    def _on_model_installed(self, kind, name, error):
        self._busy = False
        self._clock.stop()
        _ = get_app()._tr
        spec = self._pickers()[kind]
        if error:
            self.statusLabel.setText(_("The %s could not be downloaded: %s") % (spec["what"], error))
        else:
            spec["choose"](name)
            self.statusLabel.setText(_("Now using %s.") % spec["models"][name][0])
        self._fill_picker(kind)
        self._refresh_enabled()
        if not error and self._clip_id and self._words_for != self._clip_id:      # read the words waiting for it
            self.load_words()

    # -- selection ----------------------------------------------------------

    def showEvent(self, event):
        super().showEvent(event)
        if self._words_for != self._clip_id:            # selected, or moved on, while the dock was hidden
            self.load_words()

    def selection_changed(self):
        from classes.query import Clip

        selected = list(getattr(get_app().window, "selected_clips", None) or [])
        clip = Clip.get(id=selected[0]) if len(selected) == 1 else None
        clip_id = str(clip.id) if clip else ""
        if clip_id != self._clip_id:
            self._clip_id = clip_id
            self.wordsEdit.clear()
            self._words_for = ""                        # the box holds no clip's words now
            _ = get_app()._tr
            self.clipLabel.setText(_("Captions for: %s") % (clip.data.get("title") or clip_id) if clip
                                   else _("Select a clip with speech on the timeline."))
            if clip_id in self._subjects:
                self._on_subject_checked(clip_id, self._subjects[clip_id])
            elif clip:
                self._on_subject_checked(clip_id, True)          # offered until the check says otherwise
                threading.Thread(target=self._check_subject, args=(clip_id,), name="zenvi-captions-dock",
                                 daemon=True).start()
            else:
                self.behindSubject.setEnabled(True)     # nothing selected: the choice is for the whole project
            if clip:
                self.load_words()                       # the words are there to read and fix straight away
        self._refresh_enabled()

    def _check_subject(self, clip_id):
        from classes.editor_tools import titles_text_captions as tool
        from classes.query import Clip
        try:
            clip = Clip.get(id=clip_id)
            # The index only: a frame probe here could start a model download on a mere selection.
            # Not recorded counts as someone there; the tool checks the frames when it runs.
            people = tool._people_on_camera(clip) if clip else 0
            found = bool(clip) and (people is None or people > 0)
        except Exception:
            log.warning("Captions dock: could not check the clip for a speaker", exc_info=True)
            return
        self.subjectChecked.emit(clip_id, found)

    def _on_subject_checked(self, clip_id, found):
        self._subjects[clip_id] = found
        if clip_id != self._clip_id:                    # the selection may have moved on meanwhile
            return
        _ = get_app()._tr
        self.behindSubject.setEnabled(found)             # stays ticked: it still applies to the whole project
        self.behindSubject.setToolTip(
            _("Cuts the speaker out and places them on a track above the captions.") if found
            else _("No speaker was found in this clip, so there is nobody to put the captions behind."))

    def _refresh_enabled(self):
        ready = bool(self._clip_id) and not self._busy
        for widget in (self.loadButton, self.generateButton, self.removeButton):
            widget.setEnabled(ready)
        self.projectButton.setEnabled(not self._busy)
        self.recogniserCombo.setEnabled(not self._busy)
        self.mattingCombo.setEnabled(not self._busy)

    # -- status: the stage a job is at, with a running clock so a long one is visibly alive ----

    def _begin(self, status):
        self._busy = True
        self._stage, self._seconds = status, 0
        self.statusLabel.setText(status)
        self._clock.start()
        self._refresh_enabled()

    def _on_progress(self, message):
        if self._busy:
            self._stage = get_app()._tr(message)
            self._tick(0)

    def _tick(self, step=1):
        self._seconds += step
        self.statusLabel.setText("%s  %d:%02d" % (self._stage, self._seconds // 60, self._seconds % 60))

    def _show_style_hint(self):
        style = cap.STYLES.get(self.styleCombo.currentData() or "", {})
        self.styleHint.setText(get_app()._tr(style.get("description", "")))

    # -- actions (the tools do the work, off the GUI thread) ------------------

    def _run(self, status, tool_name, args, needs_clip=True):
        if self._busy or (needs_clip and not self._clip_id):
            return
        self._begin(status)

        def work():
            from classes.tool_handlers import execute_tool
            try:
                out = str(execute_tool(tool_name, args))
            except Exception as exc:
                log.error("Captions dock: %s failed", tool_name, exc_info=True)
                out = "Error: %s" % exc
            self.finished.emit(status_line(out))

        threading.Thread(target=work, name="zenvi-captions-dock", daemon=True).start()

    def load_words(self):
        # A read can download the recogniser: only on screen, and only once it is in place (the picker or Generate)
        if self._busy or not self._clip_id or not self.isVisible():
            return
        _ = get_app()._tr
        st = _models_on_disk()
        if not st["parakeet" if st["recogniser"] == "parakeet" else "speech"]:
            self.statusLabel.setText(_("Download the speech recogniser to read this clip's words."))
            return
        self._begin(_("Reading the clip's speech..."))
        clip_id = self._clip_id

        def work():
            from classes.editor_tools import titles_text_captions as tool
            from classes.query import Clip
            try:
                words = tool.clip_caption_words(Clip.get(id=clip_id))
                text = " ".join(w["text"] for w in words)
                message = _("%d words loaded.") % len(words) if words else _("No speech was found in this clip.")
            except Exception as exc:
                text, message = "", "Error: %s" % exc
            self.wordsLoaded.emit(clip_id, text, message)

        threading.Thread(target=work, name="zenvi-captions-dock", daemon=True).start()

    def _on_words_loaded(self, clip_id, text, message):
        self._busy = False
        self._clock.stop()
        self._words_for = clip_id
        if clip_id == self._clip_id:
            if text:
                self.wordsEdit.setPlainText(text)
            self.statusLabel.setText(message)
        elif self._clip_id:                             # the selection moved on meanwhile: read that clip now
            self.load_words()
        else:
            self.statusLabel.clear()
        self._refresh_enabled()

    def generate(self):
        _ = get_app()._tr
        self._run(_("Rendering captions on this computer..."), "add_captions_tool", {
            "timeline_clip_id": self._clip_id,
            "style": self.styleCombo.currentData(),
            "behind_subject": self.behindSubject.isChecked() and self.behindSubject.isEnabled(),
            "text": self.wordsEdit.toPlainText().strip(),
        })

    def generate_project(self):
        _ = get_app()._tr
        self._run(_("Captioning the whole project..."), "add_captions_tool", {
            "whole_project": True,
            "style": self.styleCombo.currentData(),
            # Per clip the tool leaves out the cut-out where nobody is on camera.
            "behind_subject": self.behindSubject.isChecked(),
        }, needs_clip=False)

    def remove(self):
        _ = get_app()._tr
        self._run(_("Removing captions..."), "remove_captions_tool", {"timeline_clip_id": self._clip_id})

    def _on_finished(self, message):
        self._busy = False
        self._clock.stop()
        if self._clip_id and self._words_for != self._clip_id:      # the selection moved on during the job
            self.load_words()
        self.statusLabel.setText(message)               # last: the result outranks a reload's hint
        self._refresh_enabled()
