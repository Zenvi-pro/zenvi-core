.. Copyright (c) 2008-2016 OpenShot Studios, LLC
 (http://www.openshotstudios.com). This file is part of
 OpenShot Video Editor (http://www.openshot.org), an open-source project
 dedicated to delivering high quality video editing and animation solutions
 to the world.

.. OpenShot Video Editor is free software: you can redistribute it and/or modify
 it under the terms of the GNU General Public License as published by
 the Free Software Foundation, either version 3 of the License, or
 (at your option) any later version.

.. OpenShot Video Editor is distributed in the hope that it will be useful,
 but WITHOUT ANY WARRANTY; without even the implied warranty of
 MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 GNU General Public License for more details.

.. You should have received a copy of the GNU General Public License
 along with OpenShot Library.  If not, see <http://www.gnu.org/licenses/>.

Import & Export
===============

Video editing projects (including tracks, clips, and keyframes) can be **imported** and **exported** from OpenShot
Video Editor in widely supported formats (**EDL**: Edit Decision Lists, and **XML**: Final Cut Pro format). For example, if
you start editing a video in a different program (Adobe Premier, Final Cut Pro, etc...), but later need to move all
your edits to OpenShot (or vice versa).

EDL (Edit Decision Lists)
-------------------------
The following features are supported when importing and exporting an EDL file with OpenShot.

.. table::
   :widths: 25 80

   ====================  ============
   EDL Option Name       Description
   ====================  ============
   EDL Format            CMX-3600 (a very widely supported variation)
   Single Track          Only a single track can be imported at a time (this is a limitation of the EDL format)
   Tape Name             Only **AX** and **BL** tape names are currently supported in OpenShot
   Edits (V and A)       Only edits are currently supported (transitions are not yet supported)
   Opacity               Opacity keyframes are supported
   Audio Levels          Volume keyframes are supported
   ====================  ============

Example EDL Output
^^^^^^^^^^^^^^^^^^

.. code-block:: python

   :caption: Example EDL format supported by OpenShot:

    TITLE: Test - TRACK 5
    FCM: NON-DROP FRAME

    001  BL       V     C        00:00:00:00 00:00:01:24 00:00:00:00 00:00:01:24
    002  AX       V     C        00:00:01:24 00:00:10:00 00:00:01:24 00:00:10:00
    * FROM CLIP NAME: 1F499.svg
    * SOURCE FILE: ../apps/openshot-qt/src/emojis/color/svg/1F499.svg
    * VIDEO LEVEL AT 00:00:00:00 IS 100% interp:bezier
    * AUDIO LEVEL AT 00:00:00:00 IS 0.00 dB interp:linear
    * SCALE X AT 00:00:01:24 IS 100% interp:bezier
    * SCALE X AT 00:00:09:29 IS 93% interp:bezier
    * SCALE Y AT 00:00:01:24 IS 100% interp:bezier
    * SCALE Y AT 00:00:09:29 IS 55% interp:bezier
    * LOCATION X AT 00:00:01:24 IS 0% interp:bezier
    * LOCATION X AT 00:00:09:29 IS -1% interp:bezier
    * LOCATION Y AT 00:00:01:24 IS 0% interp:bezier
    * LOCATION Y AT 00:00:09:29 IS -32% interp:bezier
    * ROTATION AT 00:00:01:24 IS 0DEG interp:bezier
    * ROTATION AT 00:00:09:29 IS 23.3DEG interp:bezier
    * SHEAR X AT 00:00:01:24 IS 0% interp:bezier
    * SHEAR X AT 00:00:09:29 IS -12% interp:bezier
    * SHEAR Y AT 00:00:01:24 IS 0% interp:bezier
    * SHEAR Y AT 00:00:09:29 IS -7% interp:bezier

    TITLE: Test - TRACK 4
    FCM: NON-DROP FRAME

    001  AX       V     C        00:00:00:00 00:00:09:29 00:00:00:00 00:00:09:29
    001  AX       A     C        00:00:00:00 00:00:09:29 00:00:00:00 00:00:09:29
    * FROM CLIP NAME: sintel_trailer-720p.mp4
    * SOURCE FILE: ../Downloads/openshot-testing/sintel_trailer-720p.mp4
    * VIDEO LEVEL AT 00:00:00:00 IS 0% interp:bezier
    * VIDEO LEVEL AT 00:00:01:00 IS 100% interp:bezier
    * VIDEO LEVEL AT 00:00:08:29 IS 100% interp:bezier
    * VIDEO LEVEL AT 00:00:09:29 IS 0% interp:bezier
    * AUDIO LEVEL AT 00:00:00:00 IS 0.00 dB interp:linear

    TITLE: Test - TRACK 3
    FCM: NON-DROP FRAME

    001  AX       V     C        00:00:00:00 00:00:09:29 00:00:00:00 00:00:09:29
    001  AX       A     C        00:00:00:00 00:00:09:29 00:00:00:00 00:00:09:29
    * FROM CLIP NAME: Kai_Engel_-_04_-_Moonlight_Reprise.mp3
    * SOURCE FILE: ../Downloads/openshot-testing/Kai_Engel_-_04_-_Moonlight_Reprise.mp3
    * VIDEO LEVEL AT 00:00:00:00 IS 100% interp:bezier
    * AUDIO LEVEL AT 00:00:00:00 IS -96.00 dB interp:linear
    * AUDIO LEVEL AT 00:00:03:00 IS 0.00 dB interp:linear
    * AUDIO LEVEL AT 00:00:06:29 IS 0.00 dB interp:linear
    * AUDIO LEVEL AT 00:00:09:29 IS -96.00 dB interp:linear

XML (Final Cut Pro format)
--------------------------
The following features are supported when importing and exporting an XML file with OpenShot. This XML format
is supported in many video editors (not just Final Cut Pro). In fact, most commercial video editors have some
support for importing and exporting this same XML format.

.. table::
   :widths: 25 80

   ====================  ============
   XML Option Name       Description
   ====================  ============
   XML Format            Final Cut Pro format (but most commercial video editors also support this format)
   All Tracks            All video and audio tracks are supported
   Edits                 All clips on all tracks are supported (video, image, and audio files). Transitions are not yet supported.
   Opacity               Opacity keyframes are supported
   Audio Levels          Volume keyframes are supported
   ====================  ============

Example XML Output (tree view)
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

.. image:: images/fcp-xml-tree-view.jpg
