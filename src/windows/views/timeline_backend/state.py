"""
 @file
 @brief Finite state machine for timeline interactions
 @author Jonathan Thomas <jonathan@openshot.org>

 @section LICENSE

 Copyright (c) 2008-2025 OpenShot Studios, LLC
 (http://www.openshotstudios.com). This file is part of
 OpenShot Video Editor (http://www.openshot.org), an open-source project
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
 """

from PyQt5.QtCore import QState, QStateMachine


class DragState(QState):
    def __init__(self, widget):
        super().__init__()
        self.widget = widget

    def onEntry(self, event):
        self.widget._startClipDrag()

    def onExit(self, event):
        self.widget._finishClipDrag()


class ResizeState(QState):
    def __init__(self, widget):
        super().__init__()
        self.widget = widget

    def onEntry(self, event):
        self.widget._startResize()

    def onExit(self, event):
        self.widget._finishResize()


class PlayheadState(QState):
    def __init__(self, widget):
        super().__init__()
        self.widget = widget

    def onEntry(self, event):
        self.widget._startPlayhead()

    def onExit(self, event):
        self.widget._finishPlayhead()


class BoxSelectState(QState):
    def __init__(self, widget):
        super().__init__()
        self.widget = widget

    def onEntry(self, event):
        self.widget._startBoxSelect()

    def onExit(self, event):
        self.widget._finishBoxSelect()


class KeyframeState(QState):
    def __init__(self, widget):
        super().__init__()
        self.widget = widget

    def onEntry(self, event):
        self.widget._startKeyframeDrag()

    def onExit(self, event):
        self.widget._finishKeyframeDrag()


class TimelineStateMachine(QStateMachine):
    def __init__(self, widget):
        super().__init__(widget)
        self.idle = QState()
        self.drag = DragState(widget)
        self.resize = ResizeState(widget)
        self.playhead = PlayheadState(widget)
        self.box = BoxSelectState(widget)
        self.keyframe = KeyframeState(widget)

        self.setInitialState(self.idle)
        for s in (self.idle, self.drag, self.resize, self.playhead, self.box, self.keyframe):
            self.addState(s)
        self.start()
