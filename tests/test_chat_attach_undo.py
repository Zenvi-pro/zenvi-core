"""Chat vision encode helpers and nested undo."""

import base64
from unittest.mock import MagicMock

from classes.chat_attachments import (
    attach_paths_batch,
    encode_chat_images,
    snapshot_attachments,
)
from classes.updates import nested_transaction
from windows.chat_web_view import (
    attach_chat_media_urls,
    chat_owns_clipboard_keys,
    local_paths_from_urls,
)


def test_nested_transaction_joins_outer_tid():
    class U:
        transaction_id = "outer"

    u = U()
    with nested_transaction(u) as tid:
        assert tid == "outer"
        with nested_transaction(u) as inner:
            assert inner == "outer"
    assert u.transaction_id == "outer"


def test_nested_transaction_mints_and_clears():
    class U:
        transaction_id = None

    u = U()
    with nested_transaction(u) as tid:
        assert tid
        assert u.transaction_id == tid
    assert u.transaction_id is None


def test_nested_transaction_restores_after_side_effect_clear():
    class U:
        transaction_id = None

    u = U()
    with nested_transaction(u) as tid:
        u.transaction_id = None  # simulate processEvents wiping the tid
        assert tid
    assert u.transaction_id is None


def test_attach_paths_batch_undos_as_one(tmp_path):
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    a.write_bytes(b"a")
    b.write_bytes(b"b")
    atts = []
    stack = []

    before, added = attach_paths_batch(atts, [str(a), str(b)])
    assert added == 2
    stack.append(before)
    atts[:] = snapshot_attachments(stack.pop())
    assert atts == []


def test_attach_chat_media_urls_helper(tmp_path):
    clip = tmp_path / "shot.png"
    clip.write_bytes(b"png")
    url = MagicMock()
    url.isLocalFile.return_value = True
    url.toLocalFile.return_value = str(clip)

    assert local_paths_from_urls([url]) == [str(clip)]

    class Chat:
        def __init__(self):
            self.atts = []

        def attach_paths(self, paths, insert_mention=False):
            _, added = attach_paths_batch(self.atts, paths)
            return added

    chat = Chat()
    assert attach_chat_media_urls(chat, [url]) is True
    assert len(chat.atts) == 1


def test_chat_owns_keys_when_view_has_focus():
    class View:
        def hasFocus(self):
            return True

        def focusProxy(self):
            return None

        def underMouse(self):
            return False

    class Chat:
        def isVisible(self):
            return True

        _chat_view = View()

    assert chat_owns_clipboard_keys(Chat(), focus_widget=None, under_mouse=False) is True


def test_encode_chat_images_jpeg_from_png(tmp_path):
    try:
        from PIL import Image
    except ImportError:
        import pytest
        pytest.skip("Pillow required for encode test")

    path = tmp_path / "shot.png"
    Image.new("RGB", (40, 30), color=(20, 40, 60)).save(path)

    atts = [{"kind": "image", "path": str(path), "name": "shot.png", "file_id": "f1"}]
    images = encode_chat_images(atts)
    assert len(images) == 1
    assert images[0]["mime_type"] == "image/jpeg"
    assert images[0]["name"] == "shot.png"
    assert images[0]["file_id"] == "f1"
    raw = base64.b64decode(images[0]["image_base64"])
    assert raw[:2] == b"\xff\xd8"  # JPEG SOI


def test_encode_chat_images_skips_non_images(tmp_path):
    clip = tmp_path / "a.mp4"
    clip.write_bytes(b"x")
    images = encode_chat_images([{"kind": "video", "path": str(clip), "name": "a.mp4"}])
    assert images == []


def test_encode_chat_images_caps_at_four(tmp_path):
    try:
        from PIL import Image
    except ImportError:
        import pytest
        pytest.skip("Pillow required for encode test")

    atts = []
    for i in range(6):
        p = tmp_path / f"{i}.png"
        Image.new("RGB", (8, 8), color=(i, i, i)).save(p)
        atts.append({"kind": "image", "path": str(p), "name": p.name, "file_id": ""})
    assert len(encode_chat_images(atts)) == 4


def test_undo_ignores_reentrant_calls():
    from classes.updates import UpdateManager

    um = UpdateManager()
    um._undo_redo_busy = True
    um.actionHistory.append(
        type(
            "A",
            (),
            {
                "transaction": "t1",
                "type": "update",
                "values": {},
                "key": ["x"],
                "copy": lambda self: self,
            },
        )()
    )
    um.undo()
    assert len(um.actionHistory) == 1
