# XBOX Video Partition Remover — BUILD REPORT

Built 2026-11-23, per Duane's spec
(`~/workspace/xbox-video-partition-remover-spec.md`, build-go given
2026-11-23). Nothing published — releases are Duane's word.

## Deliverables

- **Linux:** `XboxVideoPartitionRemover-x86_64.AppImage`
  18,916,544 bytes, sha256
  `e54b7e716d1d4d932346c2fe601eff323bfaae4fd30956f2ecc460bd20eb3771`
  Bundles Python 3.12 + Tk (the app is stdlib-only, so no Qt —
  much smaller than the family's Qt AppImages). Rebuild with
  `build-appimage.sh`.
- **Windows:** `XboxVideoPartitionRemover-Windows.zip`
  20,093 bytes, `zip -T` OK, sha256
  `b6f623c2505b51b38b85ceb63ad401d9da691f473ccac54416231068c7d2cae8`
  Plain Python source + `.bat` launcher, no binaries, no packing
  (the family AV-clean pattern). Needs Python 3.10+ installed.

The three app sources (`main.py`, `xvpr_core.py`, `netshare.py`)
were verified **byte-identical** across the source tree, the
AppImage, and the Windows zip.

## What it does

Strips the first 387 MiB (405,798,912 bytes) from full Redump Xbox
ISOs — the in-process equivalent of
`dd if=<game>.iso of=<same name>.iso skip=387 bs=1M`, seek +
streamed copy with progress and cancel, never a dd shell-out.
Output keeps the input's name unchanged (Duane's clarification:
the dd example names were placeholders). Queue with Add Game /
Add Folder (recursive) / Network Share, sequential processing,
status bar + per-file progress (bytes, %, speed), queue and
results log persisted to disk from day one.

## Verification

**Unit suite — `tests/test_core.py`: 45 passed, 0 failed.**
Constants and size classification (refuse at/below the skip,
warn off-size, ok at 7,835,492,352); naming and path guards;
strip correctness against position-stamped content (output starts
at block 387); cancel leaves no output and no `.part`; folder
scan; zip members/extract byte-intact with the source archive's
hash unchanged; scratch-space warning both ways (patched
disk_usage); JSON persistence; **the real first-run setup** —
downloaded 7-Zip 26.06 live, SHA-256 matched the value pinned in
the code, the binary runs; .7z list/extract round-trip; the
"needs setup" message when no 7-Zip is reachable. One fixture bug
was found and fixed in the test generator itself during the run
(a sub-8-byte tail block) — the core was never at fault.

**dd-equivalence — `tests/proof_dd_equivalence.py`: PASS.**
A 471,871,545-byte deterministic, position-stamped file (odd
tail): the app strip and real `dd bs=1M skip=387` produced
byte-identical output, sha256
`510194cac01f4b9a36ad4acb938d6fc41a81062b1b66542b3b81cae88c78991c`
for both. The too-small refusal was re-exercised end-to-end.

**Packaged GUI drive — `tests/drive_gui.py`: 23 passed, 0
failed**, run twice with identical results: against the source
tree and against the extracted AppImage. Fixture: a real smbd
(Samba 4.19.5) on 127.0.0.4:445 serving an authenticated share
(wrong password → NT_STATUS_LOGON_FAILURE; a file fetched over
the wire hashed identical to the local copy). The app mounted
through its own Network Share flow, added one game + a two-disc
folder from the share, processed all three — every output the
exact size and **byte-identical to real dd output** — sources
byte-identical afterwards, queue/results persisted, share
remembered with the **password nowhere on disk**. Also driven:
the size-warning path, the output-on-source **Blocked** guard,
and the overwrite ask both ways (declined → Skipped, accepted →
Done; a drive-found bug — Skipped items were not re-processable —
was fixed in the app and the drive went fully green).

**Packaged first-run setup — `tests/proof_packaged_setup.py`:
9 passed, 0 failed** (extracted AppImage, PATH scrubbed of any
system 7z). The packaged app downloaded and hash-verified 7-Zip
itself, installed `7zz` into its own tools dir, then a `.7z`
built with that binary was added and processed through the GUI:
extract → strip → correct output → scratch copy cleaned up.

**Launch smokes:** source and AppImage (`--appimage-extract`
and `--appimage-extract-and-run`) construct the UI under xvfb
and exit 0.

## Honest caveats

- **GVFS/FUSE (the standing family caveat):** this VM has no
  `/dev/fuse`, so a real `gio mount` cannot run here. The drive's
  mount appearance is a stub `gio` that reproduces gio's exact
  prompt sequence (header line containing "password", then
  Domain, then Password) and publishes the GVFS-shaped mount
  point as a symlink to the fixture share — so the app's own
  `_gio_mount` pending-prompt code (the family's fixed reference
  implementation, lifted verbatim from the VB tools) ran for
  real, and the stub verified the answers landed in the right
  prompts (WORKGROUP in Domain, the password in Password — the
  old family bug would have failed the mount). The SMB server,
  authentication, and every byte processed were real. Against
  the **real** gio, the app's mount attempt in this VM fails in
  0.1 s with a readable error ("volume doesn't implement
  mount") and no hang. A live GVFS mount stands on the identical
  code path the sibling apps exercise on Duane's desktop, where
  gvfs is present — rig-only, as with the rest of the family.
- **Windows is code-verified only** (not run on Windows here):
  UNC paths flow through the same netshare module the family
  ships; the first-run download pins the official `7zr.exe`
  (sha256 in code) the same way the Linux path pins its tarball.
- Fixture ISOs were ~450 MB synthetic files, not a real 7.29 GiB
  Redump dump; the strip is size-agnostic and the dd-equivalence
  proof covers the byte path, but a full-size real dump has not
  been run through the packaged app here.
- The AppImage bundles this VM's Python 3.12/Tk libraries; on a
  host it still needs a compatible glibc (same family caveat as
  the other AppImages).

## Layout

- `src/XboxVideoPartitionRemover/` — the distributable folder
  (`app/main.py` GUI, `app/xvpr_core.py` core, `app/netshare.py`,
  `run.sh`, `XboxVideoPartitionRemover.bat`, `README.md`).
- `tests/` — `test_core.py`, `proof_dd_equivalence.py`,
  `drive_gui.py`, `proof_packaged_setup.py` (all re-runnable;
  the drive needs the fixture smbd — config and stub under
  `proof/fixture/`, smbd currently stopped).
- `proof/fixture/` — the SMB fixture (share content, smb.conf,
  stub gio), kept for re-runs.
