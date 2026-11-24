#!/usr/bin/env python3
"""Proof: the app's in-process strip is byte-identical to
    dd if=src of=dst bs=1M skip=387
on a ~450 MB deterministic, position-stamped file (any offset slip
changes the hash). Also re-checks the refusal path end-to-end.
Run: python3 proof_dd_equivalence.py [workdir]"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                       / "src" / "XboxVideoPartitionRemover" / "app"))
from test_core import make_pattern_file  # noqa: E402
import xvpr_core as core  # noqa: E402


def main():
    work = Path(sys.argv[1] if len(sys.argv) > 1
                else "/tmp/xvpr-dd-proof")
    work.mkdir(parents=True, exist_ok=True)
    src = work / "redump-game.iso"
    size = core.SKIP_BYTES + 63 * 1024 * 1024 + 12345  # ~450 MB, odd tail
    if not src.exists() or src.stat().st_size != size:
        print(f"generating {size:,}-byte synthetic source...")
        make_pattern_file(src, size)
    print(f"source: {src} ({size:,} bytes)")

    app_out = work / "app-out.iso"
    dd_out = work / "dd-out.iso"
    core.strip_file(src, app_out)
    subprocess.run(["dd", f"if={src}", f"of={dd_out}", "bs=1M",
                    "skip=387", "status=none"], check=True)

    ha = core.sha256_file(app_out)
    hd = core.sha256_file(dd_out)
    print(f"app strip sha256: {ha}")
    print(f"dd        sha256: {hd}")
    assert app_out.stat().st_size == size - core.SKIP_BYTES
    assert ha == hd, "MISMATCH: app strip differs from dd"
    print("PASS: app strip is byte-identical to dd bs=1M skip=387")

    small = work / "small.iso"
    make_pattern_file(small, 10 * 1024 * 1024)
    try:
        core.strip_file(small, work / "small-out.iso")
        print("FAIL: too-small input was not refused")
        return 1
    except core.StripError as ex:
        print(f"PASS: too-small input refused ({ex})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
