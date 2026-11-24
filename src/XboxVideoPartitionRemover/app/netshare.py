#!/usr/bin/env python3
"""Network-share browsing for XBOX Video Partition Remover.

Duane's whole game library lives on his home server (SMB shares), so
the pickers must reach network shares, not just local disks:

- Windows: shares are plain UNC paths (\\\\SERVER\\share\\...). The
  native file dialog browses them, and every path the engine touches
  (probe, scaffold, package) is passed as a normal path argument, so a
  UNC path flows through unchanged. This module just builds and
  validates the UNC path for the "Network share..." dialog.

- Linux: a share becomes a real path by mounting it with GVFS
  (gio mount smb://server/share), which lands it under
  $XDG_RUNTIME_DIR/gvfs/smb-share:server=...,share=... - a plain POSIX
  directory the converter can read and write like any local folder.
  We deliberately do NOT rely on the desktop file dialog happening to
  show network places: the app mounts the share itself, then points
  the ordinary pickers at the mounted path.

No Qt imports here: the mount/list logic is testable headless, and the
GUI drives it on a worker thread. Passwords are used for the mount
attempt only - they are never stored (settings remember server,
share and username, never the password).
"""
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

IS_WINDOWS = sys.platform.startswith("win")


# ------------------------------------------------------------ paths

def is_unc(p):
    """True for a Windows UNC path (\\\\SERVER\\share or //SERVER/share)."""
    s = str(p).replace("\\", "/")
    return s.startswith("//") and len(s.strip("/")) > 0


def unc_root(server, share):
    """\\\\server\\share for the dialog's Windows route."""
    return "\\\\" + server.strip("\\/") + "\\" + share.strip("\\/")


def gvfs_dir():
    """The per-user GVFS mount root, or None when there is none."""
    xdg = os.environ.get("XDG_RUNTIME_DIR", "")
    cands = []
    if xdg:
        cands.append(Path(xdg) / "gvfs")
    cands.append(Path(f"/run/user/{os.getuid()}") / "gvfs")
    for c in cands:
        if c.is_dir():
            return c
    return cands[0] if cands else None


_GVFS_SMB_RE = re.compile(r"^smb-share:server=(?P<server>[^,]+),"
                          r"share=(?P<share>.+)$")


def list_smb_mounts():
    """Currently mounted GVFS smb shares:
    [{server, share, path}] with path a real local directory."""
    out = []
    base = gvfs_dir()
    if base is None or not base.is_dir():
        return out
    for entry in sorted(base.iterdir()):
        m = _GVFS_SMB_RE.match(entry.name)
        if m and entry.is_dir():
            out.append({
                "server": urllib.parse.unquote(m.group("server")),
                "share": urllib.parse.unquote(m.group("share")),
                "path": str(entry),
            })
    return out


def find_mount(server, share):
    """The local path of an already-mounted share, or None."""
    for m in list_smb_mounts():
        if (m["server"].lower() == server.lower()
                and m["share"].lower() == share.lower()):
            return m["path"]
    return None


def smb_url(server, share, username=""):
    user = f"{urllib.parse.quote(username, safe='')}@" if username else ""
    return (f"smb://{user}{urllib.parse.quote(server, safe='.-')}/"
            f"{urllib.parse.quote(share, safe='')}")


def gio_available():
    return shutil.which("gio") is not None


# ------------------------------------------------------------ mounting

def _gio_mount(url, username, password, workgroup, timeout=60):
    """Run `gio mount url`, answering its credential prompts.

    gio asks for credentials on its stdin when a mount needs them -
    for SMB the order is "Domain [WORKGROUP]:" first, then
    "Password:", under a header line that itself contains the word
    "password" ("Enter password for share ..."). So the whole
    buffer must NOT be keyword-searched: only the pending prompt -
    the text after the last newline, since prompts are printed
    without one - says what is actually being asked. We drive gio
    through a pty so the prompts behave exactly as they do in a
    terminal, and feed the answers the user typed into the dialog.
    Returns (rc, output text)."""
    import pty

    master, slave = pty.openpty()
    p = subprocess.Popen(["gio", "mount", url], stdin=slave,
                         stdout=slave, stderr=slave,
                         close_fds=True)
    os.close(slave)
    buf = b""
    answered = 0
    deadline = time.time() + timeout
    os.set_blocking(master, False)
    while True:
        if p.poll() is not None:
            # drain whatever is left
            try:
                while True:
                    chunk = os.read(master, 4096)
                    if not chunk:
                        break
                    buf += chunk
            except OSError:
                pass
            break
        if time.time() > deadline:
            p.kill()
            os.close(master)
            return 124, (buf.decode("utf-8", "replace")
                         + "\n(mount timed out)")
        try:
            chunk = os.read(master, 4096)
            if chunk:
                buf += chunk
        except BlockingIOError:
            time.sleep(0.05)
            continue
        except OSError:
            break
        text = buf.decode("utf-8", "replace")
        pending = text.replace("\r", "\n").rsplit("\n", 1)[-1]
        answer = None
        if "omain" in pending:
            answer = workgroup or "WORKGROUP"
        elif "assword" in pending:
            answer = password
        elif "sername" in pending or pending.rstrip().endswith("ser:"):
            answer = username
        if answer is not None and answered < 8:
            os.write(master, (answer + "\n").encode())
            answered += 1
            buf = b""
    rc = p.wait()
    try:
        os.close(master)
    except OSError:
        pass
    return rc, buf.decode("utf-8", "replace")


def mount_share(server, share, username="", password="",
                workgroup="", timeout=60):
    """Make server/share usable as a local path.

    Returns (path, error): exactly one of them is non-empty. On
    Windows the UNC root itself is the path (no mounting step); on
    Linux the GVFS mount path. A share that is already mounted is
    returned as-is, so reconnecting is free."""
    server, share = server.strip(), share.strip()
    if not server or not share:
        return "", "server and share are both required"
    if IS_WINDOWS:
        root = unc_root(server, share)
        return (root, "") if os.path.isdir(root) else (
            "", f"cannot reach {root} - check the server is on and "
                "the share name is right")
    existing = find_mount(server, share)
    if existing:
        return existing, ""
    if not gio_available():
        return "", ("gio is not installed - network mounting needs "
                    "the gvfs packages (see Help: 'gvfs gvfs-backends')")
    url = smb_url(server, share, username)
    try:
        rc, out = _gio_mount(url, username, password, workgroup, timeout)
    except Exception as ex:
        return "", f"mount failed: {ex}"
    if rc != 0:
        msg = " ".join(out.split()) or f"gio mount exited {rc}"
        return "", f"could not mount {url}: {msg}"
    # gio can take a moment to publish the mount point.
    for _ in range(50):
        path = find_mount(server, share)
        if path:
            return path, ""
        time.sleep(0.2)
    return "", (f"gio reports {url} mounted, but no mount point "
                "appeared under the GVFS folder")


def describe_path(p):
    """Short human label for a path: which share it sits on, if any."""
    s = str(p)
    if is_unc(s):
        parts = [x for x in s.replace("\\", "/").split("/") if x]
        if len(parts) >= 2:
            return f"network share \\\\{parts[0]}\\{parts[1]}"
        return "network share"
    base = gvfs_dir()
    if base is not None and s.startswith(str(base) + os.sep):
        for m in list_smb_mounts():
            if s == m["path"] or s.startswith(m["path"] + os.sep):
                return f"network share {m['server']}/{m['share']}"
    return ""
