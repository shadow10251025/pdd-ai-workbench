import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from ai_review import persist_review_package, regenerate_module
from ai_workflow import WorkflowError


class ModuleReviewTests(unittest.TestCase):
    def package(self, root, kind="detail", module_type="scene"):
        job = "a" * 16
        directory = root / "data" / "ai_jobs" / job
        directory.mkdir(parents=True)
        images = []
        for index, color in enumerate(("red", "blue"), start=1):
            path = directory / f"input_{index}.jpg"
            Image.new("RGB", (640, 640), color).save(path)
            images.append(path.relative_to(root).as_posix())
        generated = directory / "detail.jpg"
        Image.new("RGB", (750, 1000), "white").save(generated)
        rel = generated.relative_to(root).as_posix()
        result = {
            "job_id": job, "generation_kind": "offline_demo", "style": "fresh",
            "source_images": images, "main_images": [], "detail_images": [rel],
            "spec_images": {"blue": rel} if kind == "spec" else {},
            "metadata": {}, "prompt_pack": {"style_lock": "Campaign Style Lock: #FFFFFF"},
            "module_outputs": [{"id": "detail_001", "kind": kind, "module_type": module_type, "path": rel, "prompt": "One useful product photo", "reference_indices": [2], "spec_value": "blue" if kind == "spec" else ""}],
            "size_chart_rows": [{"size": "S", "measurements": {"chest": "100 cm"}}],
            "image_api_key": "do-not-save", "metadata_extra": {"prompt_api_key": "nested-secret"},
        }
        persist_review_package(root, result)
        return result

    def fake_sheet(self, config, prompt, reference, out_path, rows, cols, progress, trace, refs):
        self.assertEqual((rows, cols), (1, 1))
        self.assertEqual(len(refs), 1)
        self.assertGreater(refs[0].getpixel((2, 2))[2], 240)
        self.assertTrue(prompt.startswith("Campaign Style Lock"))
        trace.update({"mode": "offline", "reference_sent": False})
        image = Image.new("RGB", (750, 1000), "green")
        image.save(out_path)
        return image

    def test_one_module_revision_preserves_source_and_uses_bound_reference(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.package(root)
            original_bytes = (root / source["detail_images"][0]).read_bytes()
            with patch.dict(os.environ, {"PDD_AI_PROVIDER": "mock", "PDD_IMAGE_API_URL": "", "PDD_IMAGE_API_KEY": ""}), patch("ai_workflow.generate_sheet", side_effect=self.fake_sheet) as generate:
                result = regenerate_module(root, source["job_id"], "detail_001", "A new natural scene")
            self.assertEqual(generate.call_count, 1)
            self.assertNotEqual(result["job_id"], source["job_id"])
            self.assertEqual(result["parent_job_id"], source["job_id"])
            self.assertEqual(result["provider_calls"], 0)
            self.assertEqual(len(result["detail_images"]), 1)
            self.assertTrue((root / result["detail_images"][0]).is_file())
            self.assertEqual((root / source["detail_images"][0]).read_bytes(), original_bytes)
            serialized = (root / "data" / "ai_jobs" / result["job_id"] / "review_package.json").read_text(encoding="utf-8")
            self.assertNotIn("do-not-save", serialized)
            self.assertNotIn("nested-secret", serialized)

    def test_sku_revision_stays_square_and_updates_spec_mapping(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.package(root, kind="spec", module_type="sku_preview")
            with patch.dict(os.environ, {"PDD_AI_PROVIDER": "mock", "PDD_IMAGE_API_URL": "", "PDD_IMAGE_API_KEY": ""}), patch("ai_workflow.generate_sheet", side_effect=self.fake_sheet):
                result = regenerate_module(root, source["job_id"], "detail_001")
            path = root / result["spec_images"]["blue"]
            with Image.open(path) as image:
                self.assertEqual(image.width, image.height)

    def test_size_chart_revision_does_not_call_image_provider(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.package(root, module_type="size_chart")
            with patch("ai_workflow.generate_sheet") as generate:
                result = regenerate_module(root, source["job_id"], "detail_001")
            generate.assert_not_called()
            self.assertEqual(result["provider_calls"], 0)
            self.assertTrue((root / result["detail_images"][0]).is_file())

    def test_job_path_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(WorkflowError):
                regenerate_module(Path(temporary), "../../private", "detail_001", "test")

    def test_missing_module_is_rejected_without_generation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.package(root)
            with patch("ai_workflow.generate_sheet") as generate:
                with self.assertRaises(WorkflowError):
                    regenerate_module(root, source["job_id"], "does-not-exist", "test")
            generate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
