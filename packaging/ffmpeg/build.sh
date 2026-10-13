#!/bin/zsh
# Build the ffmpeg and ffprobe that ship inside Syllabus.app.
#
#     packaging/ffmpeg/build.sh [output-dir] [arm64|x86_64]
#
# The architecture defaults to this Mac's. Naming the other one cross-compiles
# with the same clang (an x86_64 build on Apple silicon needs Rosetta for the
# checks at the end, which run the result). Syllabus.app carries both: the
# workflow builds each on its own runner and packaging/ffmpeg/universal.sh
# joins them with lipo.
#
# A static build of a pinned ffmpeg release (see SOURCE below), configured
# with only what the pipeline uses: the avfoundation microphone input, the
# native AAC encoder, the m4a/mp4 and segment muxers, and decoders for the
# audio people drop into the inbox. GPL and nonfree parts are disabled, so
# the result is LGPL 2.1 and can travel inside the app with its license text.
# No third-party libraries: only macOS frameworks are linked, dynamically.
#
# Runs on any Mac with the Command Line Tools (clang, make); no Homebrew.
# The workflow (.github/workflows/ffmpeg.yml) runs this on an Apple silicon
# runner and on an Intel one, joins the two with universal.sh, and publishes
# the tarball, which packaging/build.sh downloads.
set -euo pipefail

HERE="${0:A:h}"
VERSION="$(tr -d '[:space:]' < "$HERE/VERSION")"
SHA256="$(tr -d '[:space:]' < "$HERE/SHA256")"
SOURCE="https://ffmpeg.org/releases/ffmpeg-${VERSION}.tar.xz"
OUT="${1:-$HERE/../build/ffmpeg}"
OUT="${OUT:A}"
WORK="${TMPDIR:-/tmp}/syllabus-ffmpeg-build"
HOST="$(uname -m)"
ARCH="${2:-$HOST}"
case "$ARCH" in
    arm64) FF_ARCH=aarch64 ;;
    x86_64) FF_ARCH=x86_64 ;;
    *) echo "build.sh: unknown architecture $ARCH (arm64 or x86_64)" >&2; exit 1 ;;
esac
# The macOS the app supports (LSMinimumSystemVersion in syllabus.spec), so
# neither slice uses an API that is newer.
MIN_MACOS=13.0
ARCH_FLAGS=(--arch="$FF_ARCH" --cc="clang -arch $ARCH" --cxx="clang++ -arch $ARCH")
[[ "$ARCH" != "$HOST" ]] && ARCH_FLAGS+=(--enable-cross-compile --target-os=darwin)
# Intel only: ffmpeg's hand-written x86 assembly needs nasm, which neither
# runner image nor the Command Line Tools ship. The C versions of the same
# routines are used instead; for mono speech at 64k that costs nothing a
# person would notice. Apple silicon's NEON assembly is built in by clang.
[[ "$ARCH" == "x86_64" ]] && ARCH_FLAGS+=(--disable-x86asm)

mkdir -p "$WORK" "$OUT"
cd "$WORK"

if [[ ! -f "ffmpeg-${VERSION}.tar.xz" ]]; then
    echo "fetching $SOURCE"
    curl -sfL -o "ffmpeg-${VERSION}.tar.xz" "$SOURCE"
fi
echo "$SHA256  ffmpeg-${VERSION}.tar.xz" | shasum -a 256 -c -
rm -rf "ffmpeg-${VERSION}"
tar xf "ffmpeg-${VERSION}.tar.xz"
cd "ffmpeg-${VERSION}"

