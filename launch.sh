#!/bin/bash
# Detect if running under a bare WM (no compositor / no Wayland compositor)
if [ -z "$WAYLAND_DISPLAY" ] && [ -z "$SWAYSOCK" ] && [ -z "$KDE_FULL_SESSION" ] && [ -z "$GNOME_DESKTOP_SESSION_ID" ]; then
    export WEBKIT_DISABLE_DMABUF_RENDERER=1
fi

exec python main.py "$@"
