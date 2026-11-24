#!/usr/bin/env python3
"""Core logic for XBOX Video Partition Remover (no GUI imports).

The job: a full Redump Xbox ISO starts with a 387 MiB video
partition the game never uses. Stripping it is exactly

    dd if=<redump game>.iso of=<same name>.iso skip=387 bs=1M

done in-process (seek + streamed copy) so it behaves identically on
Windows and Linux, reports progress, and can be cancelled. Sources
are only ever read; output goes to a "<name>.part" file that is
renamed into place only after its size verifies, so a cancelled or
failed run never leaves a convincing-looking half copy behind.
"""
import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

IS_WINDOWS = sys.platform.startswith("win")

SKIP_BYTES = 387 * 1024 * 1024            # 405,798,912
EXPECTED_FULL_SIZE = 7_835_492_352        # full Redump dump
EXPECTED_OUT_SIZE = EXPECTED_FULL_SIZE - SKIP_BYTES   # 7,429,693,440
COPY_CHUNK = 8 * 1024 * 1024
# Archive handling needs room for the extracted ISO plus its output.
SCRATCH_HEADROOM = 512 * 1024 * 1024


class StripError(Exception):
    """A user-readable failure."""


class StripCancelled(Exception):
    """The cancel flag fired mid-copy."""


# ------------------------------------------------------------ paths

def app_home(app_dir=None):
    """Where state (queue, settings, tools, scratch) lives.

    XVPR_HOME wins (tests, portable installs). Otherwise the app
    folder itself when it is writable (the folder/zip
    distributions), else a per-user data dir - an AppImage's
    mount is read-only, so its state cannot live beside the app.
    """
    env = os.environ.get("XVPR_HOME", "")
    if env:
        return Path(env)
    if app_dir is not None:
        cand = Path(app_dir)
        try:
            cand.mkdir(parents=True, exist_ok=True)
            probe = cand / ".xvpr-write-probe"
            probe.write_text("x")
            probe.unlink()
            return cand
        except OSError:
            pass
    xdg = os.environ.get("XDG_DATA_HOME", "")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    home = base / "XboxVideoPartitionRemover"
    home.mkdir(parents=True, exist_ok=True)
    return home


def _norm(p):
    s = os.path.normpath(str(p))
    return s.lower() if IS_WINDOWS else s


def paths_equal(a, b):
    """True when two paths name the same file location."""
    return _norm(a) == _norm(b)


def output_path_for(source, outdir):
    """The output keeps the input's name, unchanged."""
    return Path(outdir) / Path(source).name


def check_input_size(size):
    """Classify an input size: (verdict, message).

    'refuse' - too small to even contain the video partition.
    'warn'   - not the expected full-dump size; the user decides.
    'ok'     - exactly the expected full-dump size.
    """
    if size <= SKIP_BYTES:
        return "refuse", (f"only {size:,} bytes - smaller than the "
                          f"{SKIP_BYTES:,}-byte video partition, so "
                          "there is no game partition to extract")
    if size != EXPECTED_FULL_SIZE:
        return "warn", (f"{size:,} bytes is not the expected "
                        f"full-dump size ({EXPECTED_FULL_SIZE:,}); "
                        "it may already be stripped or be a "
                        "different dump format")
    return "ok", "expected full-dump size"


def find_isos(root):
    """Every .iso under a folder, recursively, in stable order."""
    root = Path(root)
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            if name.lower().endswith(".iso"):
                out.append(Path(dirpath) / name)
    return out


# ------------------------------------------------------------ the strip

def strip_file(src, dst, progress=None, cancel=None):
    """Copy src to dst starting SKIP_BYTES in - the dd, in-process.

    progress(done, total) is called as bytes land; cancel() returning
    True aborts. Writes go to dst + '.part' and are renamed on
    success only. Returns (in_size, out_size). Raises StripError /
    StripCancelled with user-readable messages.
    """
    src, dst = Path(src), Path(dst)
    try:
        in_size = src.stat().st_size
    except OSError as ex:
        raise StripError(f"cannot read {src}: {ex}")
    verdict, msg = check_input_size(in_size)
    if verdict == "refuse":
        raise StripError(f"{src.name}: {msg}")
    if paths_equal(src, dst):
        raise StripError("output would overwrite the source file "
                         "itself - pick a different output folder")
    dst.parent.mkdir(parents=True, exist_ok=True)
    part = dst.with_name(dst.name + ".part")
    total = in_size - SKIP_BYTES
    done = 0
    try:
        with open(src, "rb") as fin, open(part, "wb") as fout:
            fin.seek(SKIP_BYTES)
            while True:
                if cancel is not None and cancel():
                    raise StripCancelled("cancelled")
                block = fin.read(COPY_CHUNK)
                if not block:
                    break
                fout.write(block)
                done += len(block)
                if progress is not None:
                    progress(done, total)
    except StripCancelled:
        _silent_unlink(part)
        raise
    except OSError as ex:
        _silent_unlink(part)
        raise StripError(f"copy failed: {ex}")
    out_size = part.stat().st_size
    if out_size != total:
        _silent_unlink(part)
        raise StripError(f"output size mismatch: wrote {out_size:,} "
                         f"bytes, expected {total:,}")
    os.replace(part, dst)
    return in_size, out_size


