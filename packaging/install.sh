#!/bin/sh
set -eu
windows_scroll_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if ! command -v python3 >/dev/null 2>&1; then
    printf '%s\n' 'Windows Scroll Linux needs Python 3. Install your distribution python3 package, then run this installer again.' >&2
    exit 1
fi
exec python3 -I "$windows_scroll_dir/install.py" "$@"
