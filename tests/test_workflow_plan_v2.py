"""End-to-end regressions for the editable image-pack workbench.

These tests exercise produced files and request traces, not only the plan
helpers.  Every provider call is replaced with a small deterministic sheet;
the suite never sends photos or credentials to an external service.
"""

from __future__ import annotations

import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

import ai_workflow as workflow


class WorkflowPlanV2Tests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.raw_images = []
        for color in ((181, 55, 61), (42, 92, 161)):
            image = Image.new("RGB", (640, 640), color)
            ImageDraw.Draw(image).rectangle((120, 80, 440, 560), fill=(240, 240, 235))
            output = io.BytesIO()
            image.save(output, "JPEG")
            self.raw_images.append(output.getvalue())
        self.calls = []
        self.metadata = workflow._validate_metadata({
            "title": "针织上衣",
            "specs": {"颜色": ["红色", "蓝色"], "尺码": ["S", "M", "L"]},
            "identity_evidence": ["可见完整衣身轮廓和针织纹理"],
            "scene_suggestions": ["自然光居家", "周末咖啡馆"],
            "selling_points": ["针织纹理"],
        }, "针织上衣")
        self.env_patch = patch.dict(os.environ, {
            "PDD_AI_PROVIDER": "mock", "PDD_AI_DEMO": "",
            "PDD_IMAGE_API_URL": "", "PDD_IMAGE_API_KEY": "",
            "PDD_PROMPT_API_URL": "", "PDD_PROMPT_API_KEY": "",
            "PDD_AI_IMAGE_CACHE": "0",
        }, clear=False)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def _sheet(self, config, prompt, input_image, out_path, rows=2, cols=3,
               progress=None, trace=None, reference_images=None):
        self.calls.append({"prompt": prompt, "rows": rows, "cols": cols,
                           "reference_count": len(reference_images or [])})
        sheet = Image.new("RGB", (cols * 520, rows * 520), "white")
        draw = ImageDraw.Draw(sheet)
        call_index = len(self.calls)
        for index in range(rows * cols):
            x, y = (index % cols) * 520, (index // cols) * 520
            color = ((call_index * 53 + index * 29) % 255,
                     (call_index * 97 + index * 61) % 255,
                     (call_index * 31 + index * 43) % 255)
            draw.rectangle((x + 24, y + 24, x + 496, y + 496), fill=color)
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        sheet.save(out_path, "JPEG")
        if trace is not None:
            trace.update({"mode": "offline", "reference_sent": False,
                          "endpoint_kind": "none"})
        return sheet

    def _run(self, **overrides):
        args = dict(
            image_bytes=self.raw_images,
            filename=["red.jpg", "blue.jpg"],
            product_hint="女款针织上衣", style="fresh", rows=3, cols=3,
            project_dir=self.root, strategy="quality", generation_rounds=2,
            auto_module_plan=False,
        )
        args.update(overrides)
        with patch.object(workflow, "generate_sheet", side_effect=self._sheet), \
             patch.object(workflow, "generate_metadata", side_effect=lambda *a, **k: copy.deepcopy(self.metadata)):
            return workflow.run_workflow(**args)

    def _outputs(self, result, kind):
        return [item for item in result["module_outputs"] if item.get("kind") == kind]

    def test_economy_three_rounds_matches_actual_requests_and_selected_count(self):
        result = self._run(strategy="grid", generation_rounds=3,
                           detail_modules=[{"type": "scene", "count": 6}])
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(result["generation_rounds"], len(result["generation_trace"]))
        self.assertEqual(len(result["detail_images"]), 6)
        self.assertEqual(len(self._outputs(result, "detail")), 6)

    def test_twenty_selected_details_are_not_lost_at_grid_capacity(self):
        result = self._run(detail_modules=[
            {"type": "scene", "count": 8},
            {"type": "benefit", "count": 8},
            {"type": "proof", "count": 4},
        ])
        self.assertEqual(len(result["detail_images"]), 20)
        self.assertEqual(result["generated_detail_count"], 20)
        self.assertEqual(result["generation_rounds"], len(self.calls))
        self.assertTrue(all(call["rows"] * call["cols"] <= 16 for call in self.calls))
        self.assertTrue(all((self.root / path).is_file() for path in result["detail_images"]))

    def test_explicit_empty_module_selection_means_hero_only(self):
        result = self._run(detail_modules=[])
        self.assertEqual(result["detail_images"], [])
        self.assertEqual(result["detail_module_plan"], [])
        self.assertEqual(len(result["main_images"]), 6)
        self.assertEqual(len(self._outputs(result, "hero")), 6)

    def test_size_chart_uses_manifest_identity_after_multiple_rounds(self):
        result = self._run(
            generation_rounds=3,
            detail_modules=[{"type": "scene", "count": 2},
                            {"type": "size_chart", "count": 1},
                            {"type": "proof", "count": 1}],
            size_chart_enabled=True,
            selected_specs={"颜色": ["红色"], "尺码": ["S", "M"]},
            size_chart_specs=[{"size": "S", "measurements": {"胸围": "88cm"}},
                              {"size": "M", "measurements": {"胸围": "92cm"}}],
        )
        outputs = self._outputs(result, "detail")
        self.assertEqual([item["module_type"] for item in outputs],
                         ["scene", "scene", "size_chart", "proof"])
        chart = next(item for item in outputs if item["module_type"] == "size_chart")
        self.assertIn(chart["path"], result["size_chart_files"])
        self.assertEqual(result["detail_images"][2], chart["path"])
        self.assertEqual(len(result["size_chart_files"]), 1)
        with Image.open(self.root / chart["path"]) as image:
            self.assertGreaterEqual(image.width, 480)

    def test_size_chart_filters_to_selected_sizes_without_guessing_values(self):
        result = self._run(
            detail_modules=[{"type": "size_chart", "count": 1}],
            size_chart_enabled=True, size_chart_only_selected=True,
            selected_specs={"尺码": ["S", "M"]},
            size_chart_specs=[{"size": "S", "measurements": {"胸围": "88cm"}},
                              {"size": "L", "measurements": {"胸围": "100cm"}}],
        )
        rows = {row["size"]: row for row in result["size_chart_rows"]}
        self.assertEqual(set(rows), {"S", "M"})
        self.assertEqual(rows["S"]["measurements"]["胸围"], "88cm")
        self.assertEqual(rows["M"]["measurements"], {})
        self.assertTrue(rows["M"]["needs_measurement"])

    def test_unbound_sku_does_not_reuse_any_detail_image(self):
        result = self._run(
            detail_modules=[{"type": "scene", "count": 1},
                            {"type": "sku_preview", "count": 1}],
            selected_specs={"颜色": ["红色", "蓝色"], "尺码": ["M"]},
        )
        self.assertEqual(result["spec_images"], {})
        self.assertTrue(result["spec_preview_missing"])
        self.assertEqual(self._outputs(result, "spec"), [])

    def test_bound_sku_has_its_own_square_output_and_single_variant_reference(self):
        result = self._run(
            detail_modules=[{"type": "scene", "count": 1},
                            {"type": "sku_preview", "count": 1}],
            selected_specs={"颜色": ["红色", "蓝色"], "尺码": ["M"]},
            variant_references={"红色": [1]},
        )
        self.assertEqual(set(result["spec_images"]), {"红色"})
        path = result["spec_images"]["红色"]
        self.assertNotIn(path, result["detail_images"])
        self.assertNotIn(path, result["main_images"])
        with Image.open(self.root / path) as image:
            self.assertEqual(image.width, image.height)
            self.assertGreaterEqual(image.width, 480)
        spec = next(item for item in result["module_outputs"] if item.get("kind") == "spec")
        self.assertEqual(spec["spec_value"], "红色")
        self.assertEqual(spec["path"], path)
        self.assertTrue(any(call["reference_count"] == 1 for call in self.calls))

    def test_module_identifiers_are_unique_and_stable_for_same_plan(self):
        first = self._run(detail_modules=[{"type": "scene", "count": 2}, {"type": "proof", "count": 1}])
        second = self._run(detail_modules=[{"type": "scene", "count": 2}, {"type": "proof", "count": 1}])
        first_ids = [item["id"] for item in first["module_outputs"]]
        second_ids = [item["id"] for item in second["module_outputs"]]
        self.assertEqual(len(first_ids), len(set(first_ids)))
        self.assertEqual(first_ids, second_ids)
        self.assertNotEqual(first["job_id"], second["job_id"])

    def test_auto_recommendation_is_applied_and_manual_choices_take_priority(self):
        self.metadata["module_recommendations"] = [
            {"type": "scene", "count": 2}, {"type": "proof", "count": 1},
        ]
        automatic = self._run(detail_modules=None, auto_module_plan=True)
        self.assertEqual([item["module_type"] for item in self._outputs(automatic, "detail")],
                         ["scene", "scene", "proof"])
        manual = self._run(detail_modules=[{"type": "mechanism", "count": 1}], auto_module_plan=True)
        self.assertEqual([item["module_type"] for item in self._outputs(manual, "detail")], ["mechanism"])
        conservative = self._run(detail_modules=None, auto_module_plan=False)
        self.assertNotEqual(len(conservative["detail_images"]), 3)

    def test_provider_credentials_do_not_enter_results_or_saved_json(self):
        image_secret = "test-only-image-credential-do-not-store"
        prompt_secret = "test-only-prompt-credential-do-not-store"
        result = self._run(detail_modules=[{"type": "scene", "count": 1}], provider_overrides={
            "image_api_key": image_secret, "prompt_api_key": prompt_secret,
        })
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(image_secret, serialized)
        self.assertNotIn(prompt_secret, serialized)
        for path in self.root.rglob("*.json"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn(image_secret, text, str(path))
            self.assertNotIn(prompt_secret, text, str(path))

    def test_plan_preview_is_network_free_and_reports_real_execution_count(self):
        with patch.object(workflow, "_http_json", side_effect=AssertionError("plan must not use API")):
            plan = workflow.build_generation_plan(
                product_hint="针织上衣", rows=3, cols=3, strategy="grid",
                generation_rounds=3, auto_module_plan=False,
                detail_modules=[{"type": "scene", "count": 6}],
            )
        self.assertEqual(plan["detail_count"], 6)
        self.assertEqual(plan["planned_image_requests"], 3)
        self.assertEqual(len(plan["round_plan"]), 3)


if __name__ == "__main__":
    unittest.main()
