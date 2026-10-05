"""Real libopenshot 1.0 + Qt: an imported Premiere sequence renders where Premiere drew it.

Media is generated with ffmpeg, probed by the real ``linked_media.probe_media``,
planned by the importer, then rendered by a libopenshot Timeline built from the
planned clips. Skipped without libopenshot / real PyQt5 / ffmpeg:

    QT_QPA_PLATFORM=offscreen ZENVI_REAL_QT=1 PYTHONPATH=$HOME/zenvi-deps-1.0/python \\
        .venv/bin/python -m pytest tests/test_premiere_libopenshot.py -q
"""

import copy
import json
import os
import shutil
import subprocess
from fractions import Fraction

import pytest

pytest.importorskip("PyQt5.QtSvg")
openshot = pytest.importorskip("openshot")
if shutil.which("ffmpeg") is None:
    pytest.skip("ffmpeg not on PATH", allow_module_level=True)

from PyQt5.QtGui import QColor, QImage  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

XML = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE xmeml>
<xmeml version="4"><sequence id="sequence-1"><uuid>t</uuid><duration>50</duration>
<rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate><name>Real</name>
<media><video><format><samplecharacteristics><rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate>
<width>1920</width><height>1080</height></samplecharacteristics></format>
<track>
 <clipitem id="clipitem-1"><name>Base</name><enabled>TRUE</enabled><duration>100</duration>
  <rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate><start>0</start><end>50</end><in>0</in><out>50</out>
  <file id="file-1"><name>base.mp4</name><pathurl>{base}</pathurl><rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate>
   <duration>100</duration><media><video><samplecharacteristics><width>1920</width><height>1080</height>
   </samplecharacteristics></video><audio><channelcount>2</channelcount></audio></media></file>
  <link><linkclipref>clipitem-1</linkclipref><mediatype>video</mediatype></link>
  <link><linkclipref>clipitem-3</linkclipref><mediatype>audio</mediatype></link>
 </clipitem>
 <enabled>TRUE</enabled><locked>FALSE</locked></track>
<track>
 <clipitem id="clipitem-2"><name>Logo</name><enabled>TRUE</enabled><duration>1080000</duration>
  <rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate><start>0</start><end>50</end><in>90000</in><out>90050</out>
  <file id="file-2"><name>logo.png</name><pathurl>{logo}</pathurl><rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate>
   <duration>1080000</duration><media><video><samplecharacteristics><width>800</width><height>400</height>
   </samplecharacteristics></video></media></file>
  <filter><effect><name>Basic Motion</name><effectid>basic</effectid><effectcategory>motion</effectcategory>
   <effecttype>motion</effecttype><mediatype>video</mediatype>
   <parameter authoringApp="PremierePro"><parameterid>scale</parameterid><name>Scale</name><value>50</value></parameter>
   <parameter authoringApp="PremierePro"><parameterid>center</parameterid><name>Center</name>
    <value><horiz>0.6</horiz><vert>-0.7</vert></value></parameter>
  </effect></filter>
 </clipitem>
 <enabled>TRUE</enabled><locked>FALSE</locked></track></video>
<audio><track currentExplodedTrackIndex="0" totalExplodedTrackCount="1">
 <clipitem id="clipitem-3"><name>Base</name><enabled>TRUE</enabled><duration>100</duration>
  <rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate><start>0</start><end>50</end><in>0</in><out>50</out>
  <file id="file-1"/><sourcetrack><mediatype>audio</mediatype><trackindex>1</trackindex></sourcetrack>
  <link><linkclipref>clipitem-1</linkclipref><mediatype>video</mediatype></link>
 </clipitem><enabled>TRUE</enabled><locked>FALSE</locked></track></audio>
