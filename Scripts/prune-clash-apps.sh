#!/bin/bash
# Keep the installed apps; prune recognized generated bundles and registrations.
set -euo pipefail
export PATH='/usr/bin:/bin:/usr/sbin:/sbin'
readonly SCRIPT_DIR="$(cd -P "$(dirname "$0")" && pwd -P)"
exec /usr/bin/python3 -I "$SCRIPT_DIR/prune-clash-apps.py" "$@"
