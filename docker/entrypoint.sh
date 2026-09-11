#!/bin/sh
set -eu
# Each replica owns its own terminal and Wine state. The ledger is external.
if [ ! -f "$WINEPREFIX/drive_c/mt5/terminal64.exe" ]; then
    mkdir -p "$WINEPREFIX/drive_c/mt5"
    cp -a /opt/mt5-template/. "$WINEPREFIX/drive_c/mt5/"
fi
exec xvfb-run -a -s '-screen 0 1280x800x24' wine 'C:\Python312\python.exe' -m signal_capture
