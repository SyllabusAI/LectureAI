#!/bin/zsh
# Join an arm64 and an x86_64 build of ffmpeg into one universal pair.
#
#     packaging/ffmpeg/universal.sh <arm64-dir> <x86_64-dir> <output-dir>
#
# Each input is what packaging/ffmpeg/build.sh wrote for one architecture.
# The output holds ffmpeg and ffprobe with both slices (lipo), the license
# texts, and a BUILD.txt that carries both builds' notes. macOS runs the
# slice that matches the Mac, so Syllabus.app needs only the one pair.
set -euo pipefail

ARM="${1:?usage: universal.sh <arm64-dir> <x86_64-dir> <output-dir>}"
X86="${2:?usage: universal.sh <arm64-dir> <x86_64-dir> <output-dir>}"
OUT="${3:?usage: universal.sh <arm64-dir> <x86_64-dir> <output-dir>}"
mkdir -p "$OUT"

for f in ffmpeg ffprobe; do
    [[ "$(lipo -archs "$ARM/$f")" == "arm64" ]] || { echo "$ARM/$f is not arm64" >&2; exit 1; }
    [[ "$(lipo -archs "$X86/$f")" == "x86_64" ]] || { echo "$X86/$f is not x86_64" >&2; exit 1; }
    lipo -create "$ARM/$f" "$X86/$f" -output "$OUT/$f"
    lipo "$OUT/$f" -verify_arch arm64 x86_64
done
# The license texts come from the same source tarball on both sides.
for f in LICENSE.md COPYING.LGPLv2.1; do
    cmp -s "$ARM/$f" "$X86/$f" || { echo "$f differs between the two builds" >&2; exit 1; }
    cp "$ARM/$f" "$OUT/$f"
done
{
    echo "Universal: arm64 and x86_64 slices joined by packaging/ffmpeg/universal.sh."
    echo
    cat "$ARM/BUILD.txt"
    echo
    cat "$X86/BUILD.txt"
} > "$OUT/BUILD.txt"

echo "joined into $OUT"
lipo -info "$OUT/ffmpeg" "$OUT/ffprobe"
# This Mac's slice, as a check that lipo kept it whole.
"$OUT/ffmpeg" -hide_banner -version | head -1
"$OUT/ffprobe" -hide_banner -version | head -1
