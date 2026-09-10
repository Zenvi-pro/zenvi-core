import os

from classes.chat_attachments import (
    display_user_text,
    format_referenced_files_block,
    kind_for_path,
    make_attachment,
)


def test_kind_for_path():
    assert kind_for_path("/tmp/a.mp4") == "video"
    assert kind_for_path("shot.PNG") == "image"
    assert kind_for_path("note.txt") == "text"
    assert kind_for_path("song.mp3") == "audio"
    assert kind_for_path("notes.zip") == "file"


def test_format_block_includes_path_and_file_id(tmp_path):
    clip = tmp_path / "reel.mp4"
    clip.write_bytes(b"x")
    att = make_attachment(str(clip), file_id="abc123", name="reel.mp4")
    block = format_referenced_files_block([att])
    assert "[Referenced files]" in block
    assert str(clip) in block or os.path.abspath(str(clip)) in block
    assert "media_bin_file_id=abc123" in block
    assert format_referenced_files_block([]) == ""


def test_display_user_text_adds_mentions_once():
    atts = [{"name": "reel.mp4"}, {"name": "still.png"}]
    assert display_user_text("color grade this", atts) == "color grade this @reel.mp4 @still.png"
    assert display_user_text("@reel.mp4 please", atts) == "@reel.mp4 please @still.png"
    assert display_user_text("", atts) == "@reel.mp4 @still.png"
    assert display_user_text("hello", []) == "hello"



def test_make_attachment_empty_path_stays_empty():
    att = make_attachment("", name="reel.mp4")
    assert att["path"] == ""
    assert att["name"] == "reel.mp4"
    cwd = os.getcwd()
    assert att["path"] != cwd
    assert not att["path"].startswith(cwd)


def test_format_block_omits_empty_path():
    att = make_attachment("", file_id="abc123", name="reel.mp4")
    block = format_referenced_files_block([att])
    assert "media_bin_file_id=abc123" in block
    assert "path=" not in block
    assert os.getcwd() not in block
    assert "@reel.mp4" in block
