"""
 @file
 @brief This file contains unit tests for selected main window flows
 @author Jonathan Thomas <jonathan@openshot.org>

 @section LICENSE

 Copyright (c) 2008-2026 OpenShot Studios, LLC
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

# Zenvi note: these are the tests OpenShot PR #6017 added to src/tests/test_main_window.py
# (My Views, dock features, the Scopes menu, live-property caching, colour keyframes). Zenvi does
# not carry upstream's full main-window suite, so only the new cases are kept here, on the same harness.


import importlib
import os
import sys
import tempfile
import threading
import types
import unittest
import zipfile
from contextlib import ExitStack
from datetime import datetime, timedelta
from unittest.mock import patch

import openshot

PATH = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if PATH not in sys.path:
    sys.path.append(PATH)

from qt_api import QCoreApplication, Qt
from qt_api import QApplication, QDockWidget, QMainWindow, QMenu, QStandardItem, QStandardItemModel

from classes.project_data import ProjectDataStore
from classes.updates import UpdateManager
from qt_test_app import ensure_app_state as ensure_qt_app_state, get_or_create_app

QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)


class DummySettings:
    actionType = types.SimpleNamespace(LOAD="load")

    def __init__(self):
        self.values = {
            "recent_projects": [],
            "history-limit": 20,
            "recovery-limit": 10,
            "default-profile": "HD 720p 30 fps",
            "default-samplerate": 48000,
            "default-channels": 2,
        }
        self.default_paths = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value

    def setDefaultPath(self, action, path):
        self.default_paths[action] = path


class DummyApp(QApplication):
    def __init__(self):
        super().__init__([])
        self.settings = DummySettings()
        self.project = None
        self.updates = None
        self.window = None
        self.logger_libopenshot = None

    def get_settings(self):
        return self.settings

    def _tr(self, text):
        return text


def ensure_app_state(app):
    return ensure_qt_app_state(
        app,
        DummySettings,
        project_factory=ProjectDataStore,
        updates_factory=UpdateManager,
        extra_attrs={"window": None, "logger_libopenshot": None},
    )


class SignalRecorder:
    def __init__(self):
        self.calls = []

    def emit(self, *args):
        self.calls.append(args)


class FilmGrainMainWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app, cls._owns_app = get_or_create_app(DummyApp)
        cls.app = ensure_app_state(app)
        metrics = types.ModuleType("classes.metrics")
        metrics.track_metric_session = lambda *args, **kwargs: None
        metrics.track_metric_screen = lambda *args, **kwargs: None
        sys.modules["classes.metrics"] = metrics
        sys.modules.pop("windows.views.timeline", None)
        sys.modules.pop("windows.main_window", None)
        sys.modules.pop("windows.views.properties_tableview", None)
        sys.modules.pop("windows.models.properties_model", None)
        cls.main_window_module = importlib.import_module("windows.main_window")
        cls.properties_tableview_module = importlib.import_module("windows.views.properties_tableview")
        cls.properties_model_module = importlib.import_module("windows.models.properties_model")

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "_owns_app", False) and cls.app:
            cls.app.quit()

    def setUp(self):
        ensure_app_state(self.app)
        self.app.settings = DummySettings()
        self.app.window = None


    def test_active_custom_view_setter_does_not_shadow_reader(self):
        fake_window = types.SimpleNamespace()
        fake_window._active_custom_view_id = types.MethodType(
            self.main_window_module.MainWindow._active_custom_view_id,
            fake_window)
        fake_window._set_active_custom_view_id = types.MethodType(
            self.main_window_module.MainWindow._set_active_custom_view_id,
            fake_window)

        fake_window._set_active_custom_view_id("view-1")

        self.assertTrue(callable(fake_window._active_custom_view_id))
        self.assertEqual(fake_window._active_custom_view_id(), "view-1")
        self.assertEqual(self.app.settings.values["active_custom_view"], "view-1")

    def test_add_and_show_docks_keep_default_dock_features(self):
        fake_window = QMainWindow()
        normal_dock = QDockWidget("Normal", fake_window)
        normal_dock.setObjectName("dockNormal")

        self.main_window_module.MainWindow.addDocks(fake_window, [normal_dock], Qt.RightDockWidgetArea)
        self.assertTrue(normal_dock.features() & QDockWidget.DockWidgetClosable)
        self.assertTrue(normal_dock.features() & QDockWidget.DockWidgetMovable)
        self.assertTrue(normal_dock.features() & QDockWidget.DockWidgetFloatable)

        normal_dock.hide()
        fake_window.showDocks = lambda docks: self.main_window_module.MainWindow.showDocks(fake_window, docks)
        self.main_window_module.MainWindow.showDocks(fake_window, [normal_dock])
        self.assertTrue(normal_dock.features() & QDockWidget.DockWidgetClosable)
        self.assertTrue(normal_dock.features() & QDockWidget.DockWidgetMovable)
        self.assertTrue(normal_dock.features() & QDockWidget.DockWidgetFloatable)

    def test_scope_menu_keeps_conditional_show_and_close_all_actions(self):
        fake_window = QMainWindow()
        fake_window.scopes_menu = QMenu(fake_window)
        fake_window.dockAudio = QDockWidget("Audio Levels", fake_window)
        fake_window.dockAudio.setObjectName("dockAudio")
        fake_window.dockHistogram = QDockWidget("Histogram", fake_window)
        fake_window.dockHistogram.setObjectName("dockHistogram")
        fake_window.dockLumaWaveform = QDockWidget("Luma Waveform", fake_window)
        fake_window.dockLumaWaveform.setObjectName("dockLumaWaveform")
        fake_window.dockVectorscope = QDockWidget("Vectorscope", fake_window)
        fake_window.dockVectorscope.setObjectName("dockVectorscope")
        for dock in [
                fake_window.dockAudio,
                fake_window.dockHistogram,
                fake_window.dockLumaWaveform,
                fake_window.dockVectorscope]:
            fake_window.addDockWidget(Qt.RightDockWidgetArea, dock)
            dock.hide()

        open_docks = set()
        fake_window._scope_docks = lambda: self.main_window_module.MainWindow._scope_docks(fake_window)
        fake_window._dock_is_open = lambda dock: dock in open_docks
        fake_window.closeDocks = lambda docks: self.main_window_module.MainWindow.closeDocks(fake_window, docks)
        fake_window.show_all_scope_docks = lambda: None
        fake_window._add_dock_visibility_actions = (
            lambda menu, docks, show_text, close_text, show_callback=None:
            self.main_window_module.MainWindow._add_dock_visibility_actions(
                fake_window, menu, docks, show_text, close_text, show_callback))

        self.main_window_module.MainWindow._rebuild_scopes_menu(fake_window)
        action_texts = [action.text() for action in fake_window.scopes_menu.actions() if not action.isSeparator()]
        self.assertIn("Show All Scopes", action_texts)
        self.assertNotIn("Close All Scopes", action_texts)
        self.assertNotIn("Lock Scopes", action_texts)

        open_docks.add(fake_window.dockAudio)
        self.main_window_module.MainWindow._rebuild_scopes_menu(fake_window)
        action_texts = [action.text() for action in fake_window.scopes_menu.actions() if not action.isSeparator()]
        self.assertIn("Show All Scopes", action_texts)
        self.assertIn("Close All Scopes", action_texts)
        self.assertNotIn("Unlock Scopes", action_texts)

    def test_live_property_resume_keeps_cache_disabled_until_seek_or_play(self):
        settings = openshot.Settings.Instance()
        previous = settings.ENABLE_PLAYBACK_CACHING
        try:
            settings.ENABLE_PLAYBACK_CACHING = False
            fake_view = types.SimpleNamespace(live_property_cache_paused=True)

            self.properties_tableview_module.PropertiesTableView.resume_live_property_caching(fake_view)

            self.assertFalse(fake_view.live_property_cache_paused)
            self.assertFalse(settings.ENABLE_PLAYBACK_CACHING)
        finally:
            settings.ENABLE_PLAYBACK_CACHING = previous

    def test_insert_keyframe_adds_current_color_property_frame(self):
        saved = []
        refreshed = SignalRecorder()
        self.app.window = types.SimpleNamespace(refreshFrameSignal=refreshed)

        effect = types.SimpleNamespace(
            data={
                "wave_color": {
                    "red": {"Points": [{"co": {"X": 1.0, "Y": 0.0}, "interpolation": openshot.LINEAR}]},
                    "green": {"Points": [{"co": {"X": 1.0, "Y": 123.0}, "interpolation": openshot.LINEAR}]},
                    "blue": {"Points": [{"co": {"X": 1.0, "Y": 255.0}, "interpolation": openshot.LINEAR}]},
                    "alpha": {"Points": [{"co": {"X": 1.0, "Y": 255.0}, "interpolation": openshot.LINEAR}]},
                }
            },
        )
        effect.save = lambda: saved.append(effect.data)

        model = QStandardItemModel()
        label = QStandardItem("Wave Color")
        label.setData((
            "wave_color",
            {
                "type": "color",
                "red": {"value": 0},
                "green": {"value": 123},
                "blue": {"value": 255},
                "alpha": {"value": 255},
                "closest_point_x": 1,
                "previous_point_x": 1,
                "object_id": None,
                "max": 255.0,
            },
        ))
        value = QStandardItem("")
        value.setData([("effect-1", "effect")])
        model.appendRow([label, value])

        parent = types.SimpleNamespace(
            currentIndex=lambda: model.index(0, 0),
            clearSelection=lambda: None,
            setCurrentIndex=lambda index: None,
        )
        helper = self.properties_model_module.PropertiesModel.__new__(
            self.properties_model_module.PropertiesModel)
        helper.model = model
        helper.parent = parent
        helper.frame_number = 30
        helper._trim_preview_mode = False

        with patch.object(self.properties_model_module.Effect, "get", return_value=effect):
            helper.insert_keyframe(value)

        self.assertEqual(len(saved), 1)
        color = effect.data["wave_color"]
        self.assertIn(30, [point["co"]["X"] for point in color["red"]["Points"]])
        self.assertIn(30, [point["co"]["X"] for point in color["green"]["Points"]])
        self.assertIn(30, [point["co"]["X"] for point in color["blue"]["Points"]])
        self.assertIn(30, [point["co"]["X"] for point in color["alpha"]["Points"]])
        self.assertEqual(refreshed.calls, [()])


if __name__ == "__main__":
    unittest.main()
