#!/bin/bash
# Compile Resources/AppIcon.icon into Resources/AppIcon.icns and Resources/Assets.car.
set -euo pipefail
umask 022
export PATH='/usr/bin:/bin:/usr/sbin:/sbin'
export COPYFILE_DISABLE=1

readonly PROJECT_DIR="$(cd -P "$(dirname "$0")/.." && pwd -P)"
readonly RESOURCES_DIR="$PROJECT_DIR/Resources"
readonly ICON_SOURCE="$RESOURCES_DIR/AppIcon.icon"

die() { printf 'build-icon: %s\n' "$*" >&2; exit 1; }

[[ "$(uname -s)" == 'Darwin' ]] || die 'macOS is required'
[[ -d "$ICON_SOURCE" && ! -L "$ICON_SOURCE" ]] || die "missing icon package: $ICON_SOURCE"
for rel in icon.json Assets/base.svg Assets/arc.svg Assets/wave.svg; do
    f="$ICON_SOURCE/$rel"
    [[ -f "$f" && ! -L "$f" && -r "$f" ]] || die "missing or unsafe icon input: $f"
done

actool_bin="$(/usr/bin/xcrun --find actool 2>/dev/null || true)"
[[ -n "$actool_bin" && -x "$actool_bin" ]] || die 'actool is unavailable (requires Xcode)'

scratch="$(/usr/bin/mktemp -d /private/tmp/clash-icon-build.XXXXXXXX)"
cleanup() {
    local status=$?
    trap - EXIT
    if [[ -n "${scratch:-}" && -d "$scratch" && ! -L "$scratch" && -O "$scratch" ]]; then
        /bin/rm -rf -- "$scratch"
    fi
    exit "$status"
}
trap cleanup EXIT

/bin/mkdir -p "$scratch/in/AppIcon.icon/Assets" "$scratch/out"
/bin/cp -X "$ICON_SOURCE/icon.json" "$scratch/in/AppIcon.icon/icon.json"
for svg in base.svg arc.svg wave.svg; do
    /bin/cp -X "$ICON_SOURCE/Assets/$svg" "$scratch/in/AppIcon.icon/Assets/$svg"
done

"$actool_bin" --compile "$scratch/out" \
    --platform macosx --minimum-deployment-target 12.0 \
    --app-icon AppIcon --standalone-icon-behavior all \
    --output-partial-info-plist "$scratch/partial.plist" \
    --errors --warnings \
    "$scratch/in/AppIcon.icon" > "$scratch/actool.plist"

[[ -s "$scratch/out/AppIcon.icns" && -s "$scratch/out/Assets.car" ]] || die 'actool did not produce AppIcon.icns and Assets.car'
/usr/bin/iconutil -c iconset -o "$scratch/verify.iconset" "$scratch/out/AppIcon.icns" || die 'generated AppIcon.icns failed iconutil verification'
[[ -s "$scratch/verify.iconset/icon_512x512@2x.png" && -s "$scratch/verify.iconset/icon_16x16.png" ]] || die 'generated AppIcon.icns is missing expected sizes'

/bin/cp -X "$scratch/out/AppIcon.icns" "$RESOURCES_DIR/AppIcon.icns"
/bin/cp -X "$scratch/out/Assets.car" "$RESOURCES_DIR/Assets.car"
/bin/chmod 644 "$RESOURCES_DIR/AppIcon.icns" "$RESOURCES_DIR/Assets.car"
printf 'Built icon resources:\n  %s\n  %s\n' "$RESOURCES_DIR/AppIcon.icns" "$RESOURCES_DIR/Assets.car"
