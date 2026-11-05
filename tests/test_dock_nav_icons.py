import os
import xml.etree.ElementTree as ET

from themes.cosmic.theme import DOCK_NAV_ICONS

IMAGES_DIR = os.path.join(os.path.dirname(__file__), "..", "src", "themes", "cosmic", "images")

EXPECTED_DOCK_NAMES = {
    "dockColorGradeWheels",
    "dockIndex",
    "dockAudioRecording",
    "dockLumaWaveform",
    "dockHistogram",
    "dockVectorscope",
    "dockAudio",
}


def test_expected_docks_have_nav_icons():
    listed = {object_name for object_name, _ in DOCK_NAV_ICONS}
    assert EXPECTED_DOCK_NAMES <= listed, sorted(EXPECTED_DOCK_NAMES - listed)


def test_nav_icon_files_exist_and_are_svg():
    for _, filename in DOCK_NAV_ICONS:
        path = os.path.join(IMAGES_DIR, filename)
        assert os.path.isfile(path), filename
        assert ET.parse(path).getroot().tag.split("}")[-1] == "svg", filename
