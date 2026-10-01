#!/bin/zsh
# Build unfinder.app (and optionally install it).
#
#   ./build.sh            → dist/unfinder.app
#   ./build.sh --install  → also copies it to /Applications (replacing an older copy)
set -euo pipefail
cd "$(dirname "$0")"

echo "→ Building the icon"
uv run --quiet make_icon.py

echo "→ Building unfinder.app (takes a minute)"
uv run --quiet --python 3.12 --with "PySide6>=6.6" --with "pyinstaller>=6.10" \
    pyinstaller --noconfirm --clean --log-level WARN unfinder.spec

echo "→ Built dist/unfinder.app ($(du -sh dist/unfinder.app | cut -f1))"

if [[ "${1:-}" == "--install" ]]; then
    if pgrep -xq unfinder; then
        echo "→ Quitting the running unfinder"
        # Qt answers the quit request with a harmless "User cancelled" error, so hide it.
        osascript -e 'tell application "unfinder" to quit' >/dev/null 2>&1 || true
        for _ in {1..20}; do pgrep -xq unfinder || break; sleep 0.25; done
        pgrep -xq unfinder && pkill -x unfinder || true
    fi
    echo "→ Installing to /Applications"
    rm -rf /Applications/unfinder.app
    ditto dist/unfinder.app /Applications/unfinder.app
    # Make Finder/Launchpad/Spotlight pick up the new icon right away
    /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f /Applications/unfinder.app
    touch /Applications/unfinder.app
    echo "✓ Installed /Applications/unfinder.app"
fi
