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

### Graphify (codebase knowledge graph)

[`graphify-out/`](graphify-out/) is committed so everyone gets a queryable map of this repo. After cloning and setting up your environment:

```bash
# Install the CLI (once per machine)
uv tool install graphifyy          # or: pipx install graphifyy
graphify cursor install            # registers the Cursor rule (or: graphify install)

# Auto-rebuild the graph after each commit (AST only, no API cost).
# Also installs a merge driver so parallel graph.json edits union-merge cleanly.
graphify hook install
```

**Team workflow** ([Graphify team setup](https://github.com/Graphify-Labs/graphify#team-setup)):

1. One person runs `/graphify .` in Cursor (or `graphify extract .`) and commits `graphify-out/` (keep `cache/` and `cost.json` local — gitignored).
2. Everyone else pulls and their assistant can query the graph immediately (`graphify query`, `path`, `explain`).
3. After docs change, run `/graphify --update` (or `graphify update .`) to refresh those nodes.

`manifest.json` uses portable relative paths, so checking out the committed graph avoids a full rebuild on first use. See `.graphifyignore` for paths excluded from indexing (e.g. image assets).

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

## Windows Setup

Windows testers should use the automated setup script, rather than
building everything by hand.

1. Clone this repo and the zenvi-core repo.
2. Open a PowerShell prompt in the repo folder.
3. Run the setup script by typing: run-win.ps1 and pressing Enter.

The script installs MSYS2 (via winget) if it isn't already on your machine,
builds libopenshot and libopenshot-audio from source, sets up a Python
virtual environment, installs all required dependencies, and then launches
Zenvi.

The first run compiles libopenshot and libopenshot-audio from source and
can take over an hour. Later runs are much faster, since the script skips
the build if it has already been done.

### Manual setup (if the script fails or you prefer doing it by hand)

Use the MSYS2 MinGW x64 shell when possible, so that /mingw64/bin is on
your PATH.

Note on Qt WebKit vs Qt WebEngine: Qt WebKit (mingw-w64-x86_64-qtwebkit) is
available through pacman and is used for the HTML/JS timeline. Qt
WebEngine, the Chromium engine used by panels such as Director, plan
review, the thinking dock, and the HTML chat UI, is not available in the
MSYS2 MinGW64 repos, so those panels will show placeholders unless you set
up a separate MSVC CPython environment with PyQtWebEngine installed via
pip. The timeline itself can still run on WebKit or in plain qwidget mode.

Step 1: Install MSYS2 from msys2.org and start the MinGW x64 shell (either
from C:/msys64/msys2_shell.cmd or the Start Menu).

Step 2 (optional): Persist the PATH by adding the following line to your
.bashrc, then reloading it:
add to ~/.bashrc: PATH=$PATH:/c/msys64/mingw64/bin:/c/msys64/mingw64/lib
then run: source ~/.bashrc

Step 3: Sync and install build dependencies. Run:
pacman -Syu
then run:
pacman -S --needed --disable-download-timeout base-devel git mingw-w64-x86_64-toolchain mingw64/mingw-w64-x86_64-ffmpeg mingw64/mingw-w64-x86_64-swig mingw64/mingw-w64-x86_64-cmake mingw64/mingw-w64-x86_64-doxygen mingw64/mingw-w64-x86_64-zeromq mingw64/mingw-w64-x86_64-python-pyqt5 mingw64/mingw-w64-x86_64-python-pip mingw64/mingw-w64-x86_64-python-pyzmq mingw64/mingw-w64-x86_64-rust
then run:
pip3 install httplib2 tinys3 github3.py==0.9.6 requests --break-system-packages

Note on Windows SDK / ASIO vs CI: GitHub's windows-latest CI runners
already include the Windows SDK, so the Steinberg ASIO SDK is not required
in CI, and the production Windows release build disables JUCE_ASIO. Steps
4 and 5 below are only needed on your own machine if you want ASIO
hardware drivers in libopenshot-audio, or if CMake reports it cannot find
the Windows or DirectX paths.

Step 4: Install the Windows SDK from Microsoft's site, then set the
DXSDK_DIR environment variable to point at it, for example:
export DXSDK_DIR="C:\Program Files (x86)\Windows Kits\10"

Step 5: Install the ASIO SDK from steinberg.net, extract it to somewhere
like C:\Program Files, then set the ASIO_SDK_DIR environment variable, for
example:
export ASIO_SDK_DIR="C:\Program Files\ASIOSDK\common"

Step 6: Build and install unittest-cpp into MSYS's /usr. Run:
git clone https://github.com/unittest-cpp/unittest-cpp.git
then:
cd unittest-cpp/builds
then:
cmake -G "MSYS Makefiles" -DCMAKE_MAKE_PROGRAM=mingw32-make -DCMAKE_INSTALL_PREFIX:PATH=/usr -DCMAKE_POLICY_VERSION_MINIMUM=3.5 ../
then:
make
then:
make install
then set: export UNITTEST_DIR=C:\msys64\usr
then: cd ~

Step 7: Build and install libopenshot-audio into /usr, so its libraries
land in C:/msys64/usr/bin. Run:
git clone https://github.com/OpenShot/libopenshot-audio.git
then:
cd libopenshot-audio && mkdir build && cd build
then:
cmake -G "MSYS Makefiles" -DCMAKE_MAKE_PROGRAM=mingw32-make -DCMAKE_INSTALL_PREFIX:PATH=/usr ../
then:
make
then:
make install
then set: export LIBOPENSHOT_AUDIO_DIR=C:\msys64\usr
then: cd ~

Step 8: Install extra Qt and ZMQ packages needed by libopenshot. Run:
pacman -S mingw64/mingw-w64-x86_64-qt5-svg
then:
pacman -S mingw64/mingw-w64-x86_64-cppzmq

Step 9: Build and install libopenshot into MinGW's /mingw64. Run:
git clone https://github.com/OpenShot/libopenshot.git
then:
cd libopenshot && mkdir build && cd build
then:
cmake -G "MSYS Makefiles" -DCMAKE_MAKE_PROGRAM=mingw32-make -DCMAKE_INSTALL_PREFIX:PATH=/mingw64 -DDISABLE_TESTS=1 -DCMAKE_CXX_FLAGS="-include cstdint" ../
then:
make
then:
make install
then: cd ~

Step 10: Packaging tools. These are only needed for freeze.py and building
installers. Pip can often install cx_Freeze on MinGW if MinGW is first on
your PATH and you have base build tools (cmake and ninja) installed, but
pacman is the more reliable way to install lief. Run:
pacman -S --needed mingw64/mingw-w64-x86_64-python-cx-freeze mingw64/mingw-w64-x86_64-python-lief

Step 11: Clone Zenvi Core and finish the Python and Qt timeline
dependencies. Run:
git clone https://github.com/Zenvi-pro/zenvi-core.git
then:
cd zenvi-core
then install PyQt5, cffi, zstandard, and Qt WebKit (note that Qt WebEngine
is not available in MinGW pacman, as mentioned above):
pacman -S --needed mingw-w64-x86_64-python-pyqt5 mingw-w64-x86_64-python-cffi mingw-w64-x86_64-python-zstandard mingw-w64-x86_64-qtwebkit mingw-w64-x86_64-libffi mingw-w64-x86_64-gcc
then create and activate a virtual environment using the same MinGW Python
so it can see the pacman-installed PyQt, cffi, zstandard, and WebKit:
/mingw64/bin/python.exe -m venv --system-site-packages .venv
then:
source .venv/bin/activate
then:
pip install -r requirements-noqt.txt
(also run pip install -r requirements-manim.txt if you need it)

Step 12: Run Zenvi against your build tree bindings, not just the
installed ones. Run:
cd ~/zenvi-core
then:
PYTHONPATH_LIBOPENSHOT=~/libopenshot/build/bindings/python bash run-zenvi-core.sh

### Known issues

Running pacman -Syu can close the MSYS2 window partway through on a fresh
MSYS2 install, which stops the script before Step 1 finishes. Simply
re-run the script a second time and it will complete.

Installing requirements-noqt.txt with pip can fail while compiling
cryptography and rpds-py from source under MSYS2's Python. If this
happens, install mingw-w64-x86_64-python-cryptography and
mingw-w64-x86_64-python-rpds-py through pacman first, instead of letting
pip build them from source.

On a machine that already has MSYS2 installed, running pacman -Syu does a
full system upgrade of hundreds of packages, including major version
bumps to FFmpeg and GCC, which can break an existing libopenshot build and
cause file conflicts in an unused ucrt64 environment. It is safer to
install only the specific packages you need instead of running a full
upgrade on an existing install.

MSYS2's default install path, C:\msys64, is currently hardcoded in a few
places rather than detected automatically, so if your MSYS2 is installed
somewhere else, you will need to adjust those paths yourself.

Running make install will silently overwrite any existing libopenshot or
libopenshot-audio install, so rebuilding replaces a working install with
no warning or confirmation.
