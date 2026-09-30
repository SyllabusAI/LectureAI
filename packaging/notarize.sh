#!/bin/zsh
# Notarize a signed Syllabus.app or disk image with Apple, then staple it.
#
#     packaging/notarize.sh dist/Syllabus.app
#     packaging/notarize.sh dist/Syllabus-X.Y.Z.dmg
#
# Authenticates with an App Store Connect API key (docs/signing.md), from the
# environment: ASC_API_KEY_PATH (the AuthKey_XXXX.p8 file), ASC_API_KEY_ID,
# ASC_API_ISSUER_ID. An app is zipped for the upload (notarytool takes a zip,
# a dmg, or a pkg) and the ticket is stapled to the app itself. On a rejection
# Apple's log, which names each file it objected to, is printed and the
# script fails.
set -euo pipefail

TARGET="${1:?usage: notarize.sh <Syllabus.app | .dmg>}"
TARGET="${TARGET:A}"
: "${ASC_API_KEY_PATH:?}" "${ASC_API_KEY_ID:?}" "${ASC_API_ISSUER_ID:?}"
auth=(--key "$ASC_API_KEY_PATH" --key-id "$ASC_API_KEY_ID" --issuer "$ASC_API_ISSUER_ID")

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
UPLOAD="$TARGET"
if [[ -d "$TARGET" ]]; then
    UPLOAD="$WORK/${TARGET:t:r}.zip"
    ditto -c -k --keepParent "$TARGET" "$UPLOAD"
fi

echo "notarizing ${TARGET:t}"
# notarytool's exit status does not say whether Apple accepted it; the JSON does.
xcrun notarytool submit "$UPLOAD" "${auth[@]}" --wait --timeout 30m \
    --output-format json > "$WORK/result.json" || true
cat "$WORK/result.json"; echo
field() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))' "$WORK/result.json" "$1" 2>/dev/null || true; }
STATUS="$(field status)"
if [[ "$STATUS" != "Accepted" ]]; then
    ID="$(field id)"
    [[ -n "$ID" ]] && xcrun notarytool log "$ID" "${auth[@]}" || true
    echo "notarize.sh: ${TARGET:t} was not accepted (status: ${STATUS:-none})" >&2
    exit 1
fi

xcrun stapler staple "$TARGET"
xcrun stapler validate "$TARGET"
