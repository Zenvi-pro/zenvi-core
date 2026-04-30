# Zenvi

Zenvi is an award-winning free and open-source video editor 
for Linux, Mac, and Windows, and is dedicated to delivering high quality 
video editing and animation solutions to the world.

## Build Status

[![openshot-qt CI Build](https://github.com/OpenShot/openshot-qt/actions/workflows/ci.yml/badge.svg)](https://github.com/OpenShot/openshot-qt/actions/workflows/ci.yml) 
[![libopenshot CI Build](https://github.com/OpenShot/libopenshot/actions/workflows/ci.yml/badge.svg)](https://github.com/OpenShot/libopenshot/actions/workflows/ci.yml) 
[![libopenshot-audio CI Build](https://github.com/OpenShot/libopenshot-audio/actions/workflows/ci.yml/badge.svg)](https://github.com/OpenShot/libopenshot-audio/actions/workflows/ci.yml)
![Discord](https://img.shields.io/discord/1143390791507644496?style=flat)

## Features

* Cross-platform (Linux, Mac, and Windows)
* Support for many video, audio, and image formats (based on FFmpeg)
* Powerful curve-based Key frame animations
* Desktop integration (drag and drop support)
* Unlimited tracks / layers
* Clip resizing, scaling, trimming, snapping, rotation, and cutting
* Video transitions with real-time previews
* Compositing, image overlays, watermarks
* Title templates, title creation, sub-titles
* 2D animation support (image sequences)
* 3D animated titles (and effects)
* SVG friendly, to create and include vector titles and credits
* Scrolling motion picture credits
* Advanced Timeline (including Drag & drop, scrolling, panning, zooming, and snapping)
* Frame accuracy (step through each frame of video)
* Time-mapping and speed changes on clips (slow/fast, forward/backward, etc...)
* Audio mixing and editing
* Digital video effects, including brightness, gamma, hue, greyscale, chroma key, and many more!
* Experimental hardware encoding and decoding (VA-API, NVDEC, D3D9, D3D11, VTB)
* Import & Export widely supported formats (EDL, XML)
* Render videos in many codecs and formats (based on FFmpeg)

## Getting Started

The quickest way to get started using Zenvi is to download one of 
our pre-built installers. On our download page, click the **Daily Builds** 
button to view the latest, experimental builds, which are created for each 
new commit to this repo.

https://zenvi.pro/download/

## Tutorial

Watch the official [step-by-step video tutorial](https://www.youtube.com/watch?list=PLymupH2aoNQNezYzv2lhSwvoyZgLp1Q0T&v=1k-ISfd-YBE), or read the official [user-guide](https://www.openshot.org/user-guide/):

## Developers

Are you interested in becoming more involved in the development of 
Zenvi? Build exciting new features, fix bugs, make friends, and become a hero! 
Please read the [step-by-step](https://github.com/OpenShot/openshot-qt/wiki/Become-a-Developer) 
instructions for getting source code, configuring dependencies, and building Zenvi.

## Documentation

Beautiful HTML documentation can be generated using Sphinx.

```sh
cd doc
make html
```

The documentation for the most recent release can be viewed online at [openshot.org/user-guide](https://www.openshot.org/user-guide/).

## Report a bug

Please report bugs using the official [Report a Bug](https://zenvi.pro/support/) 
feature on our website. This walks you through the bug reporting process, and helps 
to create a high-quality bug report for the Zenvi community.

Or you can report a new issue directly on GitHub:

https://github.com/OpenShot/openshot-qt/issues

## Translations

Translating OpenShot into other languages is very easy! Please read the [step-by-step](https://github.com/OpenShot/openshot-qt/wiki/Become-a-Translator) instructions or login to LaunchPad and get started.
All you need is a web browser.

* Application Translations: https://translations.launchpad.net/openshot/2.0/+translations
* Website Translations: https://translations.launchpad.net/openshot/website/+pots/django

## Dependencies

Although installers are much easier to use, if you must build from 
source, here are some tips: 

OpenShot is programmed in Python (version 3+), and thus does not need
to be compiled to run. However, be sure you have the following 
dependencies in order to run OpenShot successfully: 

*  Python 3.0+ (http://www.python.org)
*  PyQt5 (http://www.riverbankcomputing.co.uk/software/pyqt/download5)
*  libopenshot: OpenShot Library (https://github.com/OpenShot/libopenshot)
*  libopenshot-audio: OpenShot Audio Library (https://github.com/OpenShot/libopenshot-audio)
*  FFmpeg or Libav (http://www.ffmpeg.org/ or http://libav.org/)
*  GCC build tools (or MinGW on Windows)

## Launch

To run OpenShot from the command line with an installed `libopenshot`,
use the following syntax:
(be sure the change the path to match the install or repo location 
of openshot-qt)

```sh
cd [openshot-qt folder]
python3 src/launch.py
```
    
To run with a version of `libopenshot` built from source but not installed,
set `PYTHONPATH` to the location of the compiled Python bindings. e.g.:

```sh
cd [libopenshot folder]
cmake -B build -S . [options]
cmake --build build
    
cd [openshot-qt folder]
PYTHONPATH=[libopenshot folder]/build/bindings/python \
python3 src/launch.py
```

## Websites

- https://www.openshot.org/  (Official website and blog)
- https://github.com/OpenShot/openshot-qt (source code and issue tracker)
- https://github.com/OpenShot/libopenshot-audio (source code for audio library)
- https://github.com/OpenShot/libopenshot (source code for video library)
- https://launchpad.net/openshot/

### Copyright & License

Copyright (c) 2008-2022 OpenShot Studios, LLC. This file is part of
OpenShot Video Editor (https://www.openshot.org), an open-source project
dedicated to delivering high quality video editing and animation solutions
to the world.

OpenShot Video Editor is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

OpenShot Video Editor is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with OpenShot Library.  If not, see <http://www.gnu.org/licenses/>.

# Zenvi Core Updates:
## Custom Installation:
1. Create a virtual environment & install required dependencies: 
    ```sh
    cd [zenvi-core folder]
    sudo apt install python3-pyqt5 python3-pyqt5.qtwebengine
    sudo apt-get install -y libcairo2-dev libpango1.0-dev pkg-config ffmpeg
    python3 -m venv --system-site-packages .venv
    source ./.venv/bin/activate
    pip3 install -r requirements-noqt.txt
    pip3 install -r requirements-manim.txt
    ```
1. Run the application:
    ```sh
    cd [libopenshot folder]
    cmake -B build -S . [options]
    cmake --build build
        
    cd [zenvi-core folder]
    PYTHONPATH_LIBOPENSHOT=[libopenshot folder]/build/bindings/python \
    bash run-zenvi-core.sh
    ```

## MSYS2 (Windows): libopenshot + Zenvi Core

Use the **MSYS2 MinGW x64** shell when possible so `/mingw64/bin` is on `PATH`. Qt **WebEngine** is **not** available in the MinGW64 pacman repos; install **qtwebkit** below for the HTML timeline (or run with `-b qwidget`).

1. Install [MSYS2](https://www.msys2.org/) and start `C:/msys64/msys2_shell.cmd` (or **MinGW x64** from the Start Menu).

2. Persist PATH (optional):

    ```sh
    echo 'PATH=$PATH:/c/msys64/mingw64/bin:/c/msys64/mingw64/lib' >> ~/.bashrc
    source ~/.bashrc
    ```

3. Sync and install build dependencies:

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

4. Install the [Windows SDK](https://learn.microsoft.com/en-us/windows/apps/windows-sdk/) and set:

    ```sh
    export DXSDK_DIR="C:\Program Files (x86)\Windows Kits\10"
    ```

5. Install the [ASIO SDK](https://www.steinberg.net/asiosdk), extract to e.g. `C:\Program Files`, then:

    ```sh
    export ASIO_SDK_DIR="C:\Program Files\ASIOSDK\common"
    ```

6. **unittest-cpp** (install to MSYS `/usr`):

    ```sh
    git clone https://github.com/unittest-cpp/unittest-cpp.git
    cd unittest-cpp/builds
    cmake -G "MSYS Makefiles" -DCMAKE_MAKE_PROGRAM=mingw32-make -DCMAKE_INSTALL_PREFIX:PATH=/usr -DCMAKE_POLICY_VERSION_MINIMUM=3.5 ../
    make
    make install
    export UNITTEST_DIR=C:\msys64\usr
    cd ~
    ```

7. **libopenshot-audio** (install to `/usr` so libraries land in `C:/msys64/usr/bin`):

    ```sh
    git clone https://github.com/OpenShot/libopenshot-audio.git
    cd libopenshot-audio && mkdir build && cd build
    cmake -G "MSYS Makefiles" -DCMAKE_MAKE_PROGRAM=mingw32-make -DCMAKE_INSTALL_PREFIX:PATH=/usr ../
    make && make install
    export LIBOPENSHOT_AUDIO_DIR=C:\msys64\usr
    cd ~
    ```

8. Extra Qt / ZMQ for libopenshot:

    ```sh
    pacman -S mingw64/mingw-w64-x86_64-qt5-svg
    pacman -S mingw64/mingw-w64-x86_64-cppzmq
    ```

9. **libopenshot** (install public libs to MinGW `/mingw64`):

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

10. **Packaging / lief** (prefer pacman; pip cannot reliably build `lief` on MinGW):

    ```sh
    pacman -S --needed mingw64/mingw-w64-x86_64-python-cx-freeze mingw64/mingw-w64-x86_64-python-lief
    ```

11. **Clone Zenvi Core** and finish Python / Qt timeline deps:

    ```sh
    git clone https://github.com/Zenvi-pro/zenvi-core.git
    cd zenvi-core

    # HTML timeline: PyQt5 WebKit bindings are in python-pyqt5; Qt DLLs come from qtwebkit.
    # (There is no qt5-webengine / PyQt5 QtWebEngine in MSYS2 MinGW64.)
    pacman -S --needed mingw64/mingw-w64-x86_64-qtwebkit

    # Optional: inspect missing DLLs for native modules
    # pacman -S --needed mingw64/mingw-w64-x86_64-ntldd

    python -m venv --system-site-packages .venv
    source .venv/bin/activate
    pip install -r requirements-noqt.txt
    # pip install -r requirements-manim.txt   # if needed
    ```

12. **Run Zenvi** against your **build tree** bindings (not only install):

    ```sh
    cd ~/zenvi-core
    PYTHONPATH_LIBOPENSHOT=~/libopenshot/build/bindings/python bash run-zenvi-core.sh
    ```

    `run-zenvi-core.sh` sets `PATH` and Windows DLL search paths so `libopenshot.dll` finds **`libopenshot-audio.dll`** under `C:/msys64/usr/bin` and MinGW/Qt FFmpeg DLLs under `/mingw64/bin`.

    If the timeline backend fails to load, force WebKit or the Qt-only timeline:

    ```sh
    bash run-zenvi-core.sh -b webkit
    # or
    bash run-zenvi-core.sh -b qwidget
    ```
