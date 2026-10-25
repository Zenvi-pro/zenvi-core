"""The release refuses a Mac app that can't start on the CPU its DMG is named for."""

import importlib.util
import struct
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "verify_mac_bundle", Path(__file__).resolve().parents[1] / "installer" / "verify_mac_bundle.py")
vmb = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(vmb)

X86, ARM = vmb.CPU_TYPES["x86_64"], vmb.CPU_TYPES["arm64"]


def thin(cpu):
    return struct.pack("<II", 0xFEEDFACF, cpu) + b"\0" * 24


def fat(*cpus):
    head = struct.pack(">II", 0xCAFEBABE, len(cpus))
    return head + b"".join(struct.pack(">iiIII", cpu, 0, 0, 0, 0) for cpu in cpus)


def app(tmp_path, binaries, openshot=True):
    root = tmp_path / "Zenvi.app"
    macos = root / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    for name, data in binaries.items():
        (macos / name).write_bytes(data)
    if openshot:
        lib = root / "Contents" / "Resources" / "lib"
        lib.mkdir(parents=True)
        cpu = next(iter(binaries.values()))
        (lib / "libopenshot.31.dylib").write_bytes(cpu)
        (lib / "_openshot.cpython-311-darwin.so").write_bytes(cpu)
    (macos / "readme.txt").write_text("not code")
    return root


@pytest.mark.parametrize("data, archs", [
    (thin(X86), [X86]),
    (thin(ARM), [ARM]),
    (fat(X86, ARM), [X86, ARM]),
    (b"\xca\xfe\xba\xbe\x00\x00\x00\x37 java class", None),
    (b"#!/bin/sh\n", None),
])
def test_mach_o_architectures_are_read_from_the_header(tmp_path, data, archs):
    path = tmp_path / "bin"
    path.write_bytes(data)
    assert vmb.macho_archs(str(path)) == archs


def test_an_arm64_only_app_is_refused_as_x86_64(tmp_path):
    root = app(tmp_path, {"zenvi": thin(ARM), "QtCore": thin(ARM), "launch-mac": fat(X86, ARM)})
    problems, stats = vmb.check(str(root), "x86_64")
    assert stats["wrong_arch"] == 4  # QtCore, zenvi and the two openshot files
    assert any("lack x86_64" in p for p in problems)
    assert vmb.main(["--arch", "x86_64", str(root)]) == 1


def test_an_app_without_libopenshot_is_refused(tmp_path):
    root = app(tmp_path, {"zenvi": thin(X86)}, openshot=False)
    problems, _ = vmb.check(str(root), "x86_64")
    assert any("libopenshot" in p for p in problems)
    assert any("_openshot" in p for p in problems)


def test_a_complete_app_for_its_arch_passes(tmp_path):
    root = app(tmp_path, {"zenvi": thin(X86), "launch-mac": fat(X86, ARM)})
    assert vmb.check(str(root), "x86_64") == ([], {"machos": 4, "wrong_arch": 0})
    assert vmb.main(["--arch", "x86_64", str(root)]) == 0
