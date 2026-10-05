"""
 @file
 @brief The "Linked Source" submenu for linked clips (timeline clip menu and Project Files menus).

 A file with a ``zenvi_link`` (a Remotion / HyperFrames composition or After
 Effects comp rendered into the project) gets: its state, Edit Props...,
 Open Code, Open in Studio (when the source has one), Re-render and Unlink.
 Slow work (renders, starting editors) runs off the GUI thread; unlinking is
 one undo step.
"""

from __future__ import annotations

import os
from typing import Optional

from qt_api import QMessageBox

from classes.app import get_app
from classes.logger import log


def _tr(text: str) -> str:
    return get_app()._tr(text)


def _file_and_link(file_id: str):
    from classes.handoff.linked_media import read_link
    from classes.query import File
    f = File.get(id=str(file_id or ""))
    if not f:
        return None, None
    return f, read_link(f.data)


def state_caption(link: dict, check) -> str:
    """The submenu's first (disabled) line: kind, composition and freshness."""
    from classes.handoff.linked_media import kind_label, provider_for
    from windows.linked_clip_dialog import state_text
    kind = str(link.get("kind") or "")
    comp = (link.get("source") or {}).get("composition") or ""
    head = kind_label(kind) + (" · %s" % comp if comp else "")
    if provider_for(kind) is None:
        return "%s — %s" % (head, _tr("not checked: this build has no %s renderer") % kind_label(kind))
    if check is None:
        return head
    return "%s — %s" % (head, state_text(getattr(check, "state", None), "", _tr))


def add_linked_source_menu(window, menu, file_id: str, parent=None):
    """Append "Linked Source" to *menu* when *file_id* is a linked file; returns the submenu or None."""
    from classes.handoff import linked_media
    from windows.views.menu import StyledContextMenu
    try:
        f, link = _file_and_link(file_id)
    except Exception:
        log.debug("linked source menu skipped", exc_info=True)
        return None
    if f is None or link is None:
        return None
    kind = str(link.get("kind") or "")
    provider = linked_media.provider_for(kind)
    status = getattr(window, "handoff_status", None)
    check = (getattr(status, "last_checks", {}) or {}).get(f.id)
    rendering = linked_media.is_rendering(f.id)

    sub = StyledContextMenu(title=_tr("Linked Source"), parent=parent or menu)
    sub.setObjectName("menuLinkedSource")
    caption = sub.addAction(state_caption(link, check))
    caption.setEnabled(False)
    sub.addSeparator()

    edit = sub.addAction(_tr("Edit Props..."))
    edit.setEnabled(provider is not None and not rendering)
    edit.triggered.connect(lambda checked=False: edit_props(window, f.id))

    source = link.get("source") or {}
    can_open_code = provider is not None or bool(source.get("file") or source.get("aep") or source.get("project_dir"))
    open_code = sub.addAction(_tr("Open Code") if kind != "aftereffects" else _tr("Open in After Effects"))
    open_code.setEnabled(can_open_code)
    open_code.triggered.connect(lambda checked=False: open_linked(window, f.id, "code"))
    if linked_media.supports_studio(kind):
        studio = sub.addAction(_tr("Open in Studio"))
        studio.triggered.connect(lambda checked=False: open_linked(window, f.id, "studio"))

    sub.addSeparator()
    rerender = sub.addAction(_tr("Re-render"))
    rerender.setEnabled(provider is not None and not rendering)
    if provider is None:
        rerender.setToolTip(_tr("This Zenvi build has no renderer for %s links") % linked_media.kind_label(kind))
    rerender.triggered.connect(lambda checked=False: _rerender(window, f.id))
    sub.addSeparator()
    unlink = sub.addAction(_tr("Unlink"))
    unlink.setEnabled(not rendering)
    unlink.triggered.connect(lambda checked=False: unlink_file(window, f.id))
    menu.addMenu(sub)
    return sub


def _rerender(window, file_id: str) -> None:
    from windows.handoff_menus import rerender_files
    rerender_files(window, [file_id])


def edit_props(window, file_id: str) -> None:
    """Linked Source > Edit Props...: the props dialog, then Apply & Re-render (one undo step)."""
    from classes.handoff import linked_media
    from windows.handoff_menus import rerender_files
    from windows.linked_clip_dialog import LinkedClipDialog
    f, link = _file_and_link(file_id)
    if f is None or link is None:
        return
    status = getattr(window, "handoff_status", None)
    check = (getattr(status, "last_checks", {}) or {}).get(f.id)
    dialog = LinkedClipDialog(link, check=check.as_dict() if check is not None else None,
                              name=str(f.data.get("name") or ""),
                              can_render=linked_media.provider_for(str(link.get("kind"))) is not None, parent=window)
    if dialog.exec_() and dialog.props() is not None:
        rerender_files(window, [f.id], props=dialog.props(), replace_props=True)


def open_linked(window, file_id: str, target: str = "code") -> None:
    """Open Code / Open in Studio, off the GUI thread; failures in a message box."""
    from classes.handoff import jobs, linked_media, open_source
    f, link = _file_and_link(file_id)
    if f is None or link is None:
        return
    kind = str(link.get("kind") or "")

    def work(job):
        provider = linked_media.provider_for(kind)
        if target == "studio":
            provider.open_studio(link)
            return
        if provider is not None:
            provider.open_source(link)
            return
        source = link.get("source") or {}
        root, rel = source.get("project_dir"), source.get("file")
        path = os.path.join(root, rel) if root and rel else (source.get("aep") or root)
        if not path:
            raise linked_media.LinkError(_tr("this link records no source file to open"))
        open_source.open_in_editor(path, source.get("line"))

    def done(job):
        if job.error is not None:
            QMessageBox.warning(window, _tr("Linked Source"), _tr("Could not open the source: %s") % job.error)

    jobs.submit_job(work, label=_tr("Opening source"), interactive=True, on_done=done)


def unlink_file(window, file_id: str) -> Optional[dict]:
    """Linked Source > Unlink: keep the media, drop the link (one undo step)."""
    from classes.handoff import linked_media
    try:
        result = linked_media.unlink(file_id)
    except linked_media.LinkError as exc:
        QMessageBox.warning(window, _tr("Linked Source"), str(exc))
        return None
    from windows.handoff_menus import notify
    notify(window, _tr("Unlinked; the clip keeps its rendered media (Undo restores the link)"))
    status = getattr(window, "handoff_status", None)
    if status is not None:
        status.check_soon(force=True)
    return result
