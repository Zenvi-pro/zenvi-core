#!/bin/bash
# Zenvi Core Launcher - Completely clears snap environment to avoid library conflicts

# Save the script directory
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Get venv site-packages if venv is active
VENV_SITE=""
if [[ ! -z "$VIRTUAL_ENV" ]]; then
  VENV_SITE="$VIRTUAL_ENV/lib/python3.12/site-packages"
fi

# Build PYTHONPATH with venv site-packages and libopenshot
PYTHONPATH_VALUE="$VENV_SITE"
if [[ ! -z "$PYTHONPATH_LIBOPENSHOT" ]]; then
  PYTHONPATH_VALUE="${PYTHONPATH_LIBOPENSHOT}:$PYTHONPATH_VALUE"
fi

echo "Using PYTHONPATH: $PYTHONPATH_VALUE"

# Start with a completely clean environment
exec env -i \
  HOME="$HOME" \
  USER="$USER" \
  LOGNAME="$LOGNAME" \
  PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
  SHELL="/bin/bash" \
  TERM="$TERM" \
  DISPLAY="$DISPLAY" \
  DBUS_SESSION_BUS_ADDRESS="$DBUS_SESSION_BUS_ADDRESS" \
  XDG_RUNTIME_DIR="$XDG_RUNTIME_DIR" \
  LD_LIBRARY_PATH="/usr/lib/x86_64-linux-gnu:/usr/lib:/lib/x86_64-linux-gnu:/lib" \
  PYTHONPATH="$PYTHONPATH_VALUE" \
  python3 "$SCRIPT_DIR/src/launch.py" "$@"
