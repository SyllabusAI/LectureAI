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
# packaging/ffmpeg/release, downloaded and checked against its sha256.
FFMPEG_DIR="${SYLLABUS_FFMPEG_DIR:-$BUILD/ffmpeg}"
if [[ ! -x "$FFMPEG_DIR/ffmpeg" || ! -x "$FFMPEG_DIR/ffprobe" ]]; then
    eval "$(sed 's/^/PIN_/' packaging/ffmpeg/release)"
    if [[ -z "${PIN_sha256:-}" ]]; then
        echo "build.sh: no ffmpeg build to bundle." >&2
        echo "  packaging/ffmpeg/release has no sha256 yet: publish one with the" >&2
        echo "  'ffmpeg for Syllabus.app' workflow and record it there, or build" >&2
        echo "  locally with packaging/ffmpeg/build.sh, or set SYLLABUS_FFMPEG_DIR." >&2
        exit 1
    fi
    URL="https://github.com/SyllabusAI/LectureAI/releases/download/$PIN_tag/$PIN_asset"
    echo "fetching $URL"
    mkdir -p "$FFMPEG_DIR"
    curl -sfL -o "$BUILD/$PIN_asset" "$URL"
    echo "$PIN_sha256  $BUILD/$PIN_asset" | shasum -a 256 -c -
    tar -xzf "$BUILD/$PIN_asset" -C "$FFMPEG_DIR"
fi
for f in ffmpeg ffprobe LICENSE.md COPYING.LGPLv2.1 BUILD.txt; do
    [[ -e "$FFMPEG_DIR/$f" ]] || { echo "build.sh: $FFMPEG_DIR lacks $f" >&2; exit 1; }
done
export SYLLABUS_FFMPEG_DIR="$FFMPEG_DIR"

# Sparkle self-update, only when SYLLABUS_SPARKLE_PUBLIC_KEY is set (the
# release workflow sets it once the EdDSA key and Developer ID signing are in
# place; see docs/signing.md). Unset, nothing below runs and the bundle is
# what it always was. The framework is the release pinned in
# packaging/sparkle/release, checked against its sha256; syllabus.spec reads
# the same variable for the Info.plist keys, and intake/sparkle.py starts it.
SPARKLE_DIR=""
if [[ -n "${SYLLABUS_SPARKLE_PUBLIC_KEY:-}" ]]; then
    eval "$(sed 's/^/SPK_/' packaging/sparkle/release)"
    SPARKLE_DIR="$BUILD/sparkle-$SPK_version"
    if [[ ! -d "$SPARKLE_DIR/Sparkle.framework" ]]; then
        URL="https://github.com/sparkle-project/Sparkle/releases/download/$SPK_version/$SPK_asset"
        echo "fetching $URL"
        rm -rf "$SPARKLE_DIR"
        mkdir -p "$SPARKLE_DIR"
        curl -sfL -o "$BUILD/$SPK_asset" "$URL"
        echo "$SPK_sha256  $BUILD/$SPK_asset" | shasum -a 256 -c -
        tar -xf "$BUILD/$SPK_asset" -C "$SPARKLE_DIR"
    fi
    export SYLLABUS_SPARKLE_BIN="$SPARKLE_DIR/bin"
fi

"$PY" -m PyInstaller --noconfirm --clean \
    --distpath "$DIST" --workpath "$BUILD/pyinstaller" \
    packaging/syllabus.spec

APP="$DIST/Syllabus.app"
if [[ -n "$SPARKLE_DIR" ]]; then
    ditto "$SPARKLE_DIR/Sparkle.framework" "$APP/Contents/Frameworks/Sparkle.framework"
    # The framework arrived after PyInstaller sealed the bundle, so seal it
    # again: with the Developer ID identity if one was given, else ad hoc
    # (the release workflow signs afterward with packaging/sign.sh).
    if [[ -n "${SYLLABUS_CODESIGN_IDENTITY:-}" ]]; then
        packaging/sign.sh "$APP" "$SYLLABUS_CODESIGN_IDENTITY" \
            "${SYLLABUS_ENTITLEMENTS:-packaging/entitlements.plist}"
    else
        codesign --force --sign - "$APP"
    fi
fi
codesign --verify --deep --strict "$APP"
echo
echo "built $APP ($(du -sh "$APP" | cut -f1))"
echo "  version $("$PY" -c 'import intake; print(intake.__version__)'), $(codesign -dv "$APP" 2>&1 | grep -o 'Signature=.*' || echo 'signed ad hoc')"
echo "  ffmpeg: $("$APP/Contents/Frameworks/ffmpeg/ffmpeg" -version | head -1 | cut -d' ' -f1-3)"
if [[ -n "$SPARKLE_DIR" ]]; then
    echo "  sparkle: $SPK_version, feed $(plutil -extract SUFeedURL raw -o - "$APP/Contents/Info.plist")"
fi
echo "  open it:  open '$APP'"
