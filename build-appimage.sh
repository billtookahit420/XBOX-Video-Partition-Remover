#!/bin/bash
# Build XboxVideoPartitionRemover-x86_64.AppImage from src/.
# Bundles the system Python 3.12 + Tk (the app is stdlib-only),
# following the family AppImage pattern. appimagetool is fetched
# once into proof/appimage-build/tools/ (a build tool, not shipped).
set -euo pipefail
PROJ="$(cd "$(dirname "$0")" && pwd)"
BUILD="$PROJ/proof/appimage-build"
APPDIR="$BUILD/XboxVideoPartitionRemover.AppDir"
OUT="$PROJ/XboxVideoPartitionRemover-x86_64.AppImage"
TOOL="$BUILD/tools/appimagetool-x86_64.AppImage"

mkdir -p "$BUILD/tools"
if [ ! -f "$TOOL" ]; then
    echo "fetching appimagetool..."
    curl -sL --max-time 300 -o "$TOOL" \
        https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage
    chmod +x "$TOOL"
fi
if [ ! -x "$BUILD/tools/appimagetool-extracted/AppRun" ]; then
    (cd "$BUILD/tools" && rm -rf appimagetool-extracted squashfs-root \
        && ./appimagetool-x86_64.AppImage --appimage-extract \
           >/dev/null \
        && mv squashfs-root appimagetool-extracted)
fi

rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/lib" \
         "$APPDIR/usr/share/tcltk" "$APPDIR/usr/share/xvpr"

# Python binary + stdlib (no test suite, no pyc caches)
cp -L /usr/bin/python3.12 "$APPDIR/usr/bin/python3.12"
chmod +x "$APPDIR/usr/bin/python3.12"
cp -r /usr/lib/python3.12 "$APPDIR/usr/lib/python3.12"
rm -rf "$APPDIR/usr/lib/python3.12/test" \
       "$APPDIR/usr/lib/python3.12/idlelib" \
       "$APPDIR/usr/lib/python3.12/turtledemo"
find "$APPDIR/usr/lib/python3.12" -name __pycache__ -type d \
    -exec rm -rf {} + 2>/dev/null || true

# Tcl/Tk script libraries
cp -r /usr/share/tcltk/tcl8.6 /usr/share/tcltk/tk8.6 \
    "$APPDIR/usr/share/tcltk/"

# Shared libraries (ldd closure of python + _tkinter + tk + tcl),
# minus the core glibc set every host provides.
EXCLUDE='^(libc|libm|libdl|libpthread|librt|libresolv|libutil|ld-linux)'
{
    ldd /usr/bin/python3.12
    ldd /usr/lib/python3.12/lib-dynload/_tkinter.cpython-312-x86_64-linux-gnu.so
    ldd /lib/x86_64-linux-gnu/libtk8.6.so
    ldd /lib/x86_64-linux-gnu/libtcl8.6.so
} | grep -oE '/[^ ]+\.so[^ ]*' | sort -u | while read -r lib; do
    base="$(basename "$lib")"
    if echo "$base" | grep -qE "$EXCLUDE"; then
        continue
    fi
    cp -L "$lib" "$APPDIR/usr/lib/$base"
done

# The app itself
cp -r "$PROJ/src/XboxVideoPartitionRemover/app" \
      "$APPDIR/usr/share/xvpr/app"
cp "$PROJ/src/XboxVideoPartitionRemover/README.md" \
   "$APPDIR/usr/share/xvpr/"
find "$APPDIR/usr/share/xvpr" -name __pycache__ -type d \
    -exec rm -rf {} + 2>/dev/null || true

# Icon: a disc with a wedge removed (the stripped partition),
# drawn pixel-wise - no image tooling needed.
python3 - "$APPDIR/xvpr.png" <<'PYEOF'
import math, struct, sys, zlib

S = 256
rows = []
for y in range(S):
    row = bytearray()
    for x in range(S):
        dx, dy = x - 127.5, y - 127.5
        r = math.hypot(dx, dy)
        ang = math.degrees(math.atan2(dy, dx))
        px = (0, 0, 0, 0)
        if r <= 112:
            if -58 <= ang <= 24 and r > 40:
                px = (0, 0, 0, 0)          # the removed wedge
            elif r <= 26:
                px = (52, 58, 66, 255)     # hub
            elif r <= 40:
                px = (140, 148, 158, 255)  # hub ring
            elif r > 100:
                px = (120, 128, 138, 255)  # rim
            else:
                px = (206, 213, 220, 255)  # disc surface
        row += bytes(px)
    rows.append(b"\x00" + bytes(row))
raw = b"".join(rows)

def chunk(tag, data):
    c = struct.pack(">I", len(data)) + tag + data
    return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

png = (b"\x89PNG\r\n\x1a\n"
       + chunk(b"IHDR", struct.pack(">IIBBBBB", S, S, 8, 6, 0, 0, 0))
       + chunk(b"IDAT", zlib.compress(raw, 9))
       + chunk(b"IEND", b""))
open(sys.argv[1], "wb").write(png)
print("icon written")
PYEOF

cat > "$APPDIR/XboxVideoPartitionRemover.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=XBOX Video Partition Remover
Comment=Strip the video partition from full Redump Xbox ISOs
Exec=xbox-video-partition-remover
Icon=xvpr
Categories=Utility;
Terminal=false
EOF

cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
export PYTHONHOME="$HERE/usr"
export LD_LIBRARY_PATH="$HERE/usr/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export TCL_LIBRARY="$HERE/usr/share/tcltk/tcl8.6"
export TK_LIBRARY="$HERE/usr/share/tcltk/tk8.6"
exec "$HERE/usr/bin/python3.12" "$HERE/usr/share/xvpr/app/main.py" "$@"
EOF
chmod +x "$APPDIR/AppRun"

rm -f "$OUT"
ARCH=x86_64 "$BUILD/tools/appimagetool-extracted/AppRun" \
    "$APPDIR" "$OUT" 2>&1 | tail -3
ls -la "$OUT"
sha256sum "$OUT"
