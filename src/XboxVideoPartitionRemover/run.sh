#!/bin/sh
# XBOX Video Partition Remover - Linux launcher.
# Needs Python 3 with Tkinter (python3-tk on Debian/Ubuntu).
exec python3 "$(dirname "$0")/app/main.py" "$@"
