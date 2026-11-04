"""HyperFrames <-> Zenvi (handoff package C5).

Importing this package (``classes.handoff.plugins.load_plugins`` does) makes
HyperFrames links live and adds the menu entries:

* ``linked_media.register_provider(HyperFramesProvider())`` -- linked clips of
  kind ``hyperframes`` render with the HyperFrames CLI (the project's own
  version), with Edit Props (composition variables), Open Code, Open in
  Studio (``hyperframes preview``), Re-render and freshness;
* File > Import Project > HyperFrames Project... -- media primitives as
  native clips, compositions and scripted graphics as linked clips, the
  whole project as one linked clip (flatten), or a Zenvi export restored;
* File > Export Project > HyperFrames Project... -- the timeline as a
  HyperFrames project that previews, lints and renders, and comes back
  losslessly.

Modules: ``gsap`` (timeline scripts), ``parser`` (HTML, CSS, timing),
``mapping`` (primitive -> Zenvi clip), ``cli`` (the HyperFrames CLI and
Studio), ``wrappers`` (render entry files), ``provider``, ``importer``,
``restore``, ``exporter`` and ``dialogs`` (Qt, loaded on use). The editor
tools are in ``classes.editor_tools.handoff_hyperframes``.

HyperFrames (Apache-2.0) is never shipped: Zenvi runs the project's own
install, Zenvi's local HyperFrames setup, or ``npx hyperframes@<version>``,
always with its telemetry and self-update turned off. GSAP is loaded from
its CDN by the compositions themselves and never ships with Zenvi.
"""

from __future__ import annotations

from classes.handoff import ui_registry
from classes.handoff.hyperframes import provider as _provider

ACTION_ID = "hyperframes"
MENU_ORDER = 40


def _import_action(window) -> None:
    from classes.handoff.hyperframes import dialogs
    dialogs.import_hyperframes_project(window)


def _export_action(window) -> None:
    from classes.handoff.hyperframes import dialogs
    dialogs.export_hyperframes_project(window)


def register() -> None:
    """Register the HyperFrames provider and menu entries (idempotent; done at import)."""
    _provider.register()
    ui_registry.register_import_action(
        ACTION_ID, "HyperFrames Project...", _import_action, order=MENU_ORDER,
        tooltip="Bring a HyperFrames project in: its media as editable clips, compositions as linked clips")
    ui_registry.register_export_action(
        ACTION_ID, "HyperFrames Project...", _export_action, order=MENU_ORDER,
        tooltip="Write the timeline as a HyperFrames project that renders it and comes back to Zenvi losslessly")


register()
