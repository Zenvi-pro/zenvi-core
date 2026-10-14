"""The Captions dock: pick a HyperFrames caption style for the selected clip.

The dock is a front for the same ``add_captions_tool`` / ``remove_captions_tool``
the assistant uses, so there is one captioning path. The words shown are the
clip's own transcript; editing them fixes what the recogniser got wrong.
"""

from __future__ import annotations

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

# The recognisers the dock offers: what each is like, and how big a first download it is.
RECOGNISERS = (("whistle", "Whistle: fast, small", "18 MB"),
               ("parakeet", "Parakeet: more accurate, slower on short clips", "about 900 MB"))


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
    recogniserInstalled = pyqtSignal(str, str)  # recogniser, error ('' when it installed)

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
        self._fill_recognisers()
        if not hf.speech_supported():                   # Intel Macs: Parakeet is the only one there is
            self.recogniserLabel.hide()
            self.recogniserCombo.hide()

        buttons = QHBoxLayout()
        for button in (self.loadButton, self.generateButton, self.removeButton):
            buttons.addWidget(button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        for widget in (self.clipLabel, self.styleCombo, self.styleHint, self.behindSubject, self.recogniserLabel,
                       self.recogniserCombo, self.wordsEdit):
            layout.addWidget(widget)
        layout.addLayout(buttons)
        layout.addWidget(self.projectButton)
        layout.addWidget(self.statusLabel)

        self.styleCombo.currentIndexChanged.connect(self._show_style_hint)
        self.recogniserCombo.currentIndexChanged.connect(self._recogniser_picked)
        self.recogniserInstalled.connect(self._on_recogniser_installed)
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

    # -- speech recogniser ---------------------------------------------------

    def _fill_recognisers(self):
        """One entry per recogniser, saying whether it is installed or how big its download is."""
        _ = get_app()._tr
        st = hf.status()
        installed = {"whistle": bool(st.get("speech")), "parakeet": bool(st.get("parakeet"))}
        combo = self.recogniserCombo
        combo.blockSignals(True)
        combo.clear()
        for name, label, size in RECOGNISERS:
            combo.addItem("%s (%s)" % (_(label), _("installed") if installed[name] else _("download %s") % size),
                          name)
        combo.setCurrentIndex(max(0, combo.findData(hf.recogniser())))
        combo.blockSignals(False)
        self._installed = installed

    def _recogniser_picked(self, *_args):
        name = self.recogniserCombo.currentData()
        if not name or name == hf.recogniser() or self._busy:
            if self._busy:
                self._fill_recognisers()                # one job at a time: put the picker back
            return
        if self._installed.get(name):
            hf.choose_recogniser(name)
            return
        label = dict((n, "%s (%s)" % (get_app()._tr(lab), size)) for n, lab, size in RECOGNISERS)[name]
        if not _confirm_download(self, label):
            self._fill_recognisers()                    # declined: back to the one in use
            return
        _ = get_app()._tr
        self._begin(_("Downloading %s...") % label)

        def work():
            try:
                (hf.install_parakeet if name == "parakeet" else hf.install_speech)()
                error = ""
            except Exception as exc:
                log.warning("Captions dock: could not install the %s recogniser", name, exc_info=True)
                error = str(exc) or exc.__class__.__name__
            self.recogniserInstalled.emit(name, error)

        threading.Thread(target=work, name="zenvi-captions-dock", daemon=True).start()

    def _on_recogniser_installed(self, name, error):
        self._busy = False
        self._clock.stop()
        _ = get_app()._tr
        if error:
            self.statusLabel.setText(_("The speech recogniser could not be downloaded: %s") % error)
        else:
            hf.choose_recogniser(name)
            self.statusLabel.setText(_("Captions now use %s.") % name.capitalize())
        self._fill_recognisers()
        self._refresh_enabled()

    # -- selection ----------------------------------------------------------

    def selection_changed(self):
        from classes.query import Clip

        selected = list(getattr(get_app().window, "selected_clips", None) or [])
        clip = Clip.get(id=selected[0]) if len(selected) == 1 else None
        clip_id = str(clip.id) if clip else ""
        if clip_id != self._clip_id:
            self._clip_id = clip_id
            self.wordsEdit.clear()
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
            found = bool(clip) and tool.has_subject(clip)
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
        if self._busy or not self._clip_id:
            return
        _ = get_app()._tr
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
            return
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
        self.statusLabel.setText(message)
        if self._clip_id and self._words_for != self._clip_id:      # the selection moved on during the job
            self.load_words()
        self._refresh_enabled()
