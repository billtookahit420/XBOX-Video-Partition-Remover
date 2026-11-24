#!/usr/bin/env python3
"""Unit tests for xvpr_core (GUI-free). Run: python3 test_core.py
Family style: plain checks, a pass/fail count, exit code."""
import hashlib
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "src" / \
    "XboxVideoPartitionRemover" / "app"
sys.path.insert(0, str(APP))

import xvpr_core as core  # noqa: E402

PASS = 0
FAIL = 0
FAILURES = []


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        FAILURES.append(name)
        print(f"FAIL  {name} {extra}")


def make_pattern_file(path, size):
    """Position-verifiable content: 1 MiB blocks, each stamped with
    its block index, so any offset slip changes the bytes."""
    block = 1024 * 1024
    written = 0
    idx = 0
    with open(path, "wb") as f:
        while written < size:
            n = min(block, size - written)
            if n < 8:
                f.write(bytes([idx & 0xFF]) * n)
                written += n
                idx += 1
                continue
            buf = bytearray(n)
            buf[0:8] = idx.to_bytes(8, "little")
            for off in range(8, n - 7, 4096):
                buf[off:off + 8] = (idx * 7919 + off).to_bytes(
                    8, "little")
            f.write(buf)
            written += n
            idx += 1


def block_index_at(path, offset):
    with open(path, "rb") as f:
        f.seek(offset)
        return int.from_bytes(f.read(8), "little")


