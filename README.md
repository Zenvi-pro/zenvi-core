# OpenShot Video Editor

OpenShot Video Editor is an award-winning free and open-source video editor 
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

The quickest way to get started using OpenShot is to download one of 
our pre-built installers. On our download page, click the **Daily Builds** 
button to view the latest, experimental builds, which are created for each 
new commit to this repo.

https://www.openshot.org/download/

## Tutorial

Watch the official [step-by-step video tutorial](https://www.youtube.com/watch?list=PLymupH2aoNQNezYzv2lhSwvoyZgLp1Q0T&v=1k-ISfd-YBE), or read the official [user-guide](https://www.openshot.org/user-guide/):

## Developers

Are you interested in becoming more involved in the development of 
OpenShot? Build exciting new features, fix bugs, make friends, and become a hero! 
Please read the [step-by-step](https://github.com/OpenShot/openshot-qt/wiki/Become-a-Developer) 
instructions for getting source code, configuring dependencies, and building OpenShot.

## Documentation

Beautiful HTML documentation can be generated using Sphinx.

```sh
cd doc
make html
```

The documentation for the most recent release can be viewed online at [openshot.org/user-guide](https://www.openshot.org/user-guide/).

## Report a bug

Please report bugs using the official [Report a Bug](https://www.openshot.org/issues/new/) 
feature on our website. This walks you through the bug reporting process, and helps 
to create a high-quality bug report for the OpenShot community.

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
    python3 -m venv .venv
    source ./.venv/bin/activate
    pip3 install -r requirements.txt
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

## Basic Chat UI:
## Overview

The video editor includes an AI Assistant dock widget for chat interactions. The UI is integrated as a dockable panel similar to other editor panels.

## How to Access

1. Open the AI Assistant:
   - Click on the View menu
   - Select Docks
   - Click on AI Assistant

2. The chat panel will appear as a dockable widget that can be moved and resized like other panels.

## Using the Chat

- Type your message in the input field at the bottom
- Press Enter to send the message
- Press Shift+Enter to create a new line without sending
- Click Clear to reset the chat conversation
- Select a different model from the Model dropdown

## Window Behavior

The chat window state is saved with your application preferences:

- If you close the application with the chat window open, the chat window will appear when you start the application again.
- If you close the chat window (by clicking the X button on the dock) before closing the application, the chat window will not appear on the next startup.

To toggle the chat visibility, use View > Docks > AI Assistant at any time.

## Gemini + LangChain Setup

1. Install the Gemini dependencies inside your virtual environment:
    ```sh
    pip install langchain langchain-google-genai python-dotenv
    ```
2. Put your API key in a .env file at the repo root (preferred):
    ```sh
    echo "GEMINI_API_KEY=your-key" > .env
    ```
3. Launch the app with your venv activated and open View > Docks > AI Assistant.
4. The model dropdown will automatically populate with models available on your API key.
   - **Free tier**: Limited quota and available models. Check https://ai.google.dev/gemini-api/docs/rate-limits for quota info.
   - If you see "No models available", you've exceeded your API quota or the key is invalid.

### Example: Export via Chat

- Ask: "Export at 1920x1080" or "Export this video at 1080p".
- The AI will use the export tool to automatically start a render with the requested dimensions.
