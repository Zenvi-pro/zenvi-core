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

from classes.handoff import linked_media
from classes.handoff.remotion.provider import RemotionProvider


def register() -> None:
    """Register the Remotion link provider (idempotent; done at import)."""
    linked_media.register_provider(RemotionProvider())


register()
