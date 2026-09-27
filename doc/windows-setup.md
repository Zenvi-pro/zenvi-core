# Windows Setup

## Automated setup

Run the following from PowerShell, after cloning this repo and zenvi-core:

```sh
./run-win.ps1
```

It installs MSYS2 via winget if missing, builds libopenshot and libopenshot-audio, sets up a Python venv, installs dependencies, and launches Zenvi. First run takes over an hour since it compiles from source; later runs are fast, since it skips the build once it's already done.

If it fails partway through, or you'd rather set things up by hand, use the manual steps below.

## Manual setup

Qt WebEngine (used by Director, plan review, and the chat UI panels) is not available in MSYS2 MinGW64, only Qt WebKit is, so those panels show placeholders unless you set up a separate MSVC Python with PyQtWebEngine via pip. The timeline itself still works fine on WebKit or qwidget mode.

1. Install [MSYS2](https://www.msys2.org/) and open the MinGW x64 shell.

2. Optional: persist the PATH:

    ```sh
    echo 'PATH=$PATH:/c/msys64/mingw64/bin:/c/msys64/mingw64/lib' >> ~/.bashrc
    source ~/.bashrc
    ```

3. Update packages and install build dependencies:

    ```sh
    pacman -Syu

    pacman -S --needed --disable-download-timeout \
      base-devel git \
      mingw-w64-x86_64-toolchain \
      mingw64/mingw-w64-x86_64-ffmpeg \
      mingw64/mingw-w64-x86_64-swig \
      mingw64/mingw-w64-x86_64-cmake \
      mingw64/mingw-w64-x86_64-doxygen \
      mingw64/mingw-w64-x86_64-zeromq \
      mingw64/mingw-w64-x86_64-python-pyqt5 \
      mingw64/mingw-w64-x86_64-python-pip \
      mingw64/mingw-w64-x86_64-python-pyzmq \
      mingw64/mingw-w64-x86_64-rust

    pip3 install httplib2 tinys3 github3.py==0.9.6 requests --break-system-packages
    ```

4. Only needed for ASIO hardware drivers, or if CMake can't find Windows/DirectX paths (CI doesn't need this):

    ```sh
    export DXSDK_DIR="C:\Program Files (x86)\Windows Kits\10"
    export ASIO_SDK_DIR="C:\Program Files\ASIOSDK\common"
    ```

5. Build unittest-cpp into MSYS's `/usr`:

    ```sh
    git clone https://github.com/unittest-cpp/unittest-cpp.git
    cd unittest-cpp/builds
    cmake -G "MSYS Makefiles" -DCMAKE_MAKE_PROGRAM=mingw32-make -DCMAKE_INSTALL_PREFIX:PATH=/usr -DCMAKE_POLICY_VERSION_MINIMUM=3.5 ../
    make
    make install
    export UNITTEST_DIR=C:\msys64\usr
    cd ~
    ```

6. Build libopenshot-audio the same way, into `/usr` so its libraries land in `C:/msys64/usr/bin`:

    ```sh
    git clone https://github.com/OpenShot/libopenshot-audio.git
    cd libopenshot-audio && mkdir build && cd build
    cmake -G "MSYS Makefiles" -DCMAKE_MAKE_PROGRAM=mingw32-make -DCMAKE_INSTALL_PREFIX:PATH=/usr ../
    make
    make install
    export LIBOPENSHOT_AUDIO_DIR=C:\msys64\usr
    cd ~
    ```

7. Install extra Qt and ZMQ packages for libopenshot:

    ```sh
    pacman -S mingw64/mingw-w64-x86_64-qt5-svg
    pacman -S mingw64/mingw-w64-x86_64-cppzmq
    ```

8. Build libopenshot into MinGW's `/mingw64`:

    ```sh
    git clone https://github.com/OpenShot/libopenshot.git
    cd libopenshot && mkdir build && cd build
    cmake -G "MSYS Makefiles" -DCMAKE_MAKE_PROGRAM=mingw32-make \
      -DCMAKE_INSTALL_PREFIX:PATH=/mingw64 \
      -DDISABLE_TESTS=1 \
      -DCMAKE_CXX_FLAGS="-include cstdint" ../
    make
    make install
    cd ~
    ```

9. Only needed for `freeze.py` or building installers:

    ```sh
    pacman -S --needed mingw64/mingw-w64-x86_64-python-cx-freeze mingw64/mingw-w64-x86_64-python-lief
    ```

10. Clone zenvi-core, install PyQt5, cffi, zstandard, and Qt WebKit via pacman, then set up the venv:

    ```sh
    git clone https://github.com/Zenvi-pro/zenvi-core.git
    cd zenvi-core

    pacman -S --needed \
      mingw-w64-x86_64-python-pyqt5 \
      mingw-w64-x86_64-python-cffi \
      mingw-w64-x86_64-python-zstandard \
      mingw-w64-x86_64-qtwebkit \
      mingw-w64-x86_64-libffi \
      mingw-w64-x86_64-gcc

    /mingw64/bin/python.exe -m venv --system-site-packages .venv
    source .venv/bin/activate
    pip install -r requirements-noqt.txt
    # pip install -r requirements-manim.txt   # if needed
    ```

11. Run Zenvi against your build tree bindings:

    ```sh
    cd ~/zenvi-core
    PYTHONPATH_LIBOPENSHOT=~/libopenshot/build/bindings/python bash run-zenvi-core.sh
    ```

## Known issues

Fresh MSYS2 installs: `pacman -Syu` can close the window mid-run before step 1 finishes, just re-run it.

`pip` can fail compiling `cryptography` and `rpds-py` from `requirements-noqt.txt`; install `mingw-w64-x86_64-python-cryptography` and `mingw-w64-x86_64-python-rpds-py` via pacman instead.

On an existing MSYS2 install, `pacman -Syu` does a full system upgrade that can break an existing libopenshot build; install only the needed packages instead.

MSYS2's default path, `C:\msys64`, is hardcoded in a few places, so adjust manually if yours differs.

`make install` silently overwrites an existing libopenshot or libopenshot-audio install with no warning.