def _silent_unlink(p):
    try:
        Path(p).unlink()
    except OSError:
        pass


# ------------------------------------------------------------ archives

def is_archive(p):
    return Path(p).suffix.lower() in (".zip", ".7z")


def archive_iso_members(path):
    """The .iso members inside an archive (names only, no extract).

    .zip is read with zipfile. .7z needs a 7-Zip binary; without one
    this raises StripError saying first-run setup is needed.
    """
    path = Path(path)
    ext = path.suffix.lower()
    if ext == ".zip":
        try:
            with zipfile.ZipFile(path) as zf:
                return [n for n in zf.namelist()
                        if n.lower().endswith(".iso")]
        except (zipfile.BadZipFile, OSError) as ex:
            raise StripError(f"cannot read archive: {ex}")
    if ext == ".7z":
        tool = find_7z_tool()
        if tool is None:
            raise StripError("reading .7z archives needs 7-Zip, "
                             "which is not set up yet - run the "
                             "first-run setup (or install 7-Zip)")
        out = _run7z(tool, ["l", "-slt", str(path)])
        members = []
        for line in out.splitlines():
            if line.startswith("Path = "):
                name = line[len("Path = "):].strip()
                if name.lower().endswith(".iso"):
                    members.append(name)
        return members
    raise StripError(f"unsupported archive type: {path.suffix}")


def extract_archive(path, scratch_dir):
    """Extract an archive's .iso members into local scratch.

    Returns [(extracted_iso_path, member_name)]. Always local -
    the source archive is only read, never written, and nothing is
    extracted back onto a share.
    """
    path = Path(path)
    scratch_dir = Path(scratch_dir)
    scratch_dir.mkdir(parents=True, exist_ok=True)
    members = archive_iso_members(path)
    if not members:
        raise StripError(f"no .iso found inside {path.name}")
    ext = path.suffix.lower()
    out = []
    if ext == ".zip":
        with zipfile.ZipFile(path) as zf:
            infos = {i.filename: i for i in zf.infolist()}
            for name in members:
                dest = scratch_dir / Path(name).name
                with zf.open(infos[name]) as fin, \
                        open(dest, "wb") as fout:
                    shutil.copyfileobj(fin, fout, COPY_CHUNK)
                out.append((dest, name))
        return out
    # .7z: extract everything, then collect the ISOs.
    tool = find_7z_tool()
    if tool is None:
        raise StripError("extracting .7z archives needs 7-Zip, "
                         "which is not set up yet - run the "
                         "first-run setup (or install 7-Zip)")
    workdir = Path(tempfile.mkdtemp(prefix="xvpr-7z-",
                                     dir=str(scratch_dir)))
    _run7z(tool, ["x", "-y", f"-o{workdir}", str(path)])
    for name in members:
        found = workdir / name
        if not found.is_file():
            cands = [p for p in workdir.rglob("*")
                     if p.is_file()
                     and p.name.lower() == Path(name).name.lower()]
            if not cands:
                raise StripError(f"7-Zip did not extract {name}")
            found = cands[0]
        dest = scratch_dir / Path(name).name
        shutil.move(str(found), str(dest))
        out.append((dest, name))
    shutil.rmtree(workdir, ignore_errors=True)
    return out


def archive_scratch_need(path):
    """Bytes of local scratch an archive add will want: the
    extracted ISOs plus their stripped outputs, roughly.
    Returns 0 when the size cannot be determined."""
    path = Path(path)
    ext = path.suffix.lower()
    sizes = []
    if ext == ".zip":
        try:
            with zipfile.ZipFile(path) as zf:
                sizes = [i.file_size for i in zf.infolist()
                         if i.filename.lower().endswith(".iso")]
        except (zipfile.BadZipFile, OSError):
            return 0
    elif ext == ".7z":
        tool = find_7z_tool()
        if tool is None:
            return 0
        try:
            members = set(archive_iso_members(path))
            out = _run7z(tool, ["l", "-slt", str(path)])
        except StripError:
            return 0
        cur = None
        for line in out.splitlines():
            if line.startswith("Path = "):
                cur = line[len("Path = "):].strip()
            elif line.startswith("Size = ") and cur in members:
                try:
                    sizes.append(int(line[len("Size = "):].strip()))
                except ValueError:
                    pass
    if not sizes:
        return 0
    extracted = sum(sizes)
    outputs = sum(max(0, s - SKIP_BYTES) for s in sizes)
    return extracted + outputs


