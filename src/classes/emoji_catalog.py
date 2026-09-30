"""The emoji catalog behind the Emojis dock and the emoji tools (no Qt).

1,239 OpenMoji SVGs (``src/emojis/color/svg``) described by
``src/emojis/data/openmoji-optimized.json``, plus any SVGs the user put in
``info.EMOJIS_PATH``. ``entries()`` lists them the way the dock's model does
(name = the capitalised annotation, group = the OpenMoji group);
``add_emoji_file`` is the dock's "add this emoji to Project Files".
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from typing import List, Optional, Tuple

from classes import info

_LOCK = threading.Lock()
_METADATA = None

# Everyday words for emoji whose OpenMoji annotation differs (checked when they exist).
ALIASES = {
    "heart": "red heart", "love": "red heart", "lol": "face with tears of joy",
    "laugh": "face with tears of joy", "laughing": "face with tears of joy", "joy": "face with tears of joy",
    "lit": "fire", "flame": "fire", "100": "hundred points", "party": "party popper", "tada": "party popper",
    "celebrate": "party popper", "cry": "loudly crying face", "sad": "crying face", "smile": "grinning face",
    "happy": "grinning face", "wow": "astonished face", "shocked": "face screaming in fear",
    "money": "money bag", "check": "check mark button", "tick": "check mark", "angry": "enraged face",
    "cool": "smiling face with sunglasses", "kiss": "face blowing a kiss", "thinking": "thinking face",
    "think": "thinking face", "poop": "pile of poo", "star": "star", "sparkle": "sparkles",
    "shine": "sparkles", "rocket": "rocket", "sun": "sun", "moon": "crescent moon", "rain": "cloud with rain",
    "snow": "snowflake", "coffee": "hot beverage", "pizza": "pizza", "cake": "birthday cake",
    "birthday": "birthday cake", "gift": "wrapped gift", "music": "musical notes", "camera": "camera",
    "plane": "airplane", "car": "automobile", "home": "house", "world": "globe showing Europe-Africa",
    "earth": "globe showing Europe-Africa", "warning": "warning", "question": "red question mark",
    "exclamation": "red exclamation mark", "arrow": "right arrow", "mind blown": "exploding head",
    "skull": "skull", "dead": "skull", "crown": "crown", "king": "crown", "trophy": "trophy",
    "win": "trophy", "gold": "1st place medal", "idea": "light bulb", "bulb": "light bulb",
    "hot": "hot face", "cold": "cold face", "sleep": "sleeping face", "tired": "tired face",
    "devil": "smiling face with horns", "angel": "smiling face with halo", "ghost": "ghost", "alien": "alien",
    "robot": "robot", "dog": "dog face", "cat": "cat face", "unicorn": "unicorn", "boom": "collision",
    "explosion": "collision", "lightning": "high voltage", "zap": "high voltage", "bomb": "bomb",
    "starstruck": "star-struck", "party face": "partying face", "glow": "glowing star",
}


@dataclass
class EmojiEntry:
    code: str          # file base name: OpenMoji hexcode ("1F525") or a user file name
    path: str
    name: str          # capitalised annotation, as the dock shows it (untranslated)
    group: str         # OpenMoji group id ("smileys-emotion"), "user" for user files
    group_name: str    # dock label ("Smileys")
    char: str = ""
    tags: str = ""
    skintone: str = ""
    source: str = "bundled"

    def as_dict(self) -> dict:
        return {"code": self.code, "emoji": self.char, "name": self.name, "group": self.group,
                "source": self.source}


def emojis_dir() -> str:
    return os.path.join(info.PATH, "emojis", "color", "svg")


def metadata_path() -> str:
    return os.path.join(info.PATH, "emojis", "data", "openmoji-optimized.json")


def load_metadata() -> dict:
    global _METADATA
    with _LOCK:
        if _METADATA is None:
            with open(metadata_path(), "r", encoding="utf-8") as fh:
                _METADATA = json.load(fh)
        return _METADATA


def emoji_folders() -> List[Tuple[str, str]]:
    """[(folder, source)]: the bundled OpenMoji folder, then the user's emoji folder when it has files."""
    folders = [(emojis_dir(), "bundled")]
    user = getattr(info, "EMOJIS_PATH", "")
    if user and os.path.isdir(user) and os.listdir(user):
        folders.append((user, "user"))
    return folders


