"""Zenvi <-> Adobe Premiere Pro: File menu entries and Send To (loaded by ``classes.handoff.plugins``).

* File > Export Project > **Premiere Pro (.xml)...** -- FCP7 XML tuned for
  Premiere's importer (``classes.exporters.final_cut_pro``), with options to
  copy the media next to the XML and open the folder; titles always become
  transparent PNG stills (Premiere cannot read SVG).
* File > Import Project > **Premiere Pro XML...** -- a Premiere sequence
  (File > Export > Final Cut Pro XML in Premiere, or ``premiere_export_xml``)
  comes back as one undo step (``classes.importers.final_cut_pro``).
* File > Send To > **Premiere Pro** (enabled while Premiere runs the Zenvi
  Link panel) -- exports into the project's assets folder, so the title
  stills Premiere links to stay put, and asks Premiere to import it with
  ``premiere_import_xml``; reports the new sequence.

Everything slow (export, Zenvi Link calls) runs on the handoff executor; the
menu handlers only show dialogs on the GUI thread.
"""

from __future__ import annotations

import datetime
import os
import re
import threading
from typing import Any, Callable, Dict, List, Optional

from classes.handoff.ui_registry import register_export_action, register_import_action, register_send_action
from classes.logger import log

APP = "premiere"
IMPORT_TOOL = "premiere_import_xml"
SEND_FOLDER = "premiere"
_PATH_ARGS = ("path", "xml_path", "file_path", "file", "xml")
_arg_cache: Dict[Any, Optional[str]] = {}
_arg_lock = threading.Lock()


class SendError(Exception):
    """Send To Premiere Pro failed; the message says why and what to do."""


def _tr(text: str) -> str:
    try:
        from classes.app import get_app
        return get_app()._tr(text)
    except Exception:
        return text


# ---------------------------------------------------------------------------
# Send To (blocking parts: off the GUI thread)
# ---------------------------------------------------------------------------

def send_folder(name: str, project_path: Optional[str], *, now: Optional[datetime.datetime] = None) -> str:
    """``<project>_assets/premiere/<stamp> <name>`` (the user folder while the project is unsaved)."""
    from classes import info
    from classes.assets import get_assets_path
    root = get_assets_path(project_path, create_paths=False) if project_path else info.USER_PATH
    stamp = (now or datetime.datetime.now()).strftime("%Y%m%d-%H%M%S")
    safe = re.sub(r"[^A-Za-z0-9._ -]+", "_", name or "Timeline").strip(" ._") or "Timeline"
    return os.path.join(root or info.USER_PATH, SEND_FOLDER, f"{stamp} {safe[:60]}")


def _path_arg(base_dir: Optional[str] = None) -> str:
    """The argument name the connected Premiere's ``premiere_import_xml`` takes for the file (``path``)."""
    from classes.handoff import adobe_link
    try:
        host = adobe_link.get_host(APP, base_dir, probe=False)
        key = (base_dir, host.pid, host.started_at)
        with _arg_lock:
            if key in _arg_cache:
                return _arg_cache[key] or "path"
        name = None
        for tool in adobe_link.list_host_tools(APP, base_dir):
            if tool.get("name") == IMPORT_TOOL:
                props = (tool.get("inputSchema") or {}).get("properties") or {}
                name = next((n for n in _PATH_ARGS if n in props), None)
                break
        with _arg_lock:
            _arg_cache.clear()
            _arg_cache[key] = name
        return name or "path"
    except adobe_link.LinkHostError:
        return "path"