# Everything off, then exactly what record.py and transcribe.py ask for.
# AVFoundation is one of the libraries --disable-autodetect switches off,
# and an indev whose framework is off is silently dropped, so it is turned
# back on by name. The other frameworks it needs are checked regardless.
#   avfoundation        the microphone (record.py)
#   aac encoder         mono 64k m4a for recording and for Whisper (both)
#   ipod/mp4/segment    .m4a output and splitting long lectures (transcribe.py)
#   decoders            whatever arrives in inbox/: m4a, mp3, wav, aiff, flac, ogg
#   aresample/aformat   -ac 1 -ar 16000
./configure \
    --prefix="$PWD/out" \
    "${ARCH_FLAGS[@]}" \
    --disable-everything --disable-gpl --disable-nonfree --disable-version3 \
    --disable-shared --enable-static --disable-doc --disable-debug \
    --disable-network --disable-autodetect --disable-ffplay \
    --disable-programs --enable-ffmpeg --enable-ffprobe \
    --enable-indev=avfoundation \
    --enable-avfoundation \
    --enable-protocol=file,pipe \
    --enable-demuxer=mov,mp3,wav,aac,flac,ogg,aiff,caf,matroska,pcm_s16le \
    --enable-muxer=ipod,mp4,mov,segment,wav,null \
    --enable-encoder=aac,pcm_s16le \
    --enable-decoder=aac,aac_latm,mp3,mp3float,alac,flac,vorbis,opus,pcm_s16le,pcm_s16be,pcm_s24le,pcm_s32le,pcm_f32le,pcm_u8 \
    --enable-parser=aac,aac_latm,mpegaudio,flac,vorbis,opus \
    --enable-bsf=aac_adtstoasc \
    --enable-filter=aresample,aformat,anull,atrim,asetpts,volume,acopy,amix,pan,channelmap \
    --extra-cflags="-O2 -mmacosx-version-min=$MIN_MACOS" \
    --extra-ldflags="-Wl,-dead_strip -mmacosx-version-min=$MIN_MACOS"

make -j"$(sysctl -n hw.ncpu)" >/dev/null
make install >/dev/null

cp out/bin/ffmpeg out/bin/ffprobe "$OUT/"
strip "$OUT/ffmpeg" "$OUT/ffprobe"
cp LICENSE.md "$OUT/LICENSE.md"
cp COPYING.LGPLv2.1 "$OUT/COPYING.LGPLv2.1"
{
    echo "ffmpeg $VERSION for $ARCH, built $(date -u +%Y-%m-%dT%H:%MZ) on $HOST by packaging/ffmpeg/build.sh"
    echo "source: $SOURCE (sha256 $SHA256)"
    echo "license: LGPL 2.1 or later; see COPYING.LGPLv2.1 and LICENSE.md"
    echo "configure:"
    "$OUT/ffmpeg" -hide_banner -buildconf | sed 's/^/  /'
} > "$OUT/BUILD.txt"

echo
echo "built into $OUT"
ls -la "$OUT" | awk 'NR>1{print "  " $5 "\t" $9}'
"$OUT/ffmpeg" -version | head -1
for f in ffmpeg ffprobe; do
    [[ "$(lipo -archs "$OUT/$f")" == "$ARCH" ]] || { echo "$f is $(lipo -archs "$OUT/$f"), not $ARCH"; exit 1; }
done
"$OUT/ffmpeg" -version | grep -q "enable-gpl" && { echo "GPL crept in"; exit 1; }
"$OUT/ffmpeg" -hide_banner -devices 2>/dev/null | grep -q "avfoundation" \
    || { echo "the avfoundation microphone input is missing"; exit 1; }
"$OUT/ffmpeg" -hide_banner -encoders 2>/dev/null | grep -q " aac " \
    || { echo "the aac encoder is missing"; exit 1; }
# otool indents each library with a tab; anything not under /System or
# /usr/lib would be a dependency the app cannot carry.
if otool -L "$OUT/ffmpeg" "$OUT/ffprobe" | grep $'^\t' | grep -Ev $'^\t(/System/|/usr/lib/)'; then
    echo "links outside the system"; exit 1
fi
echo "static apart from macOS frameworks; microphone input and aac encoder present"
