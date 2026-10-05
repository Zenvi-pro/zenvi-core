"""classes.handoff.remotion.detect and .sources: is it Remotion, where is its entry, where is each composition's code."""

import json
import os
import shutil

import pytest

from classes.handoff.linked_media import LinkError, SourceMissing
from classes.handoff.remotion import detect, sources
from remotion_fakes import make_project

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "handoff_remotion", "project")


# ---------------------------------------------------------------------------
# detect
# ---------------------------------------------------------------------------

def test_a_folder_inside_the_project_resolves_to_its_root(tmp_path):
    root = make_project(str(tmp_path / "promo"))
    info = detect.inspect_project(os.path.join(root, "src"))
    assert info.root == root and info.name == "fake-remotion"
    assert info.entry == "src/index.ts" and info.entry_reason == "default"
    assert info.installed and info.version == "4.0.532" and info.package_manager == "npm"
    assert not info.is_zenvi_generated and info.public_dir == "public"


def test_not_remotion_and_missing_folders_say_what_to_pick(tmp_path):
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "package.json").write_text(json.dumps({"dependencies": {"react": "19"}}))
    with pytest.raises(detect.NotRemotionProject, match="does not depend on remotion"):
        detect.inspect_project(str(tmp_path / "web"))
    (tmp_path / "empty").mkdir()
    with pytest.raises(detect.NotRemotionProject, match="no package.json"):
        detect.inspect_project(str(tmp_path / "empty"))
    with pytest.raises(SourceMissing):
        detect.inspect_project(str(tmp_path / "gone"))


def test_entry_comes_from_the_config_then_scripts_then_defaults(tmp_path):
    root = make_project(str(tmp_path / "p"), config="import {Config} from '@remotion/cli/config';\n"
                                                    "// Config.setEntryPoint('./src/commented.ts');\n"
                                                    "Config.setEntryPoint('./src/main.tsx');\n")
    (tmp_path / "p" / "src" / "main.tsx").write_text("registerRoot(X)")
    info = detect.inspect_project(root)
    assert (info.entry, info.entry_reason, info.config_file) == ("src/main.tsx", "remotion.config", "remotion.config.ts")
    # the config names a file that is gone: the package.json script wins
    root2 = make_project(str(tmp_path / "q"), config="Config.setEntryPoint('./src/gone.ts');\n",
                         scripts={"render": "remotion render --log=verbose remotion/entry.ts MyComp out.mp4"})
    os.makedirs(os.path.join(root2, "remotion"))
    open(os.path.join(root2, "remotion", "entry.ts"), "w").close()
    info2 = detect.inspect_project(root2)
    assert (info2.entry, info2.entry_reason) == ("remotion/entry.ts", "package.json script")
    assert detect.parse_script_entries({"a": "remotion studio", "b": "remotion still src/x.tsx Thumb t.png"}) == \
        ["src/x.tsx"]
    assert detect.parse_config_public_dir("Config.setPublicDir('./static');") == "./static"


def test_installed_follows_node_resolution_and_reports_what_is_missing(tmp_path):
    root = make_project(str(tmp_path / "p"), installed=False)
    info = detect.inspect_project(root)
    assert not info.installed and set(info.missing) == set(detect.REQUIRED_PACKAGES)
    assert info.version == "4.0.532"  # declared in package.json
    with pytest.raises(LinkError, match="not installed .*npm install"):
        detect.require_ready(info)
    # a workspace root above the project holds remotion; @remotion/cli nests the renderer/bundler (npm)
    ws = tmp_path / "node_modules"
    for name, version in (("remotion", "4.0.500"), ("@remotion/cli", "4.0.500")):
        (ws / name).mkdir(parents=True)
        (ws / name / "package.json").write_text(json.dumps({"name": name, "version": version}))
    for name in ("@remotion/renderer", "@remotion/bundler"):
        nested = ws / "@remotion" / "cli" / "node_modules" / name
        nested.mkdir(parents=True)
        (nested / "package.json").write_text(json.dumps({"name": name, "version": "4.0.500"}))
    info = detect.inspect_project(root)
    assert info.installed and info.installed_version == "4.0.500"
    assert detect.require_ready(info) is info


def test_pnpm_layout_and_lockfile_package_manager(tmp_path):
    root = tmp_path / "p"
    make_project(str(root), installed=False)
    (root / "pnpm-lock.yaml").write_text("lockfileVersion: 9")
    store = root / "node_modules" / ".pnpm" / "@remotion+cli@4.0.532" / "node_modules"
    for name in ("@remotion/cli", "@remotion/renderer", "@remotion/bundler"):
        (store / name).mkdir(parents=True)
        (store / name / "package.json").write_text(json.dumps({"name": name, "version": "4.0.532"}))
    (root / "node_modules" / "@remotion").mkdir(parents=True)
    os.symlink(str(store / "@remotion" / "cli"), str(root / "node_modules" / "@remotion" / "cli"))
    (root / "node_modules" / "remotion").mkdir()
    (root / "node_modules" / "remotion" / "package.json").write_text(json.dumps({"version": "4.0.532"}))
    info = detect.inspect_project(str(root))
    assert info.installed and info.package_manager == "pnpm" and info.install_command() == "pnpm install"


