"""A re-encoded import's thumbnail is made by the thumbnail worker, not on the GUI thread.

_refresh_imported_file_thumbnail runs inside a GUI-thread hop. It used to call
GenerateThumbnail there, which decodes a frame (a minute on long-GOP media);
FileUpdated already has the worker regenerate it (develop #231).
"""

from unittest.mock import MagicMock, patch

from classes import tool_handlers


def test_refresh_only_asks_the_worker_through_file_updated():
    app = MagicMock()
    with patch.object(tool_handlers, "_get_app", return_value=app), \
            patch("classes.thumbnail.GenerateThumbnail") as generate:
        tool_handlers._refresh_imported_file_thumbnail("F1", "/media/clip_reencoded.mp4")
    app.window.FileUpdated.emit.assert_called_once_with("F1")
    generate.assert_not_called()


def test_nothing_to_refresh_without_a_file():
    app = MagicMock()
    with patch.object(tool_handlers, "_get_app", return_value=app):
        tool_handlers._refresh_imported_file_thumbnail("", "/media/x.mp4")
    app.window.FileUpdated.emit.assert_not_called()
