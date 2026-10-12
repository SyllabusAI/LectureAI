#!/bin/bash
# Install the newest Syllabus.app from the GitHub releases, from one line:
#
#     curl -fsSL https://raw.githubusercontent.com/SyllabusAI/LectureAI/main/install.sh | bash
#
# Why this exists: the app is signed ad hoc, not with a Developer ID, so a
# disk image that a browser downloads gets the quarantine flag and Gatekeeper
# blocks the first open. curl does not set that flag, so an app that this
# script copies into place opens on the first try. The script checks the
# disk image against the .sha256 file of the release before it copies it.
#
# Environment, for tests:
#   SYLLABUS_INSTALL_DIR  the folder that gets Syllabus.app (default /Applications,
#                         or ~/Applications if /Applications is not writable)
#   SYLLABUS_VERSION      a tag such as v0.6.1 (default: the latest release)
#   SYLLABUS_NO_OPEN=1    do not open the app after the copy
#
# Everything is inside main(), so a download that stops halfway runs nothing.
set -euo pipefail

REPO="SyllabusAI/LectureAI"

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }
die() { printf 'Syllabus install failed: %s\n' "$*" >&2; exit 1; }

# True if a process runs the executable inside the bundle at $1.
running() {
    local pid
    for pid in $(pgrep -x Syllabus); do
        [[ "$(ps -o comm= -p "$pid")" == "$1/Contents/MacOS/"* ]] && return 0
    done
    return 1
}

main() {
    [[ "$(uname -s)" == "Darwin" ]] || die "Syllabus runs on a Mac only."
    [[ "$(uname -m)" == "arm64" ]] || die "Syllabus needs a Mac with Apple silicon (M1 or newer)."
    local major
    major="$(sw_vers -productVersion | cut -d. -f1)"
    (( major >= 13 )) || die "Syllabus needs macOS 13 or newer. This Mac has $(sw_vers -productVersion)."

    # The tag of the latest release, from the redirect of /releases/latest.
    # This avoids the GitHub API and its limit of 60 requests an hour.
    local tag="${SYLLABUS_VERSION:-}"
    if [[ -z "$tag" ]]; then
        tag="$(curl -fsSIL -o /dev/null -w '%{url_effective}' \
            "https://github.com/$REPO/releases/latest")"
        tag="${tag##*/}"
    fi
    [[ "$tag" == v[0-9]* ]] || die "Could not find the latest release. Try again in a minute."
    local version="${tag#v}"
    local dmg="Syllabus-$version.dmg"
    local base="https://github.com/$REPO/releases/download/$tag"

    # Global, not local: the EXIT trap runs after main returns.
    tmp="$(mktemp -d)"
    mnt="$tmp/mnt"
    # Detach the image and delete the temporary folder on every exit.
    trap 'hdiutil detach -quiet "$mnt" 2>/dev/null || true; rm -rf "$tmp"' EXIT

    say "Downloading Syllabus $version"
    curl -fL --progress-bar -o "$tmp/$dmg" "$base/$dmg" \
        || die "The download of $dmg did not complete."
    curl -fsSL -o "$tmp/$dmg.sha256" "$base/$dmg.sha256" \
        || die "The download of the checksum file did not complete."

    say "Checking the download"
    (cd "$tmp" && shasum -a 256 -c -s "$dmg.sha256") \
        || die "The checksum does not match. Do not use this file. Email syllabus@maincoursemedia.com."

    local dest="${SYLLABUS_INSTALL_DIR:-}"
    if [[ -z "$dest" ]]; then
        if [[ -w /Applications ]]; then
            dest="/Applications"
        else
            dest="$HOME/Applications"
        fi
    fi
    mkdir -p "$dest"

    # A running copy holds files open inside the bundle. Quit that copy, and
    # only that copy, before the copy. Quit asks the app, so a recording in
    # progress gets its normal stop.
    local app="$dest/Syllabus.app"
    if running "$app"; then
        say "Quitting the copy of Syllabus that is running"
        osascript -e "tell application \"$app\" to quit" >/dev/null 2>&1 || true
        local i
        for i in {1..20}; do
            running "$app" || break
            sleep 0.5
        done
        running "$app" && die "Syllabus is still running. Quit it from the menu bar, then run this command again."
    fi

    say "Installing Syllabus in $dest"
    mkdir -p "$mnt"
    hdiutil attach -quiet -nobrowse -readonly -mountpoint "$mnt" "$tmp/$dmg" \
        || die "Could not open $dmg."
    [[ -d "$mnt/Syllabus.app" ]] || die "$dmg does not contain Syllabus.app."
    rm -rf "$app"
    # ditto keeps the symlinks and the code signature of the bundle.
    ditto "$mnt/Syllabus.app" "$app"

    say "Syllabus $version is installed. Your settings, schedule, and recordings stay as they were."
    if [[ -z "${SYLLABUS_NO_OPEN:-}" ]]; then
        open "$app"
        say "Syllabus is opening. Look for its icon in the menu bar."
    fi
}

main "$@"
