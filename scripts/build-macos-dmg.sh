#!/usr/bin/env bash
# Build the macOS application bundle and a distributable disk image.
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "macOS packaging requires Darwin tools (hdiutil)." >&2
    exit 1
fi

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
dist_dir="$root_dir/dist"
app_path="$dist_dir/retype.app"
dmg_path="$dist_dir/retype.dmg"
build_dir="$root_dir/build/retype-target-bundle"
staging_dir="$(mktemp -d "${TMPDIR:-/tmp}/retype-dmg.XXXXXX")"

cleanup() {
    rm -rf "$staging_dir"
}
trap cleanup EXIT

rm -rf "$app_path" "$build_dir"
rm -f "$dmg_path"

(
    cd "$root_dir"
    uv run --locked --group build python -m PyInstaller --noconfirm --clean \
        --distpath "$dist_dir" --workpath "$build_dir" \
        setup/retype-target-bundle.spec
)

test -d "$app_path"
test -x "$app_path/Contents/MacOS/retype"
rm -rf "$dist_dir/retypebundle-coll"

# hdiutil is supplied by macOS, so producing the image does not add an
# unpinned Node/npm dependency to the locked Python build environment.
cp -R "$app_path" "$staging_dir/retype.app"
ln -s /Applications "$staging_dir/Applications"
cp "$root_dir/style/icons/retype-dmg.icns" "$staging_dir/.VolumeIcon.icns"
SetFile -a C "$staging_dir"
hdiutil create -volname retype -srcfolder "$staging_dir" -ov -format UDZO \
    "$dmg_path"

test -f "$dmg_path"
echo "Built $app_path and $dmg_path"