def entry_for(path: str, source: str = "bundled", metadata: Optional[dict] = None) -> EmojiEntry:
    metadata = metadata if metadata is not None else load_metadata()
    code = os.path.splitext(os.path.basename(path))[0]
    emoji = metadata.get(code, {})
    group = emoji.get("group", "user")
    return EmojiEntry(
        code=code, path=path,
        name=str(emoji.get("annotation", code)).capitalize(),
        group=group, group_name=group.split("-")[0].capitalize(),
        char=str(emoji.get("emoji", "")),
        tags=" ".join(str(emoji.get(k, "")) for k in ("tags", "openmoji_tags", "subgroups")),
        skintone=str(emoji.get("skintone", "")),
        source=source,
    )


def entries() -> List[EmojiEntry]:
    """Every emoji the dock lists, in the dock's order (per folder, sorted by file name)."""
    metadata = load_metadata()
    out = []
    for folder, source in emoji_folders():
        for filename in sorted(os.listdir(folder)):
            if filename.startswith(".") or "thumbs.db" in filename.lower():
                continue
            out.append(entry_for(os.path.join(folder, filename), source, metadata))
    return out


def groups() -> List[Tuple[str, str]]:
    """[(label, group id)] in first-seen order (the dock's group dropdown)."""
    seen = []
    for e in entries():
        pair = (e.group_name, e.group)
        if pair not in seen:
            seen.append(pair)
    return seen


def _strip_vs(text: str) -> str:
    return text.replace("️", "").replace("︎", "")


def _words(text: str) -> List[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _group_ok(entry: EmojiEntry, group: str) -> bool:
    if not group:
        return True
    g = group.strip().lower().replace(" ", "-").replace("&", "")
    return entry.group.startswith(g) or entry.group_name.lower().startswith(g) or g in entry.group


def score(entry: EmojiEntry, query: str) -> float:
    q = str(query or "").strip()
    if not q:
        return 1.0
    ql = q.lower()
    if entry.char and _strip_vs(entry.char) == _strip_vs(q):
        return 100.0
    code = ql.replace("u+", "").replace(" ", "-")
    if entry.code.lower() == code or entry.code.lower().replace("-fe0f", "") == code:
        return 100.0
    name = entry.name.lower()
    alias = ALIASES.get(ql)
    penalty = 5.0 if entry.skintone else 0.0
    if alias and name == alias.lower():
        return 95.0
    if name == ql:
        return 90.0 - penalty
    qw = _words(ql)
    nw = _words(name)
    if qw and all(w in nw for w in qw):
        return 70.0 - penalty - len(nw) * 0.5 - len(name) / 100.0
    if name.startswith(ql):
        return 60.0 - penalty - len(name) / 100.0
    tw = _words(entry.tags)
    if qw and all(w in tw or w in nw for w in qw):
        return 40.0 - penalty - len(nw) * 0.5
    if ql in name or ql in entry.tags.lower():
        return 20.0 - penalty
    return 0.0


def search(query: str, group: str = "", limit: int = 10) -> List[Tuple[float, EmojiEntry]]:
    """Best matches for a name ("fire"), an emoji character ("🔥") or a hexcode ("1F525")."""
    ranked = []
    for entry in entries():
        if not _group_ok(entry, group):
            continue
        s = score(entry, query)
        if s > 0:
            ranked.append((s, entry))
    ranked.sort(key=lambda pair: (-pair[0], pair[1].name))
    return ranked[: max(1, int(limit))]


def add_emoji_file(filepath: str, emoji_name: Optional[str] = None):
    """The Emojis dock's add_file: the project File for an emoji SVG (created once). GUI thread.

    Raises on a file libopenshot cannot open (the dock logs and ignores it).
    """
    import openshot
    from classes.query import File

    existing = File.get(path=filepath)
    if existing:
        return existing
    clip = openshot.Clip(filepath)
    reader = clip.Reader()
    file_data = json.loads(reader.Json())
    file_data["media_type"] = "image"
    if emoji_name:
        file_data["name"] = emoji_name
    new_file = File()
    new_file.data = file_data
    new_file.save()
    return new_file