</media></sequence></xmeml>
"""


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    yield QApplication.instance() or QApplication([])


def _media(folder):
    base = folder / "base.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x2040ff:size=1920x1080:rate=25",
                    "-f", "lavfi", "-i", "sine=frequency=440", "-t", "4", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(base)], check=True)
    logo = folder / "logo.png"
    image = QImage(800, 400, QImage.Format_ARGB32)
    image.fill(QColor(255, 0, 0))
    assert image.save(str(logo), "PNG")
    return str(base), str(logo)


def _render(planned, readers, frame_number, out_png):
    timeline = openshot.Timeline(1920, 1080, openshot.Fraction(25, 1), 48000, 2, openshot.LAYOUT_STEREO)
    keep = []
    for number, c in enumerate(planned, start=1):
        clip = openshot.Clip(c.path)
        data = json.loads(clip.Json())
        data.update({"position": c.position, "start": c.start, "end": c.end, "layer": number,
                     "reader": copy.deepcopy(readers[c.path])})
        data.update(copy.deepcopy(c.props))
        clip.SetJson(json.dumps(data))
        timeline.AddClip(clip)
        keep.append(clip)
    timeline.Open()
    timeline.GetFrame(frame_number).Save(str(out_png), 1.0)
    timeline.Close()
    return QImage(str(out_png))


def test_imported_motion_renders_where_premiere_put_it(qapp, tmp_path):
    from classes.importers import final_cut_pro as imp
    base, logo = _media(tmp_path)
    xml = tmp_path / "Real.xml"
    xml.write_text(XML.format(base="file://localhost" + base.replace(" ", "%20"),
                              logo="file://localhost" + logo.replace(" ", "%20")))
    info = imp.ProjectInfo(fps=Fraction(25), width=1920, height=1080)
    plan = imp.plan_import(str(xml), info=info)               # the real probe_media
    assert sorted(c.title for c in plan.clips) == ["Base", "Logo"] and plan.missing == []
    base_clip = [c for c in plan.clips if c.title == "Base"][0]
    assert "has_audio" not in base_clip.props                   # merged with its linked audio
    readers = {path: reader for path, reader in plan.media.items()}
    image = _render(sorted(plan.clips, key=lambda c: c.title), readers, 10, tmp_path / "frame.png")
    assert (image.width(), image.height()) == (1920, 1080)

    def rgb(x, y):
        c = image.pixelColor(x, y)
        return c.red(), c.green(), c.blue()

    # Premiere: anchor (the logo's centre) at 960 + 0.6 x 800 = 1440, 540 - 0.7 x 400 = 260; 400 x 200 px
    for x, y in ((1440, 260), (1250, 170), (1630, 350)):
        r, g, b = rgb(x, y)
        assert r > 200 and g < 60 and b < 60, ((x, y), (r, g, b))
    for x, y in ((1440, 380), (1220, 260), (1660, 260), (960, 540)):
        r, g, b = rgb(x, y)
        assert b > 150 and r < 120, ((x, y), (r, g, b))         # the blue base video shows around it


def test_real_media_round_trips_back_to_premieres_numbers(qapp, tmp_path):
    from classes.exporters import final_cut_pro as fcp
    from classes.handoff.timeline_view import TimelineSnapshot
    from classes.importers import final_cut_pro as imp
    base, logo = _media(tmp_path)
    xml = tmp_path / "Real.xml"
    xml.write_text(XML.format(base="file://localhost" + base, logo="file://localhost" + logo))
    plan = imp.plan_import(str(xml), info=imp.ProjectInfo(fps=Fraction(25), width=1920, height=1080))
    files = [dict(reader, id="F%d" % i) for i, reader in enumerate(plan.media.values(), start=1)]
    ids = {f["path"]: f["id"] for f in files}
    clips = [dict(c.props, id=c.title, title=c.title, file_id=ids[c.path], layer=n * 1000000, position=c.position,
                  start=c.start, end=c.end) for n, c in enumerate(sorted(plan.clips, key=lambda c: c.title), 1)]
    project = {"fps": {"num": 25, "den": 1}, "width": 1920, "height": 1080, "files": files, "clips": clips,
               "layers": [{"id": "L%d" % n, "number": n * 1000000, "label": "", "lock": False} for n in (1, 2)]}
    out = fcp.export_timeline(TimelineSnapshot.from_project(project), str(tmp_path / "Back.xml"))
    root = fcp.ET.fromstring(open(out.path, encoding="utf-8").read().split("<!DOCTYPE xmeml>")[1])
    logo_item = [c for c in root.iter("clipitem") if c.findtext("name") == "Logo"][0]
    motion = {p.findtext("parameterid"): p for e in logo_item.iter("effect") if e.findtext("effectid") == "basic"
              for p in e.findall("parameter")}
    assert motion["scale"].findtext("value") == "50"
    assert (motion["center"].findtext("value/horiz"), motion["center"].findtext("value/vert")) == ("0.6", "-0.7")
    assert fcp.validate_xmeml(root) == []
