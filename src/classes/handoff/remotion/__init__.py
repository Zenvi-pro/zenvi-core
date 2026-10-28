"""Remotion <-> Zenvi (handoff package C4).

Importing this package (``classes.handoff.plugins.load_plugins`` does) makes
Remotion links live and adds the menu entries:

* ``linked_media.register_provider(RemotionProvider())`` -- linked clips of
  kind ``remotion`` render with the project's own Remotion (Edit Props, Open
  Code, Open in Studio, Re-render, freshness);
* File > Import Project > Remotion Project... -- list a project's
  compositions, render them as linked clips, or restore a Zenvi export as
  native clips;
* File > Export Project > Remotion Project... -- the timeline as a working
  Remotion project that comes back losslessly.

Modules: ``detect`` (is it Remotion, entry, installed), ``sources`` (where a
composition's code lives), ``helper`` (+ ``helper.mjs``, the Node side),
``provider``, ``studio``, ``install``, ``importer``, ``exporter`` (+
``template/``), ``restore`` and ``dialogs`` (Qt, loaded on use). The editor
tools are in ``classes.editor_tools.handoff_remotion``. Zenvi never ships
Remotion (source-available, company licence above 3 people): it runs the
copy installed in the user's project.
"""

from __future__ import annotations

from classes.handoff import linked_media, ui_registry
from classes.handoff.remotion.provider import RemotionProvider

ACTION_ID = "remotion"
MENU_ORDER = 30


def _import_action(window) -> None:
    from classes.handoff.remotion import dialogs
    dialogs.import_remotion_project(window)


def _export_action(window) -> None:
    from classes.handoff.remotion import dialogs
    dialogs.export_remotion_project(window)


def register() -> None:
    """Register the Remotion provider and menu entries (idempotent; done at import)."""
    linked_media.register_provider(RemotionProvider())
    ui_registry.register_import_action(
        ACTION_ID, "Remotion Project...", _import_action, order=MENU_ORDER,
        tooltip="Bring a Remotion project in: its compositions as linked clips, or a Zenvi export as native clips")
    ui_registry.register_export_action(
        ACTION_ID, "Remotion Project...", _export_action, order=MENU_ORDER,
        tooltip="Write the timeline as a Remotion project that renders it and comes back to Zenvi losslessly")


register()
