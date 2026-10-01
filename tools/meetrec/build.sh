#!/usr/bin/env bash
# Build MeetRec.app (ad-hoc signed) and install it to ~/Applications.
# Each rebuild changes the signature, so macOS asks for audio permission again.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
dest="${MEETREC_INSTALL_DIR:-$HOME/Applications}"
uv_bin="$(command -v uv)" || { echo "uv not found on PATH" >&2; exit 1; }
if pgrep -x MeetRec >/dev/null; then
    echo "MeetRec is running; quit it from the menu bar before rebuilding" >&2
    exit 1
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
app="$work/MeetRec.app"
mkdir -p "$app/Contents/MacOS"
cp "$here/Info.plist" "$app/Contents/Info.plist"
plutil -insert MeetRecUV -string "$uv_bin" "$app/Contents/Info.plist"
plutil -insert MeetRecScript -string "$here/transcribe.py" "$app/Contents/Info.plist"
# Transcripts land in the vault inbox for later triage; audio stays outside the vault.
inbox="${MEETREC_TRANSCRIPT_DIR:-}"
if [ -z "$inbox" ] && [ -n "${OV:-}" ]; then
    inbox="$(cd "$here/../../scripts" && python3 -c 'import _paths; print(_paths.tier("inbox") / "meetings")')"
fi
if [ -n "$inbox" ]; then
    plutil -insert MeetRecTranscriptDir -string "$inbox" "$app/Contents/Info.plist"
fi
echo "transcripts: ${inbox:-beside the audio (no \$OV)}"
swiftc -swift-version 5 -O "$here/MeetRec.swift" -o "$app/Contents/MacOS/MeetRec"
codesign --force --sign - "$app"

mkdir -p "$dest"
rm -rf "$dest/MeetRec.app"
ditto "$app" "$dest/MeetRec.app"
echo "installed $dest/MeetRec.app"
