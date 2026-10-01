"""Stand-ins for the preview player and the editor's selection, for the tracks-nav tool tests.

The harness window is a MagicMock. These helpers give it just enough behaviour
to observe what the tools do: a player that reacts to the transport signals
the way the preview worker applies them, and a selection list kept the way
``MainWindow.addSelection`` / ``removeSelection`` / ``clearSelections`` keep it.
The UI handlers they wire (actionPlay_trigger, actionFastForward_trigger, ...)
follow main_window.py.
"""

from __future__ import annotations

PLAY, PAUSED = 0, 1  # openshot.PLAYBACK_PLAY / PLAYBACK_PAUSED


class FakePlayer:
    """The preview's QtPlayer as the tools see it: Position / Speed / Mode."""

    def __init__(self, position=1):
        self.position = int(position)
        self.speed = 0
        self.mode = PAUSED
        self.frozen = False  # True: ignore every signal (a player that never answers)

    def Position(self):
        return self.position

    def Speed(self):
        return self.speed

    def Mode(self):
        return self.mode

    # What PlayerWorker.Play / Pause / Seek / Speed do with it.
    def play(self):
        if not self.frozen:
            self.mode, self.speed = PLAY, 1

    def pause(self):
        if not self.frozen:
            self.mode, self.speed = PAUSED, 0

    def seek(self, frame):
        if not self.frozen:
            self.position = max(1, int(frame))

    def set_speed(self, speed):
        if not self.frozen:
            self.speed = speed

    @property
    def playing(self):
        return self.mode == PLAY and self.speed != 0


def install_player(editor, *, last_frame=720, position=1):
    """Give editor.window a FakePlayer and main_window-like transport handlers."""
    player = FakePlayer(position)
    win = editor.window
    win.preview_thread.player = player
    win.preview_thread.timeline.GetMinFrame.return_value = 1
    win.timeline_sync.GetLastFrame.return_value = last_frame

    win.PlaySignal.emit.side_effect = lambda *a: player.play()
    win.PauseSignal.emit.side_effect = lambda *a: player.pause()
    win.SeekSignal.emit.side_effect = lambda frame: player.seek(frame)
    win.SpeedSignal.emit.side_effect = lambda speed: player.set_speed(speed)

    def should_play(requested_speed=0):
        next_frame = player.position + requested_speed
        return 0 < next_frame <= win.timeline_sync.GetLastFrame.return_value

    def action_play():
        if not player.playing:
            if should_play():
                win.PlaySignal.emit()
        else:
            win.PauseSignal.emit()

    def fast_forward():
        requested = player.speed + 1
        if requested == 0:
            requested = 2
        if player.mode != PLAY:
            action_play()
        if should_play(requested):
            win.SpeedSignal.emit(requested)

    def rewind():
        requested = player.speed - 1
        if requested == 0:
            requested = -1
        if should_play(requested):
            if player.mode != PLAY:
                action_play()
            win.SpeedSignal.emit(requested)

    def jump_start():
        speed = player.speed
        win.SpeedSignal.emit(1)
        win.SpeedSignal.emit(0)
        win.SeekSignal.emit(1)
        if speed >= 0:
            win.SpeedSignal.emit(speed)
        else:
            win.PauseSignal.emit()

    def jump_end():
        win.SeekSignal.emit(win.timeline_sync.GetLastFrame.return_value)

    def step_frames(delta):
        frame = max(1, player.position + int(delta))
        win.PauseSignal.emit()
        win.SpeedSignal.emit(0)
        player.seek(frame)
        return frame

    win.should_play.side_effect = should_play
    win.actionPlay_trigger.side_effect = action_play
    win.actionFastForward_trigger.side_effect = fast_forward
    win.actionRewind_trigger.side_effect = rewind
    win.actionJumpStart_trigger.side_effect = jump_start
    win.actionJumpEnd_trigger.side_effect = jump_end
    win.step_frames.side_effect = step_frames
    return player


class FakeSelection:
    """window.selected_items kept like MainWindow does, plus the derived id lists."""

    def __init__(self, editor, *, native=True):
        self.editor = editor
        self.win = editor.window
        self.win.selected_items = []
        self.win.selected_markers = []
        self.win.selected_tracks = []
        self._sync()
        self.win.addSelection.side_effect = self.add
        self.win.removeSelection.side_effect = self.remove
        self.win.clearSelections.side_effect = self.clear
        tl = self.win.timeline
        if native:
            # The native timeline widget updates the editor's selection synchronously.
            tl.AddSelectionJS.side_effect = self.add
            tl._deselect_timeline_item.side_effect = self.remove
            tl.ClearAllSelections.side_effect = self.clear
            tl.SelectAll.side_effect = self.select_all
            tl.addRippleSelection.side_effect = self.ripple

    def _sync(self):
        items = self.win.selected_items
        self.win.selected_clips = [s["id"] for s in items if s["type"] == "clip"]
        self.win.selected_transitions = [s["id"] for s in items if s["type"] == "transition"]
        self.win.selected_effects = [s["id"] for s in items if s["type"] == "effect"]

    def add(self, item_id, item_type, clear_existing=False):
        if clear_existing:
            if item_id or not item_type:
                self.win.selected_items = []
            else:
                self.win.selected_items = [s for s in self.win.selected_items if s["type"] != item_type]
        if item_id and not any(s["id"] == item_id and s["type"] == item_type for s in self.win.selected_items):
            self.win.selected_items.append({"id": item_id, "type": item_type})
        self._sync()

    def remove(self, item_id, item_type):
        for sel in list(self.win.selected_items):
            if sel["id"] == item_id and (item_type is None or sel["type"] == item_type):
                self.win.selected_items.remove(sel)
                break
        self._sync()

    def clear(self):
        self.win.selected_items = []
        self.win.selected_markers = []
        self.win.selected_tracks = []
        self._sync()

    def select_all(self):
        self.clear()
        for c in self.editor.clips():
            self.add(c["id"], "clip")
        for t in self.editor.store._data.get("effects") or []:
            self.add(t["id"], "transition")

    def ripple(self, item_id, item_type):
        """TimelineWidget.selectRipple: the item and everything at or after it on its layer."""
        items = self.editor.clips() + list(self.editor.store._data.get("effects") or [])
        target = next((d for d in items if d["id"] == item_id), None)
        if target is None:
            return
        for d in items:
            if d.get("layer") == target.get("layer") and float(d["position"]) >= float(target["position"]):
                kind = "clip" if d in self.editor.clips() else "transition"
                self.add(d["id"], kind)

    @property
    def pairs(self):
        return [(s["id"], s["type"]) for s in self.win.selected_items]


def add_transition(editor, *, layer, position, duration=1.0):
    """Insert a Mask transition record shaped like Timeline.addTransition makes."""
    from classes.query import Transition

    t = Transition()
    t.data = {"type": "Mask", "layer": int(layer), "position": float(position), "start": 0.0,
              "end": float(duration), "duration": float(duration), "title": "Transition",
              "reader": {"path": "/transitions/common/fade.svg"}}
    t.save()
    editor.mark()
    return t.id


def set_layers(editor, numbers, labels=None, locks=None):
    """Replace the project's tracks (setup only; not undoable)."""
    labels = labels or {}
    locks = locks or {}
    editor.store._data["layers"] = [
        {"id": "L%d" % (i + 1), "label": labels.get(n, ""), "number": int(n), "y": 0,
         "lock": bool(locks.get(n, False))}
        for i, n in enumerate(numbers)
    ]
    editor.mark()
