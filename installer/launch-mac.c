/*
 * launch-mac.c — Mach-O launcher for the Zenvi macOS .app bundle.
 *
 * Why this exists:
 *   macOS requires the bundle's CFBundleExecutable to be a Mach-O binary.
 *   A shell script cannot be the main executable of a Developer ID-signed,
 *   hardened-runtime, notarized .app — codesign and notarytool both reject
 *   it. This file is the Mach-O equivalent of installer/launch-mac (the
 *   bash script): it sets the same environment variables, then execs the
 *   cx_Freeze-built `zenvi` binary next to itself.
 *
 *   The shell script is kept around for Linux builds and local dev. Only
 *   macOS CI swaps in the compiled version of this file.
 *
 * Build: clang -o launch-mac installer/launch-mac.c
 */

#include <errno.h>
#include <libgen.h>
#include <mach-o/dyld.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static int set_env_path(const char *key, const char *fmt, const char *base) {
    char buf[8192];
    if ((size_t)snprintf(buf, sizeof(buf), fmt, base) >= sizeof(buf)) {
        return -1;
    }
    return setenv(key, buf, 1);
}

int main(int argc, char *argv[]) {
    /* Resolve our own location so paths are relative to Contents/MacOS/. */
    char exe_path[4096];
    uint32_t size = sizeof(exe_path);
    if (_NSGetExecutablePath(exe_path, &size) != 0) {
        fprintf(stderr, "launch-mac: _NSGetExecutablePath buffer too small\n");
        return 1;
    }

    char real_path[4096];
    if (realpath(exe_path, real_path) == NULL) {
        fprintf(stderr, "launch-mac: realpath failed: %s\n", strerror(errno));
        return 1;
    }

    /* dirname may modify its input on some platforms; safe here because
     * we don't reuse real_path afterwards. */
    char *curr_dir = dirname(real_path);

    /* Mirror the shell script's environment setup. */
    setenv("DYLD_LIBRARY_PATH", curr_dir, 1);
    set_env_path("MAGICK_CONFIGURE_PATH",   "%s/ImageMagick/etc/configuration", curr_dir);
    set_env_path("MAGICK_CODER_MODULE_PATH","%s/ImageMagick/modules-Q16/coders", curr_dir);
    set_env_path("QT_PLUGIN_PATH",          "%s/plugins", curr_dir);

    /* QtWebEngine helper: cx_Freeze places it inside an .app sub-bundle.
     * Fall back to a flat layout if that isn't present. */
    char qwe_nested[4096];
    char qwe_flat[4096];
    snprintf(qwe_nested, sizeof(qwe_nested),
             "%s/share/QtWebEngineProcess.app/Contents/MacOS/QtWebEngineProcess",
             curr_dir);
    snprintf(qwe_flat, sizeof(qwe_flat), "%s/QtWebEngineProcess", curr_dir);
    if (access(qwe_nested, F_OK) == 0) {
        setenv("QTWEBENGINEPROCESS_PATH", qwe_nested, 1);
    } else if (access(qwe_flat, F_OK) == 0) {
        setenv("QTWEBENGINEPROCESS_PATH", qwe_flat, 1);
    }

    setenv("QTWEBENGINE_DISABLE_SANDBOX", "1", 1);
    setenv("QT_MAC_WANTS_LAYER", "1", 1);

    /* exec the real frozen binary, keeping argv intact (minus argv[0]
     * which we replace so the new process knows its own name). */
    char zenvi_path[4096];
    snprintf(zenvi_path, sizeof(zenvi_path), "%s/zenvi", curr_dir);

    if (access(zenvi_path, X_OK) != 0) {
        fprintf(stderr, "launch-mac: %s not found or not executable\n", zenvi_path);
        return 1;
    }

    argv[0] = zenvi_path;
    execv(zenvi_path, argv);

    /* execv only returns on failure. */
    fprintf(stderr, "launch-mac: execv %s failed: %s\n", zenvi_path, strerror(errno));
    return 1;
}