def scratch_space_warning(path, scratch_dir):
    """A warning string when local scratch looks too small for an
    archive add, else ''."""
    need = archive_scratch_need(path) + SCRATCH_HEADROOM
    if need <= 0:
        return ""
    try:
        free = shutil.disk_usage(str(scratch_dir)).free
    except OSError:
        return ""
    if free < need:
        return (f"low local disk space: extracting and processing "
                f"{Path(path).name} wants about "
                f"{need / 1e9:.1f} GB and only {free / 1e9:.1f} GB "
                "is free")
    return ""


# ------------------------------------------------------------ 7-Zip

SEVENZIP_VERSION = "26.06"
if IS_WINDOWS:
    SEVENZIP_URL = ("https://github.com/ip7z/7zip/releases/download/"
                    "26.06/7zr.exe")
    SEVENZIP_SHA256 = ("e80db2dad715a6c5adabe292d31ed54e11f4"
                       "e9b99ca349827b50573b175891bc")
    SEVENZIP_FILE = "7zr.exe"
else:
    # The URL 7-zip.org's own download page links for Linux x64.
    SEVENZIP_URL = ("https://github.com/ip7z/7zip/releases/download/"
                    "26.06/7z2606-linux-x64.tar.xz")
    SEVENZIP_SHA256 = ("2b539cf067b2ad48e1baa5093dbaa8818b73542d"
                       "1222a0ffde9dc5f2922ea62e")
    SEVENZIP_FILE = "7z2606-linux-x64.tar.xz"


def tools_dir(home=None):
    d = (Path(home) if home else app_home()) / "tools" / "7z"
    return d


def find_7z_tool(home=None):
    """The 7-Zip console binary: the app's own tools dir first
    (first-run setup lands it there), then the system PATH."""
    names = ["7zz", "7zr.exe", "7z", "7za", "7zr"] if IS_WINDOWS \
        else ["7zz", "7z", "7za", "7zr"]
    tdir = tools_dir(home)
    for n in names:
        cand = tdir / n
        if cand.is_file():
            return str(cand)
    for n in names:
        found = shutil.which(n)
        if found:
            return found
    return None


def _run7z(tool, args):
    p = subprocess.run([tool] + args, capture_output=True,
                       text=True, timeout=3600)
    if p.returncode != 0:
        tail = (p.stdout + p.stderr).strip().splitlines()
        raise StripError("7-Zip failed: "
                         + (tail[-1] if tail else f"exit {p.returncode}"))
    return p.stdout


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(COPY_CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def run_first_run_setup(home=None, log=None):
    """Download 7-Zip from the official release, verify its
    SHA-256 against the value pinned in this file, and install
    the console binary into the app's tools dir.

    Returns (ok, message). The core strip needs no engine - this
    exists so .7z archives work, following the family's
    first-run-download pattern (nothing closed is bundled).
    """
    log = log or (lambda msg: None)
    home = Path(home) if home else app_home()
    tdir = tools_dir(home)
    tdir.mkdir(parents=True, exist_ok=True)
    dl = tdir / SEVENZIP_FILE
    log(f"downloading 7-Zip {SEVENZIP_VERSION} from "
        f"{SEVENZIP_URL}")
    try:
        req = urllib.request.Request(
            SEVENZIP_URL,
            headers={"User-Agent": "XboxVideoPartitionRemover/1.0"})
        with urllib.request.urlopen(req, timeout=120) as resp, \
                open(dl, "wb") as fout:
            shutil.copyfileobj(resp, fout, COPY_CHUNK)
    except Exception as ex:
        _silent_unlink(dl)
        return False, f"download failed: {ex}"
    digest = sha256_file(dl)
    if digest.lower() != SEVENZIP_SHA256.lower():
        _silent_unlink(dl)
        return False, ("downloaded file failed its SHA-256 check "
                       f"(got {digest[:16]}...) - not installed")
    log("download verified (SHA-256 matches the pinned value)")
    try:
        if IS_WINDOWS:
            dest = tdir / "7zr.exe"
            os.replace(dl, dest)
        else:
            with tarfile.open(dl, "r:xz") as tf:
                for member in ("7zz", "License.txt"):
                    srcobj = tf.extractfile(member)
                    if srcobj is None:
                        raise StripError(f"{member} missing from "
                                         "the 7-Zip download")
                    dest = tdir / member
                    with open(dest, "wb") as fout:
                        shutil.copyfileobj(srcobj, fout, COPY_CHUNK)
            (tdir / "7zz").chmod(0o755)
            _silent_unlink(dl)
    except Exception as ex:
        return False, f"install failed: {ex}"
    tool = find_7z_tool(home)
    if tool is None:
        return False, "installed, but the 7-Zip binary was not found"
    log(f"7-Zip ready: {tool}")
    return True, f"7-Zip {SEVENZIP_VERSION} installed"


# ------------------------------------------------------------ persistence

def save_json_atomic(path, data):
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    import json
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
        f.write("\n")
    os.replace(tmp, path)


def load_json(path, default):
    import json
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def stamp():
    return time.strftime("%Y-%m-%d %H:%M:%S")
