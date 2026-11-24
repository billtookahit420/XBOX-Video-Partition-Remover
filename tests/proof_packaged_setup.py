#!/usr/bin/env python3
"""Proof: first-run setup inside the packaged app.

Runs the real App from a given root with a PATH that has no 7z on
it, triggers the app's own run_setup() (the method the setup
dialog calls), and waits for the hash-verified download to land.
Then builds a .7z with the downloaded 7zz and adds + processes it
through the GUI path - extraction, strip, scratch cleanup.

Run: xvfb-run -a python3 proof_packaged_setup.py <app_root> <work>
"""
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from test_core import make_pattern_file  # noqa: E402
from drive_gui import pump_until, check  # noqa: E402


def main():
    app_root = Path(sys.argv[1]).resolve()
    work = Path(sys.argv[2]).resolve()
    home = work / "home"
    outdir = work / "out"
    for d in (home, outdir):
        d.mkdir(parents=True, exist_ok=True)

    os.environ["XVPR_HOME"] = str(home)
    # No 7z reachable: PATH carries only the fixture's stub bin
    # (gio) - /usr/bin stays out so the system 7z cannot leak in.
    fixture_bin = HERE.parent / "proof" / "fixture" / "bin"
    os.environ["PATH"] = str(fixture_bin)
    (home / "settings.json").write_text(
        json.dumps({"setup_prompted": True}))

    sys.path.insert(0, str(app_root / "app"))
    spec = importlib.util.spec_from_file_location(
        "xvpr_main_setup", app_root / "app" / "main.py")
    main_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(main_mod)
    core = main_mod.core
    import tkinter as tk

    check("no 7z tool before setup",
          core.find_7z_tool(home) is None)

    root = tk.Tk()
    app = main_mod.App(root)
    app.confirm_hook = lambda kind, msg: True
    app.info_hook = lambda msg: None
    root.update()

    app.run_setup()
    results = home / "results.log"
    pump_until(root,
               lambda: results.exists()
               and "7-Zip 26.06 installed" in results.read_text(),
               180, "first-run setup")
    tool = core.find_7z_tool(home)
    check("setup installed 7zz into the app tools dir",
          tool is not None and str(home) in str(tool), str(tool))
    check("downloaded archive did not linger",
          not (home / "tools" / "7z"
               / "7z2606-linux-x64.tar.xz").exists())

    # Build a .7z with the downloaded binary and drive it through.
    src = work / "Packed Game.iso"
    make_pattern_file(src, core.SKIP_BYTES + 3 * 1024 * 1024)
    arc = work / "Packed Game.7z"
    subprocess.run([tool, "a", "-y", str(arc), src.name],
                   cwd=str(work), capture_output=True, check=True)
    src.unlink()
    app.add_path(arc)
    root.update()
    check("archive queued one item", len(app.items) == 1,
          str(app.items))
    check("item records its archive origin",
          bool(app.items[0].get("archive")))
    app.output_var.set(str(outdir))
    app.start_processing()
    pump_until(root, lambda: not app.busy, 180, "archive processing")
    item = app.items[0]
    check("archive item Done", item["status"] == "Done", str(item))
    out = outdir / "Packed Game.iso"
    check("archive output size",
          out.exists()
          and out.stat().st_size == 3 * 1024 * 1024)
    with open(out, "rb") as f:
        first = int.from_bytes(f.read(8), "little")
    check("archive output starts at block 387", first == 387)
    leftovers = [p for p in (home / "archive-scratch").rglob("*")
                 if p.is_file()]
    check("scratch copy cleaned after Done", leftovers == [],
          str(leftovers))
    root.destroy()
    import drive_gui
    print(f"\n{drive_gui.PASS} passed, {drive_gui.FAIL} failed")
    return 1 if drive_gui.FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
