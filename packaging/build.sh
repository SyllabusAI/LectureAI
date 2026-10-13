#!/bin/zsh
# Build Syllabus.app from this checkout into dist/Syllabus.app (or $SYLLABUS_DIST).
#
#     packaging/build.sh
#
# Needs the checkout's Python with the app and build extras installed:
#     .venv/bin/pip install -e '.[app,build]'
# Set PYTHON to use another interpreter. Everything else it uses (sips,
# iconutil, codesign) ships with macOS.
#
# The result is signed ad hoc unless SYLLABUS_CODESIGN_IDENTITY names a
# Developer ID identity in the keychain (and SYLLABUS_ENTITLEMENTS a file).
#
# It is built for this Mac's architecture only, the one Python here runs as.
# The release workflow runs this once on Apple silicon and once on Intel and
# joins the two with packaging/universal.py into one app that runs on both.
set -euo pipefail

ROOT="${0:A:h:h}"
cd "$ROOT"
PY="${PYTHON:-$ROOT/.venv/bin/python}"
# Where the .app lands. Build somewhere else while a copy in dist/ is running:
# replacing files under a running bundle can crash it on its next import.
DIST="${SYLLABUS_DIST:-$ROOT/dist}"

if ! "$PY" -c "import PyInstaller, webview" 2>/dev/null; then
    echo "build.sh: $PY lacks PyInstaller or pywebview." >&2
    echo "  install them:  $PY -m pip install -e '.[app,build]'" >&2
    exit 1
fi

# The app icon, from the panel's own: an iconset of the sizes macOS wants,
# folded into one .icns. 512 is the source size, so nothing is scaled up.
BUILD="$ROOT/packaging/build"
ICONSET="$BUILD/Syllabus.iconset"
rm -rf "$ICONSET"
mkdir -p "$ICONSET"
for size in 16 32 128 256; do
    sips -z $size $size intake/static/icon.png \
        --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
    double=$((size * 2))
    sips -z $double $double intake/static/icon.png \
        --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
done
cp intake/static/icon.png "$ICONSET/icon_512x512.png"
iconutil -c icns "$ICONSET" -o "$BUILD/Syllabus.icns"

# ffmpeg and ffprobe, static LGPL builds from packaging/ffmpeg/. In order:
# a folder named in SYLLABUS_FFMPEG_DIR, a local build in packaging/build/ffmpeg
# (packaging/ffmpeg/build.sh puts one there), or the release pinned in
# packaging/ffmpeg/release, downloaded and checked against its sha256 by
# packaging/ffmpeg/fetch.sh. The pinned one is universal (arm64 and x86_64).
FFMPEG_DIR="${SYLLABUS_FFMPEG_DIR:-$BUILD/ffmpeg}"
if [[ ! -x "$FFMPEG_DIR/ffmpeg" || ! -x "$FFMPEG_DIR/ffprobe" ]]; then
    packaging/ffmpeg/fetch.sh "$FFMPEG_DIR"
fi
for f in ffmpeg ffprobe LICENSE.md COPYING.LGPLv2.1 BUILD.txt; do
    [[ -e "$FFMPEG_DIR/$f" ]] || { echo "build.sh: $FFMPEG_DIR lacks $f" >&2; exit 1; }
done
# This build's architecture has to be among ffmpeg's, or recording fails on
# the first Mac that runs the app.
ARCH="$(uname -m)"
for f in ffmpeg ffprobe; do
    [[ " $(lipo -archs "$FFMPEG_DIR/$f") " == *" $ARCH "* ]] || {
        echo "build.sh: $FFMPEG_DIR/$f has no $ARCH slice ($(lipo -archs "$FFMPEG_DIR/$f"))" >&2
        exit 1
    }
done
export SYLLABUS_FFMPEG_DIR="$FFMPEG_DIR"

"$PY" -m PyInstaller --noconfirm --clean \
    --distpath "$DIST" --workpath "$BUILD/pyinstaller" \
    packaging/syllabus.spec

APP="$DIST/Syllabus.app"
# Sparkle self-update, only when SYLLABUS_SPARKLE_PUBLIC_KEY is set (see
# docs/signing.md). Unset, nothing is added and intake/sparkle.py stays off.
# The release workflow leaves it unset here and runs packaging/sparkle.sh on
# the universal app instead, once both architectures are joined.
if [[ -n "${SYLLABUS_SPARKLE_PUBLIC_KEY:-}" ]]; then
    packaging/sparkle.sh "$APP"
fi
codesign --verify --deep --strict "$APP"
echo
echo "built $APP ($(du -sh "$APP" | cut -f1))"
echo "  version $("$PY" -c 'import intake; print(intake.__version__)'), $(codesign -dv "$APP" 2>&1 | grep -o 'Signature=.*' || echo 'signed ad hoc')"
echo "  architecture: $(lipo -archs "$APP/Contents/MacOS/Syllabus")"
echo "  ffmpeg: $("$APP/Contents/Frameworks/ffmpeg/ffmpeg" -version | head -1 | cut -d' ' -f1-3) ($(lipo -archs "$APP/Contents/Frameworks/ffmpeg/ffmpeg"))"
if [[ -n "${SYLLABUS_SPARKLE_PUBLIC_KEY:-}" ]]; then
    echo "  sparkle: feed $(plutil -extract SUFeedURL raw -o - "$APP/Contents/Info.plist")"
fi
echo "  open it:  open '$APP'"
