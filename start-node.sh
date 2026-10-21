#!/usr/bin/env sh
set -u

node_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1
node_python=''
for node_candidate in "$node_root/.venv/bin/python" "$node_root/.venv/Scripts/python.exe" python3 python; do
    if ! command -v "$node_candidate" >/dev/null 2>&1; then
        continue
    fi
    if "$node_candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
        node_python=$node_candidate
        break
    fi
done
if [ -z "$node_python" ]; then
    printf '%s\n' 'Python 3.11 or newer is required. Install Python and its venv/pip support with your operating system package manager, then run ./start-node.sh again.' >&2
    exit 1
fi
if [ ! -f "$node_root/scripts/node_bootstrap.py" ]; then
    printf '%s\n' 'scripts/node_bootstrap.py is missing. Restore the complete project folder and try again.' >&2
    exit 1
fi
exec "$node_python" "$node_root/scripts/node_bootstrap.py" "$@"
