# Windows Setup

## Automated setup

Run run-win.ps1 from PowerShell (after cloning this repo and zenvi-core). It installs MSYS2 via winget if missing, builds libopenshot and libopenshot-audio, sets up a Python venv, installs dependencies, and launches Zenvi. First run takes over an hour since it compiles from source; later runs are fast, since it skips the build once it's already done.

If it fails partway through, or you'd rather set things up by hand, use the manual steps below.

## Manual setup

Qt WebEngine (used by Director, plan review, and the chat UI panels) is not available in MSYS2 MinGW64, only Qt WebKit is, so those panels show placeholders unless you set up a separate MSVC Python with PyQtWebEngine via pip. The timeline itself still works fine on WebKit or qwidget mode.

1. Install MSYS2 from msys2.org and open the MinGW x64 shell.

2. Optional: add /mingw64/bin and /mingw64/lib to PATH by appending them to ~/.bashrc, then run source ~/.bashrc.

3. Update packages and install build dependencies: run pacman -Syu, then pacman -S --needed --disable-download-timeout base-devel git mingw-w64-x86_64-toolchain mingw64/mingw-w64-x86_64-ffmpeg mingw64/mingw-w64-x86_64-swig mingw64/mingw-w64-x86_64-cmake mingw64/mingw-w64-x86_64-doxygen mingw64/mingw-w64-x86_64-zeromq mingw64/mingw-w64-x86_64-python-pyqt5 mingw64/mingw-w64-x86_64-python-pip mingw64/mingw-w64-x86_64-python-pyzmq mingw64/mingw-w64-x86_64-rust, then pip3 install httplib2 tinys3 github3.py==0.9.6 requests --break-system-packages.

4. Only needed for ASIO hardware drivers, or if CMake can't find Windows/DirectX paths (CI doesn't need this): install the Windows SDK and set DXSDK_DIR to its path, then install the ASIO SDK and set ASIO_SDK_DIR to its path.

5. Build unittest-cpp into MSYS's /usr: clone unittest-cpp/unittest-cpp, cd into its builds folder, run cmake -G "MSYS Makefiles" -DCMAKE_MAKE_PROGRAM=mingw32-make -DCMAKE_INSTALL_PREFIX:PATH=/usr -DCMAKE_POLICY_VERSION_MINIMUM=3.5 .., then make, then make install, then set UNITTEST_DIR=C:\msys64\usr.

6. Build libopenshot-audio the same way, installing to /usr so its libraries land in C:/msys64/usr/bin, then set LIBOPENSHOT_AUDIO_DIR=C:\msys64\usr.

7. Install extra Qt and ZMQ packages for libopenshot: pacman -S mingw64/mingw-w64-x86_64-qt5-svg mingw64/mingw-w64-x86_64-cppzmq.

8. Build libopenshot into MinGW's /mingw64: clone OpenShot/libopenshot, cd into build, run cmake -G "MSYS Makefiles" -DCMAKE_MAKE_PROGRAM=mingw32-make -DCMAKE_INSTALL_PREFIX:PATH=/mingw64 -DDISABLE_TESTS=1 -DCMAKE_CXX_FLAGS="-include cstdint" .., then make, then make install.

9. Only needed for freeze.py or building installers: pacman -S --needed mingw64/mingw-w64-x86_64-python-cx-freeze mingw64/mingw-w64-x86_64-python-lief.

10. Clone zenvi-core, install PyQt5, cffi, zstandard, and Qt WebKit via pacman (mingw-w64-x86_64-python-pyqt5 mingw-w64-x86_64-python-cffi mingw-w64-x86_64-python-zstandard mingw-w64-x86_64-qtwebkit mingw-w64-x86_64-libffi mingw-w64-x86_64-gcc), then create a venv with /mingw64/bin/python.exe -m venv --system-site-packages .venv, activate it, and run pip install -r requirements-noqt.txt (add requirements-manim.txt too if you need it).

11. Run Zenvi against your build tree bindings: PYTHONPATH_LIBOPENSHOT=~/libopenshot/build/bindings/python bash run-zenvi-core.sh.

## Known issues

Fresh MSYS2 installs: pacman -Syu can close the window mid-run before step 1 finishes, just re-run it.

pip can fail compiling cryptography and rpds-py from requirements-noqt.txt; install mingw-w64-x86_64-python-cryptography and mingw-w64-x86_64-python-rpds-py via pacman instead.

On an existing MSYS2 install, pacman -Syu does a full system upgrade that can break an existing libopenshot build; install only the needed packages instead.

MSYS2's default path, C:\msys64, is hardcoded in a few places, so adjust manually if yours differs.

make install silently overwrites an existing libopenshot or libopenshot-audio install with no warning.
