#!/usr/bin/env python3
"""Packaged GUI drive for XBOX Video Partition Remover.

Drives the REAL App class from a given app root (source tree or an
extracted AppImage) under xvfb, against the loopback SMB fixture:

  real smbd on 127.0.0.4:445, authenticated share XVPRTEST
  + stub `gio` reproducing gio's prompt sequence, so the app's own
    Network Share mount flow (netshare._gio_mount) runs for real;
    the stub publishes the GVFS-shaped mount point as a symlink to
    the fixture share, because this VM has no /dev/fuse for a real
    GVFS mount.

Checks: mount via the app's flow (prompt answers land in the
right prompts - the family bug), Add Game + Add Folder from the
share, processing, outputs byte-identical to real dd output,
sources untouched, queue/results persisted, password nowhere on
disk, output-on-source block, overwrite ask both ways.

Run: xvfb-run -a python3 drive_gui.py <app_root> <work_dir>
"""
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIX = HERE.parent / "proof" / "fixture"
PASSWORD = "xvprpass123"
SERVER, SHARE, USER = "127.0.0.4", "XVPRTEST", "xvprtest"

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"FAIL  {name} {extra}")


def pump_until(root, cond, timeout=180, what=""):
    t0 = time.time()
    while time.time() - t0 < timeout:
        root.update()
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError(f"timeout waiting for {what}")


def main():
    app_root = Path(sys.argv[1]).resolve()
    work = Path(sys.argv[2]).resolve()
    home = work / "home"
    xdg = work / "xdg"
    outdir = work / "out"
    for d in (home, xdg / "gvfs", outdir):
        d.mkdir(parents=True, exist_ok=True)
    stub_log = work / "stub-gio.log"
    stub_log.write_text("")

    os.environ["XVPR_HOME"] = str(home)
    os.environ["XDG_RUNTIME_DIR"] = str(xdg)
    os.environ["XVPR_STUB_LOG"] = str(stub_log)
    os.environ["XVPR_FIXTURE_SHARE"] = str(FIX / "share")
    os.environ["XVPR_FIXTURE_PASSWORD"] = PASSWORD
    os.environ["PATH"] = str(FIX / "bin") + os.pathsep \
        + os.environ["PATH"]
    # Second-launch state: no first-run modal in the drive.
    (home / "settings.json").write_text(
        json.dumps({"setup_prompted": True}))

    sys.path.insert(0, str(app_root / "app"))
    spec = importlib.util.spec_from_file_location(
        "xvpr_main", app_root / "app" / "main.py")
    main_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(main_mod)
    core = main_mod.core
    import tkinter as tk

    share_root = FIX / "share"
    srcs = {"ShareGame.iso": share_root / "ShareGame.iso",
            "Disc 1.iso": share_root / "MultiDisc" / "Disc 1.iso",
            "Disc 2.iso": share_root / "MultiDisc" / "Disc 2.iso"}
    hashes_before = {k: core.sha256_file(v) for k, v in srcs.items()}

    root = tk.Tk()
    app = main_mod.App(root)
    confirms = []
    infos = []
    app.confirm_hook = lambda kind, msg: confirms.append(kind) \
        or True
    app.info_hook = lambda msg: infos.append(msg)
    root.update()

    # ---- mount through the app's own Network Share flow
    result = {}
    app.mount_share_async(SERVER, SHARE, USER, PASSWORD, "WORKGROUP",
                          lambda p, e: result.update(path=p,
                                                     err=e))
    pump_until(root, lambda: "path" in result, 60, "mount callback")
    mount = Path(result["path"])
    check("mount returned no error", result["err"] == "",
          result["err"])
    check("mount path is the GVFS-shaped point",
          mount.name == f"smb-share:server={SERVER},share={SHARE}",
          str(mount))
    check("share content visible through the mount",
          (mount / "ShareGame.iso").is_file())
    stub = stub_log.read_text()
    check("stub gio got WORKGROUP in the Domain prompt",
          "domain_answer=[WORKGROUP]" in stub, stub)
    check("stub gio got the password in the Password prompt",
          f"password_answer=[{PASSWORD}]" in stub)

    # ---- add from the share: one game + a folder (2 discs)
    app.add_path(mount / "ShareGame.iso")
    app.add_folder(mount / "MultiDisc")
    root.update()
    check("3 items queued", len(app.items) == 3,
          str([i["name"] for i in app.items]))
    check("size-warning path exercised (fixture sizes)",
          "size_warning" in confirms, str(confirms))
    settings_text = (home / "settings.json").read_text()
    check("share remembered in settings",
          SERVER in settings_text and SHARE in settings_text)
    check("password NOT in settings",
          PASSWORD not in settings_text)
    queue_text = (home / "queue.json").read_text()
    check("queue persisted, password NOT in queue file",
          PASSWORD not in queue_text and "ShareGame" in queue_text)

    # ---- process
    app.output_var.set(str(outdir))
    app.start_processing()
    pump_until(root, lambda: not app.busy, 300, "processing")
    statuses = {i["name"]: i["status"] for i in app.items}
    check("all 3 items Done",
          all(s == "Done" for s in statuses.values()),
          str(statuses))

    # ---- outputs byte-identical to real dd
    for name, src in srcs.items():
        out = outdir / name
        dd_out = work / f"dd-{name}"
        subprocess.run(["dd", f"if={src}", f"of={dd_out}", "bs=1M",
                        "skip=387", "status=none"], check=True)
        check(f"{name}: output size is input - 387 MiB",
              out.stat().st_size
              == src.stat().st_size - core.SKIP_BYTES)
        check(f"{name}: output byte-identical to dd",
              core.sha256_file(out) == core.sha256_file(dd_out))
    check("sources byte-identical after processing",
          all(core.sha256_file(v) == hashes_before[k]
              for k, v in srcs.items()))
    results = (home / "results.log").read_text()
    check("results log persisted with Done lines",
          "Done ShareGame.iso" in results
          and "Done Disc 1.iso" in results)

    # ---- output-on-source guard (through the real process path)
    app.add_path(mount / "ShareGame.iso")
    app.output_var.set(str(mount))
    app.start_processing()
    pump_until(root, lambda: not app.busy, 120, "blocked run")
    last = app.items[-1]
    check("output-on-source item is Blocked",
          last["status"] == "Blocked", str(last))
    check("source still intact after blocked run",
          core.sha256_file(srcs["ShareGame.iso"])
          == hashes_before["ShareGame.iso"])

    # ---- overwrite ask: declined -> Skipped, accepted -> Done
    app.output_var.set(str(outdir))
    app.add_path(mount / "ShareGame.iso")
    answers = {"overwrite": False}
    app.confirm_hook = lambda kind, msg: answers.get(kind, True)
    app.start_processing()
    pump_until(root, lambda: not app.busy, 120, "overwrite-decline")
    check("overwrite declined -> Skipped",
          app.items[-1]["status"] == "Skipped", str(app.items[-1]))
    app.confirm_hook = lambda kind, msg: True
    app.start_processing()
    pump_until(root, lambda: not app.busy, 120, "overwrite-accept")
    check("overwrite accepted -> Done",
          app.items[-1]["status"] == "Done", str(app.items[-1]))

    root.destroy()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
