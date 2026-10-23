"""Every build bundles the same libopenshot, and the app requires at least that one.

libopenshot 1.0.0 cannot open a microphone whose name is not ASCII (the common
"Microphone Array (Intel® Smart Sound ...)": "No such device"); 1.0.1 can.
"""

import os
import re

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PINNED = "v1.0.1"
SO_VERSION = "32"          # libopenshot's PROJECT_SO_VERSION at v1.0.1


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def test_every_build_script_defaults_to_the_pinned_tag():
    for script in ("installer/ci-win-msys-libopenshot.sh", "installer/ci-linux-libopenshot.sh",
                   "scripts/build-mac-libopenshot.sh"):
        defaults = re.findall(r'(?:LIBOPENSHOT_TAG|TAG)="\$\{LIBOPENSHOT_TAG:-(v[\d.]+)\}"', _read(script))
        assert defaults == [PINNED], script


def test_the_macos_release_job_builds_the_pinned_tags_and_keys_its_cache_on_them():
    release = _read(".github", "workflows", "release.yml")
    assert re.findall(r"LIBOPENSHOT(?:_AUDIO)?_TAG=(v[\d.]+)", release) == [PINNED, PINNED]
    keys = re.findall(r"key: .*libopenshot-(v[\d.]+)", release)
    assert keys and set(keys) == {PINNED}


def test_the_app_requires_the_version_it_bundles():
    minimum = re.search(r'^MINIMUM_LIBOPENSHOT_VERSION = "([^"]+)"', _read("src", "classes", "info.py"), re.M)
    assert "v" + minimum.group(1) == PINNED


def test_no_patch_is_left_for_an_older_1_0_tag():
    patches = os.listdir(os.path.join(ROOT, "installer", "mac-patches"))
    stale = [p for p in patches if re.match(r"libopenshot(-audio)?-v1\.0\.(\d+)-", p)
             and not p.startswith(("libopenshot-%s-" % PINNED, "libopenshot-audio-%s-" % PINNED))]
    assert stale == []
    assert "libopenshot-%s-discard-preroll.patch" % PINNED in patches


def test_run_win_builds_the_pinned_tag_even_over_an_older_checkout():
    run_win = _read("run-win.ps1")
    for repo in ("libopenshot-audio", "libopenshot"):
        assert "git -C %s checkout -q -f %s" % (repo, PINNED) in run_win, repo


def test_the_local_mac_freeze_falls_back_to_the_pinned_soname():
    assert '("libopenshot.%s.dylib", "libopenshot")' % SO_VERSION in _read("installer", "fix_macos_dylib_paths.py")
