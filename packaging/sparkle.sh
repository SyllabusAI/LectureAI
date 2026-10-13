#!/bin/zsh
# Add Sparkle self-update to a built Syllabus.app.
#
#     SYLLABUS_SPARKLE_PUBLIC_KEY=<base64> packaging/sparkle.sh path/to/Syllabus.app
#
# Copies in the Sparkle.framework release pinned in packaging/sparkle/release
# (checked against its sha256) and writes the Info.plist keys intake/sparkle.py
# looks for: the EdDSA public key, the feed URL ($SYLLABUS_SPARKLE_FEED_URL or
# the GitHub releases appcast), and the daily check. The framework ships with
# both arm64 and x86_64 slices, so it suits the universal app as it is.
#
# Run after the bundle is complete and before signing: packaging/build.sh
# calls it for a local build, and the release workflow calls it on the
# universal app. It leaves the bundle sealed ad hoc, or with
# $SYLLABUS_CODESIGN_IDENTITY when one is given; the release workflow signs
# with Developer ID afterward through packaging/sign.sh. Sparkle's own tools
# (sign_update, for the appcast) stay in packaging/build/sparkle-<version>/bin.
set -euo pipefail

ROOT="${0:A:h:h}"
APP="${1:?usage: sparkle.sh path/to/Syllabus.app}"
APP="${APP:A}"
KEY="${SYLLABUS_SPARKLE_PUBLIC_KEY:?sparkle.sh: SYLLABUS_SPARKLE_PUBLIC_KEY is not set}"
FEED="${SYLLABUS_SPARKLE_FEED_URL:-https://github.com/SyllabusAI/LectureAI/releases/latest/download/appcast.xml}"
[[ -d "$APP/Contents" ]] || { echo "sparkle.sh: no app at $APP" >&2; exit 1; }

BUILD="$ROOT/packaging/build"
eval "$(sed 's/^/SPK_/' "$ROOT/packaging/sparkle/release")"
SPARKLE_DIR="$BUILD/sparkle-$SPK_version"
mkdir -p "$BUILD"
if [[ ! -d "$SPARKLE_DIR/Sparkle.framework" ]]; then
    URL="https://github.com/sparkle-project/Sparkle/releases/download/$SPK_version/$SPK_asset"
    echo "fetching $URL"
    rm -rf "$SPARKLE_DIR"
    mkdir -p "$SPARKLE_DIR"
    curl -sfL -o "$BUILD/$SPK_asset" "$URL"
    echo "$SPK_sha256  $BUILD/$SPK_asset" | shasum -a 256 -c -
    tar -xf "$BUILD/$SPK_asset" -C "$SPARKLE_DIR"
fi

FW="$APP/Contents/Frameworks/Sparkle.framework"
rm -rf "$FW"
ditto "$SPARKLE_DIR/Sparkle.framework" "$FW"

# Checked daily, like intake/updates.py; installing still asks.
PLIST="$APP/Contents/Info.plist"
for k in SUPublicEDKey SUFeedURL SUEnableAutomaticChecks SUScheduledCheckInterval SUAutomaticallyUpdate; do
    plutil -remove "$k" "$PLIST" 2>/dev/null || true
done
plutil -insert SUPublicEDKey -string "$KEY" "$PLIST"
plutil -insert SUFeedURL -string "$FEED" "$PLIST"
plutil -insert SUEnableAutomaticChecks -bool YES "$PLIST"
plutil -insert SUScheduledCheckInterval -integer 86400 "$PLIST"
plutil -insert SUAutomaticallyUpdate -bool NO "$PLIST"
plutil -lint -s "$PLIST"

# The framework and the plist changed after the bundle was sealed, so seal
# it again.
if [[ -n "${SYLLABUS_CODESIGN_IDENTITY:-}" ]]; then
    "$ROOT/packaging/sign.sh" "$APP" "$SYLLABUS_CODESIGN_IDENTITY" \
        "${SYLLABUS_ENTITLEMENTS:-$ROOT/packaging/entitlements.plist}"
else
    codesign --force --sign - "$APP"
fi
echo "sparkle $SPK_version added to $APP, feed $FEED"