def send_to_premiere(snapshot, *, base_dir: Optional[str] = None,
                     on_progress: Optional[Callable[[float, str], None]] = None,
                     should_cancel: Optional[Callable[[], bool]] = None,
                     folder: Optional[str] = None, translate: Optional[Callable[[str], str]] = None) -> dict:
    """Export *snapshot* and open it in the connected Premiere Pro (blocking; off the GUI thread).

    Returns ``{xml, media_dir, sequence_name, sequences, offline, warnings,
    host_summary}``. Raises SendError (with how to connect when Premiere is
    not reachable) or ``final_cut_pro.ExportError``.
    """
    from classes.exporters import final_cut_pro as fcp
    from classes.handoff import adobe_link
    host = adobe_link.get_host(APP, base_dir)
    if not host.connected:
        raise SendError(f"Premiere Pro is not connected ({host.reason}). {adobe_link.connect_hint(APP)}")
    folder = folder or send_folder(snapshot.name, snapshot.project_path)
    xml_path = os.path.join(folder, (re.sub(r"[^A-Za-z0-9._ -]+", "_", snapshot.name or "Timeline") or "Timeline")
                            + ".xml")
    result = fcp.export_timeline(snapshot, xml_path, media_dir=os.path.join(folder, "media"),
                                 on_progress=on_progress, should_cancel=should_cancel, translate=translate)
    if on_progress is not None:
        on_progress(0.9, (translate or _tr)("Premiere Pro is importing the sequence"))
    args = {_path_arg(base_dir): result.path}
    try:
        reply = adobe_link.call_host_tool(APP, IMPORT_TOOL, args, timeout=None, base_dir=base_dir)
    except adobe_link.HostNotConnected as exc:
        raise SendError(str(exc)) from None
    except adobe_link.LinkHostError as exc:
        raise SendError(f"Premiere Pro could not import the sequence ({exc.code}): {exc}. The XML is at "
                        f"{result.path}; import it in Premiere with File > Import") from None
    receipt = reply.receipt or {}
    if reply.is_error:
        raise SendError(f"Premiere Pro refused the import: {reply.summary}. The XML is at {result.path}")
    data = receipt.get("data") or {}
    sequences = [s for s in (data.get("sequences") or []) if isinstance(s, dict)]
    name = str(sequences[0].get("name")) if sequences and sequences[0].get("name") else result.sequence_name
    offline = [o for o in (data.get("offline") or []) if isinstance(o, dict)]
    warnings: List[str] = list(result.warnings) + [str(w) for w in (receipt.get("warnings") or [])]
    log.info("Sent %s to Premiere Pro as %r", result.path, name)
    return {"xml": result.path, "media_dir": result.media_dir, "sequence_name": name, "sequences": sequences,
            "offline": offline, "warnings": warnings, "host_summary": reply.summary, "counts": result.counts}


# ---------------------------------------------------------------------------
# Menu handlers (GUI thread: dialogs, then a job)
# ---------------------------------------------------------------------------

def build_export_dialog(window):
    """The export options dialog: (dialog, path edit, collect checkbox, open-folder checkbox)."""
    from classes.exporters.final_cut_pro import default_export_path
    from qt_api import (QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
                        QPushButton, QVBoxLayout)
    _ = _tr
    dialog = QDialog(window)
    dialog.setObjectName("premiereExportDialog")
    dialog.setWindowTitle(_("Export to Premiere Pro"))
    layout = QVBoxLayout(dialog)
    layout.addWidget(QLabel(_("Write the timeline as Final Cut Pro XML that Premiere Pro imports "
                              "(File > Import in Premiere).")))
    row = QHBoxLayout()
    path_edit = QLineEdit(default_export_path(".xml", " (Premiere)"))
    browse = QPushButton(_("Browse..."))

    def choose():
        chosen = QFileDialog.getSaveFileName(dialog, _("Export to Premiere Pro"), path_edit.text(),
                                             _("Premiere Pro XML (*.xml)"))[0]
        if chosen:
            path_edit.setText(chosen)

    browse.clicked.connect(choose)
    row.addWidget(path_edit)
    row.addWidget(browse)
    layout.addLayout(row)
    collect = QCheckBox(_("Copy the media next to the XML (for moving the edit to another computer)"))
    layout.addWidget(collect)
    open_folder = QCheckBox(_("Open the folder when done"))
    layout.addWidget(open_folder)
    note = QLabel(_("Titles are rendered as transparent PNG stills next to the XML: Premiere cannot read SVG."))
    note.setWordWrap(True)
    layout.addWidget(note)
    buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    return dialog, path_edit, collect, open_folder