def test_old_remotion_and_missing_entry_are_refused(tmp_path):
    root = make_project(str(tmp_path / "p"), version="3.3.100")
    with pytest.raises(LinkError, match="Remotion 4 or newer"):
        detect.require_ready(detect.inspect_project(root))
    os.remove(os.path.join(root, "src", "index.ts"))
    with pytest.raises(detect.NotRemotionProject, match="no Remotion entry point"):
        detect.require_ready(detect.inspect_project(root))


def test_zenvi_generated_projects_are_recognised_by_the_timeline_header(tmp_path):
    root = make_project(str(tmp_path / "p"))
    os.makedirs(os.path.join(root, "src", "zenvi"))
    path = os.path.join(root, "src", "zenvi", "timeline.json")
    with open(path, "w") as fh:
        fh.write('{"something": 1}')
    assert not detect.inspect_project(root).is_zenvi_generated
    with open(path, "w") as fh:
        fh.write('{"zenvi_timeline": 1, "source_project": "trip"}')
    info = detect.inspect_project(root)
    assert info.is_zenvi_generated and info.zenvi_timeline == path and info.as_dict()["zenvi_generated"]


# ---------------------------------------------------------------------------
# sources (static scan)
# ---------------------------------------------------------------------------

def test_scan_finds_every_composition_and_its_component_definition():
    found = sources.scan_project(FIXTURE, "src/index.ts")
    assert set(found) == {"TitleCard", "Aliased", "Promo", "Outro", "Lazy", "Thumb"}

    title = found["TitleCard"]
    assert (title.file, title.line, title.folder, title.kind) == ("src/TitleCard.tsx", 5, "Graphics", "composition")
    assert title.root_file == "src/Root.tsx" and title.component == "TitleCard"

    aliased = found["Aliased"]   # import {Original as Fancy} from './comps/fancy'
    assert (aliased.file, aliased.line, aliased.folder) == ("src/comps/fancy.tsx", 5, "Graphics/Lower thirds")

    promo = found["Promo"]       # default import -> `export default function Promo`
    assert (promo.file, promo.line) == ("src/Promo.tsx", 5)

    outro = found["Outro"]       # namespace import + index re-export, id={'Outro'}
    assert (outro.file, outro.line, outro.component) == ("src/scenes/Outro.tsx", 3, "Outro")

    lazy = found["Lazy"]         # lazyComponent={() => import('./Lazy')} -> export default Lazy -> const Lazy
    assert lazy.lazy and (lazy.file, lazy.line) == ("src/Lazy.tsx", 3)

    thumb = found["Thumb"]       # <Still> with a component defined in Root.tsx itself
    assert thumb.kind == "still" and (thumb.file, thumb.line) == ("src/Root.tsx", 14)


def test_comments_dynamic_ids_and_braces_in_props_do_not_confuse_the_scan():
    found = sources.scan_project(FIXTURE)
    assert "Ghost" not in found and "Ghost2" not in found
    assert not any(k.startswith("dynamic") for k in found)
    root = open(os.path.join(FIXTURE, "src", "Root.tsx")).read()
    expected_line = root[:root.index('id="TitleCard"')].count("\n")  # the <Composition line is just above
    assert found["TitleCard"].root_line == expected_line


def test_unresolvable_components_fall_back_to_the_composition_line(tmp_path):
    shutil.copytree(FIXTURE, str(tmp_path / "p"))
    root = tmp_path / "p"
    (root / "src" / "Root.tsx").write_text(
        "import {Fancy} from '@ui/fancy';\n"
        "export const RemotionRoot = () => (\n"
        "  <Composition id=\"FromPackage\" component={Fancy} durationInFrames={1} fps={30} width={1} height={1}/>\n"
        ");\n")
    found = sources.scan_project(str(root))
    comp = found["FromPackage"]
    assert comp.file is None and (comp.best_file, comp.best_line) == ("src/Root.tsx", 3)
    assert comp.as_dict()["file"] == "src/Root.tsx"


def test_attribute_reader_handles_strings_spreads_and_nested_braces():
    text = '<Composition {...shared} id="A" defaultProps={{a: "}", b: {c: `x${1}`}}} still />rest'
    start = text.index("Composition") + len("Composition")
    attrs, end, self_closing = sources.parse_attributes(text, start)
    assert attrs["id"] == '"A"' and attrs["still"] == "true" and self_closing
    assert attrs["defaultProps"].startswith("{{") and text[end:] == "rest"
    src = "a // x\nb /* y\n z */ c 'http://k'"
    out = sources.strip_comments(src)
    assert len(out) == len(src) and out.split("\n")[2].index("c") == src.split("\n")[2].index("c")
    assert "x" not in out and "y" not in out and "z" not in out and "'http://k'" in out