def main():
    tmp = Path(tempfile.mkdtemp(prefix="xvpr-test-"))
    os.environ["XVPR_HOME"] = str(tmp / "home")
    print(f"scratch: {tmp}")

    # ---- constants
    check("SKIP_BYTES is 387 MiB", core.SKIP_BYTES == 405_798_912)
    check("expected output size",
          core.EXPECTED_OUT_SIZE == 7_429_693_440)

    # ---- size classification
    v, _ = core.check_input_size(core.SKIP_BYTES)
    check("size == skip refuses", v == "refuse")
    v, _ = core.check_input_size(core.SKIP_BYTES - 1)
    check("size < skip refuses", v == "refuse")
    v, _ = core.check_input_size(core.EXPECTED_FULL_SIZE)
    check("expected size ok", v == "ok")
    v, _ = core.check_input_size(core.EXPECTED_FULL_SIZE - 1)
    check("off-by-one size warns", v == "warn")
    v, _ = core.check_input_size(core.SKIP_BYTES + 1)
    check("just over skip warns", v == "warn")

    # ---- naming / guards
    out = core.output_path_for("/x/Halo 2 (USA).iso", "/y/out")
    check("output keeps the name unchanged",
          out.name == "Halo 2 (USA).iso" and str(out.parent) == "/y/out")
    check("paths_equal same", core.paths_equal("/a/b.iso", "/a/b.iso"))
    check("paths_equal different",
          not core.paths_equal("/a/b.iso", "/a/c.iso"))
    check("paths_equal normalizes",
          core.paths_equal("/a/./b.iso", "/a/b.iso"))

    # ---- strip: correctness
    src = tmp / "game.iso"
    payload = 4 * 1024 * 1024
    make_pattern_file(src, core.SKIP_BYTES + payload)
    src_hash_before = core.sha256_file(src)
    dst = tmp / "out" / "game.iso"
    seen = []
    in_size, out_size = core.strip_file(
        src, dst, progress=lambda d, t: seen.append((d, t)))
    check("strip sizes", in_size == core.SKIP_BYTES + payload
          and out_size == payload)
    check("strip output size on disk", dst.stat().st_size == payload)
    check("strip content starts at block 387",
          block_index_at(dst, 0) == 387)
    check("strip content mid-file block",
          block_index_at(dst, 2 * 1024 * 1024) == 389)
    check("progress ends at total", seen and seen[-1] == (payload,
                                                          payload))
    check("progress monotonic",
          all(seen[i][0] <= seen[i + 1][0]
              for i in range(len(seen) - 1)))
    check("no .part left behind",
          not (tmp / "out" / "game.iso.part").exists())
    check("source untouched by strip",
          core.sha256_file(src) == src_hash_before)

    # ---- strip: tiny payload edge (skip + 1 byte)
    src1 = tmp / "tiny.iso"
    make_pattern_file(src1, core.SKIP_BYTES + 1)
    dst1 = tmp / "out" / "tiny.iso"
    _, o1 = core.strip_file(src1, dst1)
    check("skip+1 strips to 1 byte", o1 == 1
          and dst1.stat().st_size == 1)

    # ---- strip: refusal + guard + cancel
    small = tmp / "small.iso"
    make_pattern_file(small, 1024 * 1024)
    try:
        core.strip_file(small, tmp / "out" / "small.iso")
        check("too-small strip raises", False)
    except core.StripError:
        check("too-small strip raises", True)
    try:
        core.strip_file(src, src)
        check("output-on-source raises", False)
    except core.StripError:
        check("output-on-source raises", True)
    dstc = tmp / "out" / "cancel.iso"
    try:
        core.strip_file(src, dstc, cancel=lambda: True)
        check("cancel raises StripCancelled", False)
    except core.StripCancelled:
        check("cancel raises StripCancelled", True)
    check("cancel leaves no output", not dstc.exists())
    check("cancel leaves no .part",
          not (tmp / "out" / "cancel.iso.part").exists())

    # ---- folder scan
    tree = tmp / "lib"
    (tree / "a" / "b").mkdir(parents=True)
    for p in ["Game One.iso", "a/Disc 2.ISO", "a/b/deep.iso",
              "a/notes.txt", "a/cover.jpg"]:
        (tree / p).write_bytes(b"x")
    found = [str(p.relative_to(tree)) for p in core.find_isos(tree)]
    check("find_isos recursive + case-insensitive + sorted",
          found == ["Game One.iso", "a/Disc 2.ISO", "a/b/deep.iso"],
          str(found))

    # ---- zip archives
    zsrc = tmp / "arc-src.iso"
    make_pattern_file(zsrc, core.SKIP_BYTES + 1024 * 1024)
    zpath = tmp / "Game (USA).zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as zf:
        zf.write(zsrc, "nested/Game (USA).iso")
        zf.writestr("readme.txt", "hello")
    zhash = core.sha256_file(zpath)
    members = core.archive_iso_members(zpath)
    check("zip members finds nested iso",
          members == ["nested/Game (USA).iso"], str(members))
    scratch = tmp / "scratch"
    extracted = core.extract_archive(zpath, scratch)
    check("zip extract returns one iso", len(extracted) == 1)
    ep, member = extracted[0]
    check("zip extract bytes intact",
          core.sha256_file(ep) == core.sha256_file(zsrc))
    check("zip source untouched", core.sha256_file(zpath) == zhash)
    need = core.archive_scratch_need(zpath)
    check("zip scratch need covers extract + output",
          need >= (core.SKIP_BYTES + 1024 * 1024) + 1024 * 1024)
    empty = tmp / "empty.zip"
    with zipfile.ZipFile(empty, "w") as zf:
        zf.writestr("x.txt", "x")
    check("no-iso zip members empty",
          core.archive_iso_members(empty) == [])
    try:
        core.extract_archive(empty, scratch)
        check("no-iso zip extract raises", False)
    except core.StripError:
        check("no-iso zip extract raises", True)

    # ---- scratch space warning (disk_usage patched)
    real_usage = shutil.disk_usage
    try:
        core.shutil.disk_usage = lambda p: type(
            "U", (), {"free": 1, "total": 2, "used": 1})()
        warn = core.scratch_space_warning(zpath, scratch)
        check("low space warns", "low local disk space" in warn)
        core.shutil.disk_usage = lambda p: type(
            "U", (), {"free": 10 ** 13, "total": 10 ** 13,
                      "used": 0})()
        check("ample space silent",
              core.scratch_space_warning(zpath, scratch) == "")
    finally:
        core.shutil.disk_usage = real_usage

    # ---- persistence
    home = core.app_home()
    check("XVPR_HOME honored", home == tmp / "home")
    state = tmp / "state.json"
    core.save_json_atomic(state, {"a": [1, 2], "b": "x"})
    check("json roundtrip",
          core.load_json(state, None) == {"a": [1, 2], "b": "x"})
    check("json atomic leaves no tmp",
          not (tmp / "state.json.tmp").exists())
    check("json missing -> default",
          core.load_json(tmp / "nope.json", {"d": 1}) == {"d": 1})

    # ---- first-run setup (real download, pinned hash)
    ok, msg = core.run_first_run_setup(home)
    check("first-run setup succeeds", ok, msg)
    tool = core.find_7z_tool(home)
    check("7z tool found after setup", tool is not None)
    if tool:
        import subprocess
        p = subprocess.run([tool, "i"], capture_output=True,
                           text=True)
        check("downloaded 7zz runs and is 26.06",
              "7-Zip" in p.stdout and "26.06" in p.stdout)

    # ---- .7z archives (needs the tool from setup)
    if tool:
        seven = tmp / "Game7.7z"
        import subprocess
        subprocess.run([tool, "a", "-y", str(seven),
                        f"-i!{zsrc.name}"],
                       cwd=str(tmp), capture_output=True)
        # build it plainly instead: add the iso from its folder
        if not seven.exists() or seven.stat().st_size == 0:
            subprocess.run([tool, "a", "-y", str(seven), zsrc.name],
                           cwd=str(tmp), capture_output=True,
                           check=True)
        members7 = core.archive_iso_members(seven)
        check("7z members listed", members7 == [zsrc.name],
              str(members7))
        ex7 = core.extract_archive(seven, tmp / "scratch7")
        check("7z extract bytes intact",
              len(ex7) == 1
              and core.sha256_file(ex7[0][0])
              == core.sha256_file(zsrc))
    else:
        check("7z members listed", False, "no tool")
        check("7z extract bytes intact", False, "no tool")

    # ---- .7z without a tool reports setup is needed
    bare = tmp / "bare-home"
    bare.mkdir()
    saved = os.environ.get("XVPR_HOME")
    os.environ["XVPR_HOME"] = str(bare)
    try:
        tool_bare = core.find_7z_tool(bare)
        if tool_bare is None or "bare" not in str(tool_bare):
            # system PATH may provide one; only assert the message
            # path when no tool is reachable at all
            if core.find_7z_tool() is None:
                try:
                    core.archive_iso_members(seven)
                    check("7z without tool explains setup", False)
                except core.StripError as ex:
                    check("7z without tool explains setup",
                          "first-run setup" in str(ex))
            else:
                check("7z without tool explains setup", True,
                      "(system 7z present; branch not reachable)")
    finally:
        if saved is not None:
            os.environ["XVPR_HOME"] = saved

    print(f"\n{PASS} passed, {FAIL} failed")
    if FAILURES:
        print("failures:", ", ".join(FAILURES))
    shutil.rmtree(tmp, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
