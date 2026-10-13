#!/bin/zsh
# Put the ffmpeg and ffprobe that Syllabus.app bundles into a folder.
#
#     packaging/ffmpeg/fetch.sh <dir>
#
# The release pinned in packaging/ffmpeg/release, downloaded and checked
# against its sha256: the binary people actually get. packaging/build.sh and
# the release workflow's test gate both use this.
#
# A pin with no sha256 yet (a new build recorded but not published) fails,
# unless SYLLABUS_FFMPEG_BUILD_UNPINNED=1, which builds this Mac's
# architecture from source with packaging/ffmpeg/build.sh instead. The release
# workflow sets that only for runs that publish nothing, so a branch can be
# checked before its ffmpeg is published, and a tag never ships an unpinned one.
set -euo pipefail

HERE="${0:A:h}"
DIR="${1:?usage: fetch.sh <dir>}"
mkdir -p "$DIR"
DIR="${DIR:A}"

eval "$(sed 's/^/PIN_/' "$HERE/release")"
if [[ -z "${PIN_sha256:-}" ]]; then
    if [[ "${SYLLABUS_FFMPEG_BUILD_UNPINNED:-}" == 1 ]]; then
        echo "fetch.sh: $PIN_tag is not published yet; building this Mac's ffmpeg from source" >&2
        "$HERE/build.sh" "$DIR"
        exit 0
    fi
    echo "fetch.sh: no ffmpeg build to bundle." >&2
    echo "  packaging/ffmpeg/release has no sha256 yet: publish one with the" >&2
    echo "  'ffmpeg for Syllabus.app' workflow and record it there, or build" >&2
    echo "  locally with packaging/ffmpeg/build.sh, or set SYLLABUS_FFMPEG_DIR." >&2
    exit 1
fi

URL="https://github.com/SyllabusAI/LectureAI/releases/download/$PIN_tag/$PIN_asset"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
echo "fetching $URL"
curl -sfL -o "$TMP/$PIN_asset" "$URL"
echo "$PIN_sha256  $TMP/$PIN_asset" | shasum -a 256 -c -
tar -xzf "$TMP/$PIN_asset" -C "$DIR"
