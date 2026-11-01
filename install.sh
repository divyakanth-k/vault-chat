#!/bin/sh
# Bootstrap for Linux and macOS. Finds a Python and hands over to install.py,
# which holds all the real logic so the three platforms cannot drift apart.
set -e

dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

for py in python3 python; do
    if command -v "$py" >/dev/null 2>&1; then
        exec "$py" "$dir/install.py" "$@"
    fi
done

echo "Python 3.9 or newer is required but was not found on PATH." >&2
echo "  Debian/Ubuntu:  sudo apt install python3 python3-venv" >&2
echo "  Fedora:         sudo dnf install python3" >&2
echo "  macOS:          brew install python" >&2
exit 1