def _export_dialog(window) -> Optional[dict]:
    """Ask for the XML path and options. Returns {path, collect_media, open_folder} or None."""
    from qt_api import QDialog
    dialog, path_edit, collect, open_folder = build_export_dialog(window)
    if dialog.exec_() != QDialog.Accepted:
        return None
    path = path_edit.text().strip()
    if not path:
        return None
    if not path.lower().endswith(".xml"):
        path += ".xml"
    return {"path": path, "collect_media": collect.isChecked(), "open_folder": open_folder.isChecked()}


def start_export(window):
    """File > Export Project > Premiere Pro: the options dialog, then the export job (returned)."""
    from classes.exporters.final_cut_pro import run_export_job
    choice = _export_dialog(window)
    if not choice:
        return None
    return run_export_job(window, choice["path"], collect_media=choice["collect_media"],
                          open_folder=choice["open_folder"], title=_tr("Export to Premiere Pro"),
                          done_message=_tr("Exported %s for Premiere Pro") % os.path.basename(choice["path"]))


def start_import(window):
    """File > Import Project > Premiere Pro XML: choose the file, then the import job (returned)."""
    from classes import info
    from classes.app import get_app
    from classes.importers.final_cut_pro import run_import_job
    from qt_api import QFileDialog
    _ = _tr
    start = os.path.dirname(get_app().project.current_filepath or "") or info.HOME_PATH
    path = QFileDialog.getOpenFileName(window, _("Import Premiere Pro XML"), start,
                                       _("Premiere Pro / Final Cut Pro XML (*.xml)"))[0]
    if not path:
        return None
    return run_import_job(window, path, prompt=True, title=_("Import Premiere Pro XML"))


def start_send(window):
    """File > Send To > Premiere Pro: snapshot now, export + Zenvi Link import in a job (returned)."""
    from classes.handoff import jobs
    from classes.handoff.timeline_view import TimelineSnapshot
    from qt_api import QMessageBox
    _ = _tr
    snapshot = TimelineSnapshot.from_app()
    if not snapshot.clips:
        QMessageBox.information(window, _("Send to Premiere Pro"), _("The timeline has no clips to send."))
        return None

    def work(job):
        return send_to_premiere(snapshot, on_progress=lambda f, m: job.report(f, m),
                                should_cancel=job.should_cancel, translate=_)

    def done(job):
        from windows.handoff_menus import notify
        if job.state == jobs.CANCELLED:
            notify(window, _("Send to Premiere Pro cancelled"))
            return
        if job.error is not None:
            QMessageBox.warning(window, _("Send to Premiere Pro"), str(job.error))
            return
        result = job.result
        notify(window, _("Opened “%s” in Premiere Pro") % result["sequence_name"])
        lines = []
        if result["offline"]:
            lines.append(_("Premiere could not find %d media file(s): %s") % (
                len(result["offline"]), ", ".join(str(o.get("name")) for o in result["offline"][:5])))
        if result["warnings"]:
            lines.append(_("Some things could not be carried over exactly:"))
            lines.extend("• " + w for w in result["warnings"][:12])
        if lines:
            QMessageBox.information(window, _("Send to Premiere Pro"), "\n".join(lines))

    return jobs.submit_job(work, label=_("Sending to Premiere Pro"), kind="premiere-send", on_done=done)


def _export_action(window) -> None:
    start_export(window)


def _import_action(window) -> None:
    start_import(window)


def _send_action(window) -> None:
    start_send(window)


register_export_action("premiere", "Premiere Pro (.xml)...", _export_action, order=20,
                       tooltip="Final Cut Pro XML tuned for Adobe Premiere Pro (titles as PNG stills)")
register_import_action("premiere_xml", "Premiere Pro XML...", _import_action, order=20,
                       tooltip="Bring a Premiere Pro sequence (exported as Final Cut Pro XML) in as one undo step")
register_send_action("premiere", "Premiere Pro", APP, _send_action, order=20,
                     tooltip="Open the timeline as a new sequence in the connected Premiere Pro")
