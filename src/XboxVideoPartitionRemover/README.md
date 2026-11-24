# XBOX Video Partition Remover

Strips the video partition from full Redump Xbox ISOs, leaving the
game partition as a standalone ISO of about 6.9 GiB.

A full Redump dump starts with a 387 MiB video partition. This tool
does the equivalent of

```
dd if=<redump game>.iso of=<same name>.iso skip=387 bs=1M
```

in-process (no dd needed, identical on Windows and Linux), with a
queue, progress, and checks. The output keeps the input's name,
unchanged. Source files are only ever read, never modified.

## Requirements

- Python 3.10+ with Tkinter
  (Linux: `python3` + `python3-tk`; Windows: the python.org
  installer includes it).
- Linux network shares use GVFS (`gio`, from the `gvfs` /
  `gvfs-backends` packages) - only needed for the Network Share
  button.
- .7z archives need 7-Zip: on first run the app offers to download
  it from the official 7-Zip release and verifies the download's
  SHA-256 before installing it into the app's `tools/` folder.
  Nothing closed-source is bundled. Plain .iso and .zip inputs
  work without it.

## How to use

1. Pick an **output folder**.
2. **Add Game** for a single `.iso` (or `.zip` / `.7z` archive -
   the ISOs inside are extracted to local scratch and queued),
   **Add Folder** to queue every `.iso` under a folder
   (multi-disc sets queue as separate items), or **Network
   Share...** to mount a share from your server first and pick
   from there. Your whole library can stay on the server.
3. **Process Queue**. Items run one at a time with a progress bar
   (bytes, %, speed) and a status bar that says what is happening.

## Checks and safety

- An input smaller than the 387 MiB video partition is refused.
- An input that is not the expected full-dump size
  (7,835,492,352 bytes) warns and lets you decide.
- The output size must come out exactly input − 405,798,912
  bytes, or the item fails loudly in the log.
- If the output would land on top of the source file itself, the
  item is blocked - pick a different output folder.
- An existing output is never overwritten without asking.
- Output is written to a `.part` file and renamed only when it
  verifies, so cancelling never leaves a half copy that looks
  finished.

## Files the app keeps

Beside the app (or, for the AppImage, under
`~/.local/share/XboxVideoPartitionRemover/`):

- `queue.json` - the queue, saved as it changes (a crash or power
  cut never loses it; in-flight items re-queue on next launch).
- `results.log` - the results log, appended across runs.
- `settings.json` - output folder and recent shares (server,
  share, username). **Passwords are never saved** - a share
  password is used for that mount only.
- `tools/7z/` - the 7-Zip console binary from first-run setup.
- `archive-scratch/` - local scratch for archive extraction;
  extracted copies are removed when their item finishes.

## Credits

- 7-Zip by Igor Pavlov (LGPL) - downloaded on first run, never
  bundled; its license lands in `tools/7z/License.txt`.
- Network-share support follows the same proven implementation
  as the rest of the converter family.
