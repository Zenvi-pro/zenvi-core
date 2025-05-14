#!/bin/bash
set -x    # trace every command

# 1) Print out the incoming (host) environment
echo "=== Launch Debug: START ===" >&2
echo "HOST LD_LIBRARY_PATH = '$LD_LIBRARY_PATH'" >&2
echo "HOST    PATH           = '$PATH'" >&2
echo "All APPIMAGE_ vars:" >&2
env | grep '^APPIMAGE_' | sed 's/^/  /' >&2

# 2) Save the host env for later subprocess restores
export HOST_LD_LIBRARY_PATH="$LD_LIBRARY_PATH"
export HOST_PATH="$PATH"

# 3) Now apply the AppImage’s isolation overrides
HERE=$(dirname "$(realpath "$0")")
export LD_LIBRARY_PATH="${HERE}"
export QT_PLUGIN_PATH="${HERE}"
export OPENSSL_CONF="/dev/null"

# 4) Ensure snap stub dir is still on the AppImage’s PATH
if [ -d /snap/bin ]; then
    case ":$PATH:" in
        *":/snap/bin:"*) ;;
        *) PATH="/snap/bin:$PATH";;
    esac
fi

echo "=== Launch Debug: AFTER overrides ===" >&2
echo "  LD_LIBRARY_PATH = '$LD_LIBRARY_PATH'" >&2
echo "  PATH            = '$PATH'" >&2
echo "==============================" >&2

# 5) Hand off to the real OpenShot Qt binary
exec "${HERE}/openshot-qt" "$@"
