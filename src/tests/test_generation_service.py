import os
import sys
import types
import unittest
from unittest.mock import patch


PATH = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if PATH not in sys.path:
    sys.path.append(PATH)

TEST_MEDIA_ROOT = os.path.join(os.sep, "mock-media")

generate_module = types.ModuleType("windows.generate")
generate_module.GenerateMediaDialog = type("GenerateMediaDialog", (), {})
sys.modules.setdefault("windows.generate", generate_module)

from classes.generation_service import GenerationService


class _StatusBarRecorder:
    def __init__(self):
        self.calls = []

    def showMessage(self, text, timeout):
        self.calls.append((text, timeout))


class _QueueStub:
    def __init__(self):
        self.jobs = {}


class GenerationServiceTests(unittest.TestCase):
    def test_split_generation_suffix_handles_legacy_and_current_formats(self):
        service = GenerationService.__new__(GenerationService)

        self.assertEqual(service._split_generation_suffix("alpha_gen"), ("alpha", 1))
        self.assertEqual(service._split_generation_suffix("alpha_gen1"), ("alpha", 1))
        self.assertEqual(service._split_generation_suffix("alpha_gen_001"), ("alpha", 1))
        self.assertEqual(service._split_generation_suffix("alpha_gen#2"), ("alpha", 2))
        self.assertEqual(service._split_generation_suffix("alpha"), ("alpha", None))

    def test_default_generation_name_uses_gen_suffix_and_increments_from_legacy_names(self):
        service = GenerationService.__new__(GenerationService)

        existing_files = [
            types.SimpleNamespace(data={"name": "p232_229_gen1", "path": os.path.join(TEST_MEDIA_ROOT, "p232_229_gen1.flac")}),
            types.SimpleNamespace(data={"name": "p232_229_gen#2 [Noise: Remove]", "path": os.path.join(TEST_MEDIA_ROOT, "p232_229_gen#2.flac")}),
        ]
        with patch("classes.generation_service.File.filter", return_value=existing_files):
            file_obj = types.SimpleNamespace(data={"path": os.path.join(TEST_MEDIA_ROOT, "p232_229_gen_001.flac")})
            self.assertEqual(service._default_generation_name(file_obj), "p232_229_gen3")

            file_obj = types.SimpleNamespace(data={"path": os.path.join(TEST_MEDIA_ROOT, "afdr001_30s.wav")})
            self.assertEqual(service._default_generation_name(file_obj), "afdr001_30s_gen1")

    def test_default_generation_name_prefers_display_name_over_collision_adjusted_path(self):
        service = GenerationService.__new__(GenerationService)

        existing_files = [
            types.SimpleNamespace(
                data={
                    "name": "generation_gen1 [Noise: Remove]",
                    "path": os.path.join(TEST_MEDIA_ROOT, "generation_gen1_2.png"),
                }
            ),
        ]
        with patch("classes.generation_service.File.filter", return_value=existing_files):
            file_obj = types.SimpleNamespace(
                data={
                    "name": "generation_gen1 [Noise: Remove]",
                    "path": os.path.join(TEST_MEDIA_ROOT, "generation_gen1_2.png"),
                }
            )
            self.assertEqual(service._default_generation_name(file_obj), "generation_gen2")

    def test_next_generation_name_preserves_custom_names_and_normalizes_generation_names(self):
        service = GenerationService.__new__(GenerationService)

        existing_files = [
            types.SimpleNamespace(data={"name": "alpha_gen1", "path": os.path.join(TEST_MEDIA_ROOT, "alpha_gen1.flac")}),
            types.SimpleNamespace(data={"name": "alpha_gen2 [Noise: Remove]", "path": os.path.join(TEST_MEDIA_ROOT, "alpha_gen2.flac")}),
            types.SimpleNamespace(data={"name": "custom_name", "path": os.path.join(TEST_MEDIA_ROOT, "custom_name.flac")}),
        ]
        with patch("classes.generation_service.File.filter", return_value=existing_files):
            self.assertEqual(service._next_generation_name("alpha_gen#2"), "alpha_gen3")
            self.assertEqual(service._next_generation_name("alpha_gen"), "alpha_gen3")
            self.assertEqual(service._next_generation_name("fresh_custom"), "fresh_custom")
            self.assertEqual(service._next_generation_name("custom_name"), "custom_name_gen1")

    def test_next_generation_name_considers_already_queued_jobs(self):
        service = GenerationService.__new__(GenerationService)
        service.win = types.SimpleNamespace(generation_queue=_QueueStub())
        service.win.generation_queue.jobs = {
            "J1": {"name": "generation_gen1"},
            "J2": {"name": "generation_gen2"},
        }
        with patch("classes.generation_service.File.filter", return_value=[]):
            self.assertEqual(service._next_generation_name("generation_gen1"), "generation_gen3")
            self.assertEqual(service._next_generation_name("generation"), "generation_gen3")

    def test_workflow_display_label_uses_tight_variant(self):
        service = GenerationService.__new__(GenerationService)
        self.assertEqual(
            service._workflow_display_label({"menu_parent": "noise", "display_name": "Remove"}),
            "Noise: Remove",
        )
        self.assertEqual(
            service._workflow_display_label({"menu_parent": "track_object", "display_name": "Highlight..."}),
            "Track: Highlight",
        )
        self.assertEqual(
            service._workflow_display_label({"display_name": "Increase Resolution (4x)"}),
            "Increase Resolution (4x)",
        )

    def test_source_display_root_name_strips_workflow_suffix(self):
        service = GenerationService.__new__(GenerationService)

        file_obj = types.SimpleNamespace(data={"name": "alpha_gen2 [Noise: Remove]", "path": os.path.join(TEST_MEDIA_ROOT, "alpha_gen2.flac")})
        self.assertEqual(service._source_display_root_name(file_obj), "alpha")

        file_obj = types.SimpleNamespace(data={"name": "Bravo Custom [Clarity: Speech]", "path": os.path.join(TEST_MEDIA_ROOT, "bravo.wav")})
        self.assertEqual(service._source_display_root_name(file_obj), "Bravo Custom")

    def test_output_local_name_omits_index_for_single_output(self):
        service = GenerationService.__new__(GenerationService)

        self.assertEqual(service._output_local_name("alpha_gen", 1, 1, ".flac"), "alpha_gen.flac")
        self.assertEqual(service._output_local_name("alpha_gen", 1, 2, ".flac"), "alpha_gen_001.flac")
        self.assertEqual(service._output_local_name("alpha_gen", 2, 2, ".flac"), "alpha_gen_002.flac")

    def test_action_generate_trigger_queues_all_selected_files_for_quick_actions(self):
        files = [
            types.SimpleNamespace(id="F1", data={"path": os.path.join(TEST_MEDIA_ROOT, "alpha.wav")}),
            types.SimpleNamespace(id="F2", data={"path": os.path.join(TEST_MEDIA_ROOT, "bravo.wav")}),
            types.SimpleNamespace(id="F3", data={"path": os.path.join(TEST_MEDIA_ROOT, "charlie.wav")}),
        ]
        status_bar = _StatusBarRecorder()
        queued_targets = []

        service = GenerationService.__new__(GenerationService)
        service.win = types.SimpleNamespace(
            selected_files=lambda: list(files),
            statusBar=status_bar,
        )
        service.template_registry = types.SimpleNamespace()
        service.is_comfy_available = lambda force=False: True
        service.templates_for_context = lambda source_file=None: [{"id": "audio-noise-remove"}]
        service._selected_generation_targets = lambda source_file=None: list(files)
        service._enqueue_generation_for_file = (
            lambda source_file, payload: queued_targets.append((source_file.id if source_file else None, dict(payload))) or (True, "")
        )
        with patch("classes.generation_service.File.filter", return_value=[]):
            service.action_generate_trigger(
                source_file=files[0],
                template_id="audio-noise-remove",
                open_dialog=False,
            )

        self.assertEqual([target_id for target_id, _ in queued_targets], ["F1", "F2", "F3"])
        self.assertEqual(status_bar.calls, [("Queued 3 generation jobs", 3000)])
        self.assertTrue(all(payload["template_id"] == "audio-noise-remove" for _, payload in queued_targets))
        self.assertEqual(
            [payload["name"] for _, payload in queued_targets],
            ["alpha_gen1", "bravo_gen1", "charlie_gen1"],
        )

    def test_action_generate_trigger_create_workflow_reserves_names_across_queued_jobs(self):
        status_bar = _StatusBarRecorder()
        queued_payload_names = []
        queue = _QueueStub()

        service = GenerationService.__new__(GenerationService)
        service.win = types.SimpleNamespace(
            selected_files=lambda: [],
            statusBar=status_bar,
            generation_queue=queue,
        )
        service.template_registry = types.SimpleNamespace()
        service.is_comfy_available = lambda force=False: True
        service.templates_for_context = lambda source_file=None: [{"id": "txt2img-basic"}]
        service._selected_generation_targets = lambda source_file=None: []

        def enqueue_generation_for_file(_source_file, payload):
            queued_payload_names.append(payload["name"])
            queue.jobs[f"J{len(queue.jobs) + 1}"] = {"name": payload["name"]}
            return True, ""

        service._enqueue_generation_for_file = enqueue_generation_for_file

        with patch("classes.generation_service.File.filter", return_value=[]):
            service.action_generate_trigger(template_id="txt2img-basic", open_dialog=False)
            service.action_generate_trigger(template_id="txt2img-basic", open_dialog=False)
            service.action_generate_trigger(template_id="txt2img-basic", open_dialog=False)

        self.assertEqual(
            queued_payload_names,
            ["generation_gen1", "generation_gen2", "generation_gen3"],
        )


if __name__ == "__main__":
    unittest.main()
