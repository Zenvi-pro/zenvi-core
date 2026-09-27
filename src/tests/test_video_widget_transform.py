"""
 @file
 @brief This file contains unit tests for VideoWidget transform and location geometry
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

import os
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import openshot
from qt_api import QApplication, QColor, QPoint, QRect, QRectF, QStandardItem, QTransform, Qt, QWidget


PATH = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if PATH not in sys.path:
    sys.path.append(PATH)

from tests.qt_test_app import ensure_app_state, get_or_create_app


class DummySettings:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key, False)

    def set(self, key, value):
        self.values[key] = value


class DummyApp(QApplication):
    def __init__(self):
        super().__init__([])
        self.settings = DummySettings()


app, _owns_app = get_or_create_app(DummyApp)
ensure_app_state(app, DummySettings, extra_attrs={"window": types.SimpleNamespace()})

from windows.video_widget import VideoWidget
from windows.models.properties_model import ClipStandardItemModel, PropertiesModel
from windows.process_effect import (
    ProcessEffect,
    load_yolo_models_manifest,
    yolo_classes_path,
    yolo_download_button_label,
    yolo_model_label,
    yolo_model_path,
)
from classes import http_client
from classes.effect_init import effect_options


def clip_with(scale_mode, gravity=openshot.GRAVITY_CENTER):
    return types.SimpleNamespace(data={"scale": scale_mode, "gravity": gravity})


def props(location_x=0.0, location_y=0.0, scale_x=1.0, scale_y=1.0):
    return {
        "scale_x": {"value": scale_x},
        "scale_y": {"value": scale_y},
        "location_x": {"value": location_x},
        "location_y": {"value": location_y},
        "parentObjectId": {"memo": ""},
    }


class FakeSignal:
    def __init__(self):
        self.count = 0

    def emit(self, *args):
        self.count += 1


def mouse_event_at(x, y):
    return types.SimpleNamespace(pos=lambda: QPoint(x, y))


class FakePropertiesParent:
    def __init__(self, model):
        self.model = model

    def currentIndex(self):
        return self.model.index(0, 1)

    def clearSelection(self):
        pass

    def setCurrentIndex(self, index):
        pass


class VideoWidgetTransformTests(unittest.TestCase):
    """Upstream's test_video_widget_transform.py minus the motion-preset location
    geometry cases (_clip_location_geometry, SCALE_NONE), which live with the port
    that brings those features."""

    def setUp(self):
        self.widget = VideoWidget.__new__(VideoWidget)
        self.viewport = QRect(0, 0, 160, 90)

    def rect_for(self, scale_mode, location_x=0.0, location_y=0.0, scale_x=1.0, scale_y=1.0):
        return VideoWidget._clip_display_rect(
            self.widget,
            40,
            40,
            clip_with(scale_mode),
            props(location_x, location_y, scale_x, scale_y),
            self.viewport,
        )

    def test_square_clip_location_x_endpoints_are_offscreen_for_fit_and_crop(self):
        for scale_mode in (openshot.SCALE_FIT, openshot.SCALE_CROP):
            with self.subTest(scale_mode=scale_mode):
                left = self.rect_for(scale_mode, location_x=-1.0)
                right = self.rect_for(scale_mode, location_x=1.0)

                self.assertLessEqual(left.x() + left.width(), 0.0)
                self.assertGreaterEqual(right.x(), self.viewport.width())

    def test_yolo5_file_sha256(self):
        with tempfile.NamedTemporaryFile(delete=False) as test_file:
            test_file.write(b"openshot-yolov5")
            test_path = test_file.name
        try:
            self.assertEqual(
                ProcessEffect.file_sha256(None, test_path),
                "c54dd3b21ba1b2283d358605d1c7740ce50adc337b4682f5d96c954db4390337",
            )
        finally:
            os.remove(test_path)

    def test_yolo_manifest_has_packaged_recommended_default(self):
        manifest = load_yolo_models_manifest()
        recommended = [model for model in manifest["models"] if model.get("recommended")]

        self.assertEqual(len(recommended), 1)
        self.assertEqual(recommended[0]["id"], "yolo26n-seg")
        self.assertEqual(yolo_model_label(recommended[0]), "YOLO26: Nano (Recommended, fastest)")
        self.assertEqual(yolo_download_button_label(recommended[0]), "Download (10 MB)")
        self.assertTrue(yolo_model_path(recommended[0]).endswith("yolo26n-seg/model.onnx"))
        self.assertTrue(yolo_classes_path(recommended[0]).endswith("yolo26n-seg/classes.names"))

    def test_yolo5_validation_uses_libopenshot(self):
        process = ProcessEffect.__new__(ProcessEffect)
        process.onnx_validation_cache = {}

        with tempfile.NamedTemporaryFile(suffix=".onnx") as test_model, \
                patch("windows.process_effect.get_app") as get_app, \
                patch(
                    "windows.process_effect.openshot.ClipProcessingJobs.ValidateONNXModel",
                    return_value="",
                ) as validate:
            get_app.return_value = types.SimpleNamespace(_tr=lambda text: text)

            valid, message = ProcessEffect.validate_onnx_model_load(process, test_model.name)

        self.assertTrue(valid)
        self.assertEqual(message, "Ready")
        validate.assert_called_once_with(test_model.name)

    def test_yolo5_validation_reports_libopenshot_error(self):
        process = ProcessEffect.__new__(ProcessEffect)
        process.onnx_validation_cache = {}

        with tempfile.NamedTemporaryFile(suffix=".onnx") as test_model, \
                patch("windows.process_effect.get_app") as get_app, \
                patch(
                    "windows.process_effect.openshot.ClipProcessingJobs.ValidateONNXModel",
                    return_value="Failed to load ONNX model: bad graph",
                ):
            get_app.return_value = types.SimpleNamespace(_tr=lambda text: text)

            valid, message = ProcessEffect.validate_onnx_model_load(process, test_model.name)

        self.assertFalse(valid)
        self.assertEqual(message, "Failed to load ONNX model: bad graph")

    def test_object_detection_generate_masks_option_is_not_shown(self):
        generate_masks = [
            option for option in effect_options["ObjectDetection"]
            if option.get("setting") == "generate_masks"
        ]

        self.assertEqual(generate_masks, [])

    def test_process_effect_disables_editable_controls_while_processing(self):
        process = ProcessEffect.__new__(ProcessEffect)
        enabled_calls = {
            "scroll": [],
            "process": [],
            "cancel": [],
        }
        process.scrollArea = types.SimpleNamespace(setEnabled=enabled_calls["scroll"].append)
        process.process_button = types.SimpleNamespace(setEnabled=enabled_calls["process"].append)
        process.cancel_button = types.SimpleNamespace(setEnabled=enabled_calls["cancel"].append)

        ProcessEffect.set_processing_controls_enabled(process, False)

        self.assertEqual(enabled_calls["scroll"], [False])
        self.assertEqual(enabled_calls["process"], [False])
        self.assertEqual(enabled_calls["cancel"], [True])

    def test_yolo5_ssl_context_prefers_certifi_bundle(self):
        certifi_stub = types.SimpleNamespace(where=lambda: "/tmp/cacert.pem")
        context_stub = object()

        with patch.dict(sys.modules, {"certifi": certifi_stub}), \
                patch("classes.http_client.os.path.exists", return_value=True), \
                patch("classes.http_client.ssl.create_default_context", return_value=context_stub) as create_context:
            self.assertIs(http_client.ssl_context(), context_stub)

        create_context.assert_called_once_with(cafile="/tmp/cacert.pem")

    def test_download_url_to_file_streams_with_ssl_context_and_progress(self):
        class FakeResponse:
            headers = {"Content-Length": "11"}

            def __init__(self):
                self.chunks = [b"hello ", b"world", b""]

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size):
                return self.chunks.pop(0)

        progress = []
        context_stub = object()
        response = FakeResponse()

        with tempfile.NamedTemporaryFile(delete=False) as test_file:
            test_path = test_file.name
        try:
            with patch("classes.http_client.ssl_context", return_value=context_stub), \
                    patch("classes.http_client.urllib.request.urlopen", return_value=response) as urlopen:
                http_client.download_file(
                    "https://example.com/model.zip",
                    test_path,
                    "test model",
                    lambda downloaded, total: progress.append((downloaded, total)),
                )

            with open(test_path, "rb") as output_file:
                self.assertEqual(output_file.read(), b"hello world")
            self.assertEqual(progress, [(6, 11), (11, 11)])
            self.assertIs(urlopen.call_args.kwargs["context"], context_stub)
        finally:
            os.remove(test_path)

    def test_margin_box_norm_converts_effect_margins_to_region(self):
        raw = {
            "left": {"value": 0.10},
            "top": {"value": 0.20},
            "right": {"value": 0.30},
            "bottom": {"value": 0.40},
        }

        self.assertEqual(VideoWidget._margin_box_norm(raw), (0.10, 0.20, 0.70, 0.60))

    def test_margin_box_clamp_preserves_opposing_edge(self):
        left, top, right, bottom = VideoWidget._clamp_margin_values(
            0.90, 0.90, 0.30, 0.30, prefer_left=True, prefer_top=True)

        self.assertAlmostEqual(left, 0.70)
        self.assertAlmostEqual(top, 0.70)
        self.assertAlmostEqual(right, 0.30)
        self.assertAlmostEqual(bottom, 0.30)

    def test_effect_has_margin_box_from_class_name(self):
        self.widget.transforming_effect = None
        self.widget.transforming_effect_object = types.SimpleNamespace(
            info=types.SimpleNamespace(class_name="Blur"))

        self.assertTrue(VideoWidget._effect_has_margin_box(self.widget))

    def test_tracked_object_resolver_uses_evaluated_selected_index(self):
        self.widget.transforming_effect = types.SimpleNamespace(
            data={
                "selected_object_index": {
                    "Points": [{"co": {"X": 1, "Y": 0}}]
                }
            }
        )
        objects = {
            "effect-uuid-0": {"visible": {"value": 1}, "name": "zero"},
            "effect-uuid-2": {"visible": {"value": 1}, "name": "two"},
        }
        raw = {"selected_object_index": {"value": 2}}

        object_id, props = VideoWidget._resolve_tracked_object(self.widget, objects, raw)

        self.assertEqual(object_id, "effect-uuid-2")
        self.assertEqual(props["name"], "two")

    def test_tracked_object_resolver_normalizes_float_selected_index(self):
        self.widget.transforming_effect = None
        objects = {
            "effect-uuid-1": {"visible": {"value": 1}, "name": "one"},
        }
        raw = {"selected_object_index": {"value": 1.0}}

        object_id, props = VideoWidget._resolve_tracked_object(self.widget, objects, raw)

        self.assertEqual(object_id, "effect-uuid-1")
        self.assertEqual(props["name"], "one")

    def test_tracked_object_resolver_ignores_all_selection_for_preview_transform(self):
        self.widget.transforming_effect = None
        objects = {
            "all": {"visible": {"value": 1}, "name": "all objects"},
            "effect-uuid-3": {"visible": {"value": 1}, "name": "three"},
        }
        raw = {"selected_object_index": {"value": -1}}

        object_id, props = VideoWidget._resolve_tracked_object(self.widget, objects, raw)

        self.assertIsNone(object_id)
        self.assertIsNone(props)

    def test_tracked_object_resolver_fallback_skips_all_object(self):
        self.widget.transforming_effect = None
        objects = {
            "all": {"visible": {"value": 1}, "name": "all objects"},
            "effect-uuid-3": {"visible": {"value": 1}, "name": "three"},
        }

        object_id, props = VideoWidget._resolve_tracked_object(self.widget, objects, {})

        self.assertEqual(object_id, "effect-uuid-3")
        self.assertEqual(props["name"], "three")

    def test_update_effect_property_writes_only_changed_tracked_object_property(self):
        class FakeEffect:
            def __init__(self):
                self.data = {
                    "objects": {
                        "effect-uuid-23": {
                            "BaseFPS": {"den": 1, "num": 1},
                            "TimeScale": 1.0,
                            "box_id": "effect-uuid-23",
                            "delta_x": {"value": 0.0},
                            "delta_y": {"value": 0.0},
                            "visible": {"value": 1},
                            "x1": {"value": 0.25},
                        }
                    }
                }
                self.saved = False

            def save(self):
                self.saved = True

        effect = FakeEffect()
        self.widget.transaction_id = None

        with patch("windows.video_widget.Effect.get", return_value=effect):
            VideoWidget.updateEffectProperty(
                self.widget,
                "effect-uuid",
                5,
                "effect-uuid-23",
                "delta_x",
                0.25,
                refresh=False,
            )

        self.assertTrue(effect.saved)
        object_payload = effect.data["objects"]["effect-uuid-23"]
        self.assertEqual(["delta_x"], list(object_payload.keys()))
        points = object_payload["delta_x"]["Points"]
        self.assertEqual(points[0]["co"], {"X": 5, "Y": 0.25})

    def test_update_effect_properties_batches_tracked_object_properties(self):
        class FakeEffect:
            def __init__(self):
                self.data = {
                    "objects": {
                        "effect-uuid-23": {
                            "delta_x": {"Points": []},
                            "delta_y": {"Points": []},
                        }
                    }
                }
                self.save_count = 0

            def save(self):
                self.save_count += 1

        effect = FakeEffect()
        self.widget.transaction_id = None
        refresh_signal = FakeSignal()
        fake_app = types.SimpleNamespace(
            updates=types.SimpleNamespace(transaction_id=None),
            window=types.SimpleNamespace(refreshFrameSignal=refresh_signal),
        )

        with patch("windows.video_widget.Effect.get", return_value=effect), \
             patch("windows.video_widget.get_app", return_value=fake_app):
            VideoWidget.updateEffectProperties(
                self.widget,
                "effect-uuid",
                5,
                "effect-uuid-23",
                {"delta_x": 0.25, "delta_y": -0.5},
            )

        self.assertEqual(effect.save_count, 1)
        object_payload = effect.data["objects"]["effect-uuid-23"]
        self.assertEqual(["delta_x", "delta_y"], list(object_payload.keys()))
        self.assertEqual(object_payload["delta_x"]["Points"][0]["co"], {"X": 5, "Y": 0.25})
        self.assertEqual(object_payload["delta_y"]["Points"][0]["co"], {"X": 5, "Y": -0.5})
        self.assertEqual(refresh_signal.count, 1)

    def test_update_effect_property_refreshes_preview_for_tracked_object(self):
        class FakeEffect:
            def __init__(self):
                self.data = {"objects": {"effect-uuid-23": {"delta_x": {"Points": []}}}}
                self.saved = False

            def save(self):
                self.saved = True

        effect = FakeEffect()
        refresh_signal = FakeSignal()
        fake_app = types.SimpleNamespace(
            updates=types.SimpleNamespace(transaction_id=None),
            window=types.SimpleNamespace(refreshFrameSignal=refresh_signal),
        )
        self.widget.transaction_id = None

        with patch("windows.video_widget.Effect.get", return_value=effect), \
             patch("windows.video_widget.get_app", return_value=fake_app):
            VideoWidget.updateEffectProperty(
                self.widget,
                "effect-uuid",
                5,
                "effect-uuid-23",
                "delta_x",
                0.25,
            )

        self.assertTrue(effect.saved)
        self.assertEqual(refresh_signal.count, 1)

    def test_tracked_object_color_update_initializes_sparse_object_color(self):
        class FakeEffect:
            def __init__(self):
                self.data = {
                    "objects": {
                        "effect-uuid-23": {
                            "delta_x": {"Points": []},
                        }
                    }
                }
                self.saved = False

            def save(self):
                self.saved = True

        effect = FakeEffect()
        model = ClipStandardItemModel()
        label_item = QStandardItem("Border")
        value_item = QStandardItem("")
        label_item.setData((
            "stroke",
            {
                "type": "color",
                "closest_point_x": 1,
                "previous_point_x": 1,
                "object_id": "effect-uuid-23",
                "red": {"value": 62, "Points": []},
                "green": {"value": 143, "Points": []},
                "blue": {"value": 0, "Points": []},
            }
        ))
        value_item.setData([("effect-uuid", "effect")])
        model.appendRow([label_item, value_item])

        helper = PropertiesModel.__new__(PropertiesModel)
        helper.model = model
        helper.frame_number = 5
        helper.parent = FakePropertiesParent(model)
        helper._trim_preview_mode = False
        helper.ignore_update_signal = False
        fake_app = types.SimpleNamespace(
            window=types.SimpleNamespace(refreshFrameSignal=FakeSignal()),
            _tr=lambda text: text,
        )

        with patch("windows.models.properties_model.Effect.get", return_value=effect), \
             patch("windows.models.properties_model.get_app", return_value=fake_app):
            PropertiesModel.color_update(helper, value_item, QColor(10, 20, 30, 255))

        self.assertTrue(effect.saved)
        self.assertEqual(["objects"], list(effect.data.keys()))
        object_payload = effect.data["objects"]["effect-uuid-23"]
        self.assertEqual(["stroke"], list(object_payload.keys()))
        self.assertEqual(
            object_payload["stroke"]["red"]["Points"][0]["co"],
            {"X": 5, "Y": 10},
        )
        self.assertEqual(
            object_payload["stroke"]["green"]["Points"][0]["co"],
            {"X": 5, "Y": 20},
        )
        self.assertEqual(
            object_payload["stroke"]["blue"]["Points"][0]["co"],
            {"X": 5, "Y": 30},
        )

    def test_tracked_object_property_editor_update_writes_narrow_object_payload(self):
        class FakeEffect:
            def __init__(self):
                self.data = {
                    "objects": {
                        "effect-uuid-23": {
                            "delta_x": {"Points": [{"co": {"X": 5, "Y": 0.0}, "interpolation": 1}]},
                            "delta_y": {"Points": [{"co": {"X": 5, "Y": 0.0}, "interpolation": 1}]},
                        },
                        "effect-uuid-24": {
                            "delta_x": {"Points": [{"co": {"X": 5, "Y": 0.5}, "interpolation": 1}]},
                        },
                    }
                }
                self.saved = False

            def save(self):
                self.saved = True

        effect = FakeEffect()
        model = ClipStandardItemModel()
        label_item = QStandardItem("Displacement X-axis")
        value_item = QStandardItem("")
        label_item.setData((
            "delta_x",
            {
                "type": "float",
                "closest_point_x": 5,
                "previous_point_x": 5,
                "object_id": "effect-uuid-23",
                "choices": [],
            }
        ))
        value_item.setData([("effect-uuid", "effect")])
        model.appendRow([label_item, value_item])

        helper = PropertiesModel.__new__(PropertiesModel)
        helper.model = model
        helper.frame_number = 5
        helper.parent = FakePropertiesParent(model)
        helper._trim_preview_mode = False
        helper.ignore_update_signal = False
        fake_app = types.SimpleNamespace(
            window=types.SimpleNamespace(refreshFrameSignal=FakeSignal()),
            _tr=lambda text: text,
        )

        with patch("windows.models.properties_model.Effect.get", return_value=effect), \
             patch("windows.models.properties_model.get_app", return_value=fake_app):
            PropertiesModel.value_updated(helper, value_item, value=0.25)

        self.assertTrue(effect.saved)
        self.assertEqual(["objects"], list(effect.data.keys()))
        self.assertEqual(["effect-uuid-23"], list(effect.data["objects"].keys()))
        object_payload = effect.data["objects"]["effect-uuid-23"]
        self.assertEqual(["delta_x"], list(object_payload.keys()))
        self.assertEqual(
            object_payload["delta_x"]["Points"][0]["co"],
            {"X": 5, "Y": 0.25},
        )

    def test_all_tracked_objects_property_update_writes_all_payload(self):
        class FakeEffect:
            def __init__(self):
                self.data = {
                    "objects": {
                        "effect-uuid-23": {
                            "background_alpha": {"Points": [{"co": {"X": 1, "Y": 0.15}, "interpolation": 1}]},
                            "delta_x": {"Points": [{"co": {"X": 1, "Y": 0.0}, "interpolation": 1}]},
                            "stroke": {
                                "red": {"Points": [{"co": {"X": 1, "Y": 10}, "interpolation": 1}]},
                                "green": {"Points": [{"co": {"X": 1, "Y": 20}, "interpolation": 1}]},
                                "blue": {"Points": [{"co": {"X": 1, "Y": 30}, "interpolation": 1}]},
                            },
                        },
                        "effect-uuid-24": {
                            "background_alpha": {"Points": [{"co": {"X": 1, "Y": 0.15}, "interpolation": 1}]},
                        },
                    }
                }
                self.saved = False

            def save(self):
                self.saved = True

        effect = FakeEffect()
        model = ClipStandardItemModel()
        label_item = QStandardItem("Background Alpha")
        value_item = QStandardItem("")
        label_item.setData((
            "background_alpha",
            {
                "type": "float",
                "closest_point_x": 1,
                "previous_point_x": 1,
                "object_id": "all",
                "choices": [],
            }
        ))
        value_item.setData([("effect-uuid", "effect")])
        model.appendRow([label_item, value_item])

        helper = PropertiesModel.__new__(PropertiesModel)
        helper.model = model
        helper.frame_number = 1
        helper.parent = FakePropertiesParent(model)
        helper._trim_preview_mode = False
        helper.ignore_update_signal = False
        fake_app = types.SimpleNamespace(
            window=types.SimpleNamespace(refreshFrameSignal=FakeSignal()),
            _tr=lambda text: text,
            project=types.SimpleNamespace(get=lambda key: {"num": 30, "den": 1}),
        )

        with patch("windows.models.properties_model.Effect.get", return_value=effect), \
             patch("windows.models.properties_model.get_app", return_value=fake_app):
            PropertiesModel.value_updated(helper, value_item, value=0.35)

        self.assertTrue(effect.saved)
        self.assertEqual(["objects"], list(effect.data.keys()))
        self.assertIn("all", effect.data["objects"])
        self.assertIn("effect-uuid-23", effect.data["objects"])
        self.assertIn("effect-uuid-24", effect.data["objects"])
        object_payload = effect.data["objects"]["all"]
        self.assertEqual(["background_alpha"], list(object_payload.keys()))
        self.assertEqual(
            object_payload["background_alpha"]["Points"][0]["co"],
            {"X": 1, "Y": 0.35},
        )
        self.assertEqual(
            effect.data["objects"]["effect-uuid-23"]["background_alpha"]["Points"][0]["co"],
            {"X": 1, "Y": 0.35},
        )

    def test_all_tracked_objects_transform_update_writes_all_payload(self):
        class FakeEffect:
            def __init__(self):
                self.data = {
                    "objects": {
                        "effect-uuid-23": {
                            "delta_x": {"Points": [{"co": {"X": 5, "Y": 0.0}, "interpolation": 1}]},
                        },
                        "effect-uuid-24": {
                            "delta_x": {"Points": [{"co": {"X": 5, "Y": 0.25}, "interpolation": 1}]},
                        },
                    }
                }
                self.saved = False

            def save(self):
                self.saved = True

        effect = FakeEffect()
        model = ClipStandardItemModel()
        label_item = QStandardItem("Displacement X-axis")
        value_item = QStandardItem("")
        label_item.setData((
            "delta_x",
            {
                "type": "float",
                "closest_point_x": 5,
                "previous_point_x": 5,
                "object_id": "all",
                "choices": [],
            }
        ))
        value_item.setData([("effect-uuid", "effect")])
        model.appendRow([label_item, value_item])

        helper = PropertiesModel.__new__(PropertiesModel)
        helper.model = model
        helper.frame_number = 5
        helper.parent = FakePropertiesParent(model)
        helper._trim_preview_mode = False
        helper.ignore_update_signal = False
        fake_app = types.SimpleNamespace(
            window=types.SimpleNamespace(refreshFrameSignal=FakeSignal()),
            _tr=lambda text: text,
            project=types.SimpleNamespace(get=lambda key: {"num": 30, "den": 1}),
        )

        with patch("windows.models.properties_model.Effect.get", return_value=effect), \
             patch("windows.models.properties_model.get_app", return_value=fake_app):
            PropertiesModel.value_updated(helper, value_item, value=0.5)

        self.assertTrue(effect.saved)
        self.assertEqual(["objects"], list(effect.data.keys()))
        self.assertIn("all", effect.data["objects"])
        self.assertIn("effect-uuid-23", effect.data["objects"])
        self.assertIn("effect-uuid-24", effect.data["objects"])
        object_payload = effect.data["objects"]["all"]
        self.assertEqual(["delta_x"], list(object_payload.keys()))
        self.assertEqual(
            object_payload["delta_x"]["Points"][0]["co"],
            {"X": 5, "Y": 0.5},
        )
        self.assertEqual(
            effect.data["objects"]["effect-uuid-24"]["delta_x"]["Points"][0]["co"],
            {"X": 5, "Y": 0.5},
        )

    def test_all_tracked_objects_color_update_writes_all_payload(self):
        class FakeEffect:
            def __init__(self):
                self.data = {
                    "objects": {
                        "effect-uuid-23": {
                            "stroke": {
                                "red": {"Points": [{"co": {"X": 1, "Y": 10}, "interpolation": 1}]},
                                "green": {"Points": [{"co": {"X": 1, "Y": 20}, "interpolation": 1}]},
                                "blue": {"Points": [{"co": {"X": 1, "Y": 30}, "interpolation": 1}]},
                            },
                        }
                    }
                }
                self.saved = False

            def save(self):
                self.saved = True

        effect = FakeEffect()
        model = ClipStandardItemModel()
        label_item = QStandardItem("Border")
        value_item = QStandardItem("")
        label_item.setData((
            "stroke",
            {
                "type": "color",
                "closest_point_x": 1,
                "previous_point_x": 1,
                "object_id": "all",
                "red": {"value": 10, "Points": []},
                "green": {"value": 20, "Points": []},
                "blue": {"value": 30, "Points": []},
            }
        ))
        value_item.setData([("effect-uuid", "effect")])
        model.appendRow([label_item, value_item])

        helper = PropertiesModel.__new__(PropertiesModel)
        helper.model = model
        helper.frame_number = 1
        helper.parent = FakePropertiesParent(model)
        helper._trim_preview_mode = False
        helper.ignore_update_signal = False
        fake_app = types.SimpleNamespace(
            window=types.SimpleNamespace(refreshFrameSignal=FakeSignal()),
            _tr=lambda text: text,
        )

        with patch("windows.models.properties_model.Effect.get", return_value=effect), \
             patch("windows.models.properties_model.get_app", return_value=fake_app):
            PropertiesModel.color_update(helper, value_item, QColor(100, 110, 120, 255))

        self.assertTrue(effect.saved)
        self.assertEqual(["objects"], list(effect.data.keys()))
        self.assertIn("all", effect.data["objects"])
        self.assertIn("effect-uuid-23", effect.data["objects"])
        object_payload = effect.data["objects"]["all"]
        self.assertEqual(["stroke"], list(object_payload.keys()))
        self.assertEqual(object_payload["stroke"]["red"]["Points"][0]["co"], {"X": 1, "Y": 100})
        self.assertEqual(object_payload["stroke"]["green"]["Points"][0]["co"], {"X": 1, "Y": 110})
        self.assertEqual(object_payload["stroke"]["blue"]["Points"][0]["co"], {"X": 1, "Y": 120})
        self.assertEqual(effect.data["objects"]["effect-uuid-23"]["stroke"]["red"]["Points"][0]["co"], {"X": 1, "Y": 100})

    def test_all_tracked_objects_update_saves_only_edited_property(self):
        class FakeEffect:
            def __init__(self):
                self.data = {
                    "objects": {
                        "all": {
                            "stroke_width": {"Points": [{"co": {"X": 1, "Y": 8.0}, "interpolation": 1}]},
                            "stroke_alpha": {"Points": [{"co": {"X": 1, "Y": 0.7}, "interpolation": 1}]},
                        },
                    }
                }
                self.saved = False

            def save(self):
                self.saved = True

        effect = FakeEffect()
        model = ClipStandardItemModel()
        label_item = QStandardItem("Stroke alpha")
        value_item = QStandardItem("")
        label_item.setData((
            "stroke_alpha",
            {
                "type": "float",
                "closest_point_x": 1,
                "previous_point_x": 1,
                "object_id": "all",
                "choices": [],
            }
        ))
        value_item.setData([("effect-uuid", "effect")])
        model.appendRow([label_item, value_item])

        helper = PropertiesModel.__new__(PropertiesModel)
        helper.model = model
        helper.frame_number = 1
        helper.parent = FakePropertiesParent(model)
        helper._trim_preview_mode = False
        helper.ignore_update_signal = False
        fake_app = types.SimpleNamespace(
            window=types.SimpleNamespace(refreshFrameSignal=FakeSignal()),
            _tr=lambda text: text,
            project=types.SimpleNamespace(get=lambda key: {"num": 30, "den": 1}),
        )

        with patch("windows.models.properties_model.Effect.get", return_value=effect), \
             patch("windows.models.properties_model.get_app", return_value=fake_app):
            PropertiesModel.value_updated(helper, value_item, value=0.25)

        object_payload = effect.data["objects"]["all"]
        self.assertEqual(["stroke_alpha"], list(object_payload.keys()))
        self.assertEqual(object_payload["stroke_alpha"]["Points"][0]["co"], {"X": 1, "Y": 0.25})

    def test_all_tracked_objects_update_seeds_missing_keyframe_property(self):
        class FakeEffect:
            def __init__(self):
                self.data = {
                    "objects": {
                        "all": {
                            "stroke_width": {"Points": [{"co": {"X": 1, "Y": 8.0}, "interpolation": 1}]},
                        },
                        "effect-uuid-23": {
                            "stroke_alpha": {"Points": [{"co": {"X": 1, "Y": 0.7}, "interpolation": 1}]},
                        },
                    }
                }
                self.saved = False

            def save(self):
                self.saved = True

        effect = FakeEffect()
        model = ClipStandardItemModel()
        label_item = QStandardItem("Stroke alpha")
        value_item = QStandardItem("")
        label_item.setData((
            "stroke_alpha",
            {
                "type": "float",
                "closest_point_x": 1,
                "previous_point_x": 1,
                "object_id": "all",
                "choices": [],
                "Points": [{"co": {"X": 1, "Y": 0.7}, "interpolation": 1}],
            }
        ))
        value_item.setData([("effect-uuid", "effect")])
        model.appendRow([label_item, value_item])

        helper = PropertiesModel.__new__(PropertiesModel)
        helper.model = model
        helper.frame_number = 1
        helper.parent = FakePropertiesParent(model)
        helper._trim_preview_mode = False
        helper.ignore_update_signal = False
        fake_app = types.SimpleNamespace(
            window=types.SimpleNamespace(refreshFrameSignal=FakeSignal()),
            _tr=lambda text: text,
            project=types.SimpleNamespace(get=lambda key: {"num": 30, "den": 1}),
        )

        with patch("windows.models.properties_model.Effect.get", return_value=effect), \
             patch("windows.models.properties_model.get_app", return_value=fake_app):
            PropertiesModel.value_updated(helper, value_item, value=0.25)

        object_payload = effect.data["objects"]["all"]
        self.assertEqual(["stroke_alpha"], list(object_payload.keys()))
        self.assertIsInstance(object_payload["stroke_alpha"], dict)
        self.assertEqual(object_payload["stroke_alpha"]["Points"][0]["co"], {"X": 1, "Y": 0.25})

    def test_tracked_object_transform_modes_exclude_origin_and_rotation(self):
        QWidget.__init__(self.widget)
        self.widget.transform = QTransform()
        self.widget.mouse_dragging = False
        self.widget.transform_mode = None
        self.widget.resize_button = types.SimpleNamespace(isVisible=lambda: False)
        self.widget.cursors = {}
        self.widget.transforming_effect = types.SimpleNamespace(data={})
        self.widget.transforming_effect_object = types.SimpleNamespace(
            info=types.SimpleNamespace(has_tracked_object=True, class_name="ObjectDetection")
        )
        self.widget.centerHandle = QRectF(44, 44, 12, 12)
        self.widget.topLeftHandle = QRectF(0, 0, 12, 12)
        self.widget.topRightHandle = QRectF(88, 0, 12, 12)
        self.widget.bottomLeftHandle = QRectF(0, 88, 12, 12)
        self.widget.bottomRightHandle = QRectF(88, 88, 12, 12)
        self.widget.topHandle = QRectF(44, 0, 12, 12)
        self.widget.bottomHandle = QRectF(44, 88, 12, 12)
        self.widget.leftHandle = QRectF(0, 44, 12, 12)
        self.widget.rightHandle = QRectF(88, 44, 12, 12)
        self.widget.topShearHandle = QRectF(0, 0, 100, 12)
        self.widget.leftShearHandle = QRectF(0, 0, 12, 100)
        self.widget.rightShearHandle = QRectF(88, 0, 12, 100)
        self.widget.bottomShearHandle = QRectF(0, 88, 100, 12)
        self.widget.clipBounds = QRectF(0, 0, 100, 100)

        VideoWidget.checkTransformMode(self.widget, 0, 0, 0, mouse_event_at(50, 50))
        self.assertEqual(self.widget.hover_transform_mode, "location")

        VideoWidget.checkTransformMode(self.widget, 0, 0, 0, mouse_event_at(150, 150))
        self.assertIsNone(self.widget.hover_transform_mode)
        self.assertEqual(self.widget.hover_cursor, Qt.ArrowCursor)


if __name__ == "__main__":
    unittest.main()
