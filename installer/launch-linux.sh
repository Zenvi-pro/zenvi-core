#!/bin/bash

# Locate this script
HERE=$(dirname "$(realpath "$0")")

# Ensure our local libs and plugins load first
export LD_LIBRARY_PATH="${HERE}"
export QT_PLUGIN_PATH="${HERE}"

# Workaround for newer OpenSSL on Debian/Ubuntu
export OPENSSL_CONF="/dev/null"

# Add /snap/bin to PATH if it exists and isn’t already there,
# so the user can simply set “/snap/bin/blender” or rely on
# “blender” pointing to the snap shim.
if [ -d /snap/bin ]; then
    case ":$PATH:" in
        *":/snap/bin:"*) ;;
        *) export PATH="/snap/bin:$PATH" ;;
    esac
fi

# Finally, launch OpenShot
exec "${HERE}/openshot-qt" "$@"
