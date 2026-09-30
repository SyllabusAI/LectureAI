#!/bin/zsh
# Sign a built Syllabus.app with a Developer ID identity, ready to notarize.
#
#     packaging/sign.sh path/to/Syllabus.app "<identity>" [entitlements.plist]
#
# <identity> is a Developer ID Application identity (its name or SHA-1) in a
# keychain on the search list, or set SYLLABUS_KEYCHAIN to name the keychain.
# "-" signs ad hoc with the hardened runtime and no timestamp, which is how
# this script is checked on a Mac without a certificate.
#
# Signed inside out, the order Apple asks for, rather than with one
# `codesign --deep`: --deep would put the app's entitlements on every nested
# library and would re-sign Sparkle's helpers without their own. So:
#   1. every Mach-O file in the bundle outside Sparkle.framework, executables
#      with the entitlements (Syllabus itself, ffmpeg, ffprobe), libraries and
#      Python extension modules without;
#   2. each nested .framework other than Sparkle (PyInstaller's Python.framework);
#   3. Sparkle.framework, if the build carries it, piece by piece as Sparkle's
#      documentation lays out;
#   4. the app, with the entitlements.
# Then `codesign --verify --deep --strict` checks the whole tree.
set -euo pipefail

APP="${1:?usage: sign.sh path/to/Syllabus.app <identity> [entitlements]}"
APP="${APP:A}"
ID="${2:?usage: sign.sh path/to/Syllabus.app <identity> [entitlements]}"
ENT="${3:-${0:A:h}/entitlements.plist}"
ENT="${ENT:A}"

[[ -d "$APP/Contents" ]] || { echo "sign.sh: no app at $APP" >&2; exit 1; }
[[ -f "$ENT" ]] || { echo "sign.sh: no entitlements at $ENT" >&2; exit 1; }
plutil -lint -s "$ENT"

base=(codesign --force --sign "$ID" --options runtime)
# Notarization requires a secure timestamp; an ad hoc signature cannot have one.
[[ "$ID" != "-" ]] && base+=(--timestamp)
[[ -n "${SYLLABUS_KEYCHAIN:-}" ]] && base+=(--keychain "$SYLLABUS_KEYCHAIN")

SPARKLE="$APP/Contents/Frameworks/Sparkle.framework"
MAIN="$APP/Contents/MacOS/Syllabus"
count=0

# 1. Loose Mach-O files. Symlinks are skipped: they point at files signed here.
while IFS= read -r -d '' f; do
    [[ "$f" == "$SPARKLE"/* || "$f" == "$MAIN" ]] && continue
    kind="$(file -b "$f")"
    [[ "$kind" == Mach-O* ]] || continue
    if [[ "$kind" == *executable* ]]; then
        "${base[@]}" --entitlements "$ENT" "$f"
    else
        "${base[@]}" "$f"
    fi
    count=$((count + 1))
done < <(find "$APP/Contents" -type f -print0)

# 2. Nested frameworks, deepest first, Sparkle aside.
while IFS= read -r fw; do
    [[ "$fw" == "$SPARKLE" ]] && continue
    "${base[@]}" "$fw"
done < <(find "$APP/Contents" -type d -name '*.framework' | awk '{ print length, $0 }' | sort -rn | cut -d' ' -f2-)

# 3. Sparkle, as https://sparkle-project.org/documentation/sandboxing/ and its
# code signing notes describe for an app that is not sandboxed.
if [[ -d "$SPARKLE" ]]; then
    B="$SPARKLE/Versions/B"
    [[ -d "$B/XPCServices/Installer.xpc" ]] && "${base[@]}" "$B/XPCServices/Installer.xpc"
    [[ -d "$B/XPCServices/Downloader.xpc" ]] && \
        "${base[@]}" --preserve-metadata=entitlements "$B/XPCServices/Downloader.xpc"
    "${base[@]}" "$B/Autoupdate"
    "${base[@]}" "$B/Updater.app"
    "${base[@]}" "$SPARKLE"
fi

# 4. The app itself.
"${base[@]}" --entitlements "$ENT" "$APP"

codesign --verify --deep --strict --verbose=2 "$APP"
echo "signed $APP ($count nested files) as ${ID}"
codesign -dv "$APP" 2>&1 | grep -E '^(Authority|TeamIdentifier|Timestamp|Runtime)' || true
