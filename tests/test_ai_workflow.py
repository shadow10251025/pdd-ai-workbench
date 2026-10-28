import json
import os
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from PIL import Image

from ai_workflow import (
    Image,
    WorkflowConfig,
    build_prompt_pack,
    crop_grid,
    generate_metadata,
    generate_sheet,
    _normalize_api_endpoint,
    _validate_metadata,
    build_quality_prompt_packs,
    _inject_care_panel_prompt,
    _inject_identity_guard_prompt,
    _identity_guard_text,
    _reference_coverage,
    _inject_creative_brief_prompt,
    _limit_creative_brief,
    _clean_creative_blocks,
    _clean_scene_plan,
    _build_module_prompt_previews,
    _clean_detail_modules,
    _default_detail_modules,
    _clean_size_chart_specs,
    _build_detail_module_plan,
    _build_round_plan,
    _inject_detail_module_prompt,
    _fallback_creative_blocks,
    PDD_CAROUSEL_MAX_BYTES,
    PDD_DETAIL_MAX_BYTES,
    build_campaign_style_lock,
    WorkflowError,
    _image_cache_key,
    _image_cache_paths,
    run_workflow,
)


class AIWorkflowTests(unittest.TestCase):
    def test_detail_module_plan_is_bounded_and_size_chart_is_opt_in(self):
        raw = [
            {"type": "carousel_sell", "count": 1},
            {"type": "angle", "count": 1},
            {"type": "lifestyle", "count": 4},
            {"type": "size", "count": 2},
            {"type": "unknown", "count": 99},
            {"type": "sku", "count": 1},
        ]
        cleaned = _clean_detail_modules(raw)
        self.assertEqual([item["type"] for item in cleaned], ["carousel_sell", "angle", "scene", "size_chart", "sku_preview"])
        self.assertEqual(cleaned[2]["count"], 4)
        self.assertLessEqual(sum(item["count"] for item in cleaned), 20)
        without_chart = _build_detail_module_plan(raw, "针织上衣", size_chart_enabled=False)
        self.assertNotIn("size_chart", [item["type"] for item in without_chart])
        with_chart = _build_detail_module_plan(raw, "针织上衣", size_chart_enabled=True)
        self.assertIn("size_chart", [item["type"] for item in with_chart])

    def test_default_module_plan_is_ecommerce_sequence_without_measurement_claims(self):
        default = _default_detail_modules("女款针织上衣")
        kinds = [item["type"] for item in default]
        self.assertIn("hook", kinds)
        self.assertIn("benefit", kinds)
        self.assertIn("closing_cta", kinds)
        self.assertNotIn("size_chart", kinds)
        self.assertNotIn("sku_preview", kinds)
        enabled = _default_detail_modules("女款针织上衣", size_chart_enabled=True)
        self.assertIn("size_chart", [item["type"] for item in enabled])

    def test_size_chart_rows_keep_only_selected_values_and_never_invent_measurements(self):
        rows = _clean_size_chart_specs([
            {"size": "S", "measurements": {"胸围": "88cm", "衣长": "60cm"}},
            {"size": "M", "measurements": {}},
            {"size": "L", "胸围": "96cm"},
            {"size": "XL", "measurements": {"胸围": "100cm"}},
        ], selected_sizes=["S", "M"])
        self.assertEqual([row["size"] for row in rows], ["S", "M"])
        self.assertEqual(rows[0]["measurements"]["胸围"], "88cm")
        self.assertFalse(rows[0]["needs_measurement"])
        self.assertTrue(rows[1]["needs_measurement"])
        self.assertEqual(rows[1]["measurements"], {})

    def test_round_plan_is_bounded_and_cost_transparent(self):
        modules = _build_detail_module_plan([
            {"type": "hook", "count": 2},
            {"type": "scene", "count": 3},
            {"type": "sku_preview", "count": 1},
        ], "商品")
        plan = _build_round_plan("quality", 4, modules, 3, 3)
        self.assertEqual(len(plan), 4)
        self.assertEqual(plan[0]["role"], "hero")
        self.assertTrue(all(item["api_request"] for item in plan))
        self.assertTrue(all("API" in item["cost_note"] for item in plan))
        self.assertLessEqual(sum(item["requested_panels"] for item in plan), 20)
        one_round = _build_round_plan("quality", 1, modules, 3, 3)
        self.assertEqual(len(one_round), 1, "选择 1 轮应真正保持单次 API 请求")

    def test_detail_prompt_contains_selected_modules_and_opt_in_size_chart_data(self):
        pack = build_quality_prompt_packs("针织上衣", "fresh", 3, 3)
        modules = _build_detail_module_plan([
            {"type": "benefit", "count": 1},
            {"type": "size_chart", "count": 1},
        ], "针织上衣", size_chart_enabled=True)
        rows = _clean_size_chart_specs([{"size": "M", "胸围": "96cm"}])
        _inject_detail_module_prompt(pack, modules, size_chart_enabled=True, size_chart_rows=rows)
        prompt = pack["detail"]["prompt"]
        self.assertIn("核心卖点 (benefit): 1 panel(s)", prompt)
        self.assertIn("SIZE CHART IS ENABLED", prompt)
        self.assertIn("96cm", prompt)
        self.assertIn("M", prompt)
        self.assertNotIn("SIZE CHART IS DISABLED", prompt)

    def test_module_prompt_previews_follow_selected_module_types(self):
        pack = build_quality_prompt_packs("针织上衣", "fresh", 2, 2)
        modules = _build_detail_module_plan([
            {"type": "lifestyle", "count": 2},
            {"type": "sku_preview", "count": 1},
        ], "针织上衣")
        previews = _build_module_prompt_previews(pack, {"scene_plan": []}, "针织上衣", detail_modules=modules)
        details = [item for item in previews if item.get("kind") == "detail"]
        self.assertEqual([item["module_type"] for item in details], ["scene", "scene", "sku_preview"])
        self.assertIn("Assigned business module", details[0]["prompt"])
        self.assertIn("SKU PREVIEW DATA", details[2]["prompt"])

    def test_admin_ui_exposes_module_round_and_size_chart_contract(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "web" / "index.html").read_text(encoding="utf-8")
        script = (root / "web" / "static" / "ai_workflow.js").read_text(encoding="utf-8")
        for marker in ("aiDetailModules", "aiGenerationRounds", "aiRoundStrategy", "aiSizeChartModule", "aiSizeChartSpecs"):
            self.assertIn(marker, html)
        for field in ("detail_modules_json", "generation_rounds", "size_chart_enabled", "size_chart_specs_json"):
            self.assertIn(field, script)
    def test_identity_guard_overrides_tentative_category_and_keeps_unknown_structure_unknown(self):
        pack = build_quality_prompt_packs("女款针织开衫", "minimal", 2, 3)
        metadata = _validate_metadata({
            "identity_evidence": ["照片可见为闭合的针织前片", "未见前中门襟或纽扣"],
            "identity_constraints": ["不得添加开衫开口、纽扣、口袋"],
            "uncertain_features": ["后领被遮挡，无法确认领型"],
            "identity_confidence": 0.61,
        }, "女款针织开衫")
        _inject_identity_guard_prompt(pack, metadata, reference_count=3)
        guard = pack["identity_guard"]
        self.assertIn("product hint/category is only a search label", guard)
        self.assertIn("ignore the words", guard)
        self.assertIn("不得添加开衫开口", guard)
        self.assertIn("below 0.75", guard)
        self.assertIn("reference photo(s)", pack["hero"]["prompt"])
        self.assertIn("reference photo(s)", pack["detail"]["prompt"])

    def test_identity_metadata_fields_are_conservative_and_bounded(self):
        metadata = _validate_metadata({
            "visual_evidence": ["可见针织纹理"] * 30,
            "visible_constraints": "未见洗标；未见钮扣",
            "uncertainties": ["遮挡"],
            "visual_confidence": 82,
        }, "针织上衣")
        self.assertLessEqual(len(metadata["identity_evidence"]), 12)
        self.assertEqual(metadata["identity_confidence"], 0.82)
        self.assertIn("未见洗标", metadata["identity_constraints"])
        self.assertIn("遮挡", metadata["uncertain_features"])

    def test_reference_coverage_reports_single_view_risk(self):
        coverage = _reference_coverage(1)
        self.assertEqual(coverage["level"], "low")
        self.assertTrue(any("背面" in item for item in coverage["risk_flags"]))
        guard = _identity_guard_text({}, reference_count=1)
        self.assertIn("REFERENCE COVERAGE RISK (low)", guard)
    def test_prompt_pack_reuses_style_lock_and_grid(self):
        pack = build_prompt_pack("米白色针织开衫", "minimal", 2, 3)
        self.assertEqual(pack["panel_count"], 6)
        self.assertIn(pack["style_lock"], pack["prompt"])
        self.assertIn("exact 2 by 3 grid", pack["prompt"])
        self.assertIn("never invent certifications", pack["prompt"])
        self.assertIn("detail-macro", pack["template_ids"])
        self.assertEqual(len(pack["shot_plan"]), 6)
        self.assertEqual(pack["skill"], "ecom-details-image")
        self.assertEqual(pack["skill_template_count"], 25)

    def test_prompt_pack_uses_project_vendor_skill_clone(self):
        pack = build_quality_prompt_packs("保温杯", "fresh", 2, 3)
        self.assertIn("project vendor skill", pack["skill_source"])
        self.assertIn("GitHub clone", pack["skill_source"])

    def test_quality_prompt_separates_hero_and_detail_infographics(self):
        pack = build_quality_prompt_packs("女款针织开衫", "luxury", 2, 3)
        self.assertEqual(pack["mode"], "quality")
        self.assertIn("exact 2 by 3", pack["hero"]["prompt"])
        self.assertEqual(pack["hero"]["panel_count"], 6)
        self.assertIn("mobile-first PDD product-detail infographic", pack["detail"]["prompt"])
        self.assertIn("champagne", pack["style_lock"])
        self.assertIn("E-commerce infographic", pack["detail"]["prompt"])
        self.assertIn("printed artwork", pack["detail"]["prompt"])

    def test_category_scene_plan_is_not_generic_for_apparel_and_electronics(self):
        apparel = build_quality_prompt_packs("女款针织上衣", "fresh", 3, 3, True)
        electronics = build_quality_prompt_packs("无线蓝牙耳机", "tech", 3, 3, False)
        self.assertIn("commute", apparel["hero"]["prompt"])
        self.assertIn("cafe", apparel["detail"]["prompt"])
        self.assertIn("ports", electronics["hero"]["prompt"])
        self.assertIn("desk", electronics["detail"]["prompt"])

    def test_skill_template_mix_expands_scene_system_by_category(self):
        apparel = build_quality_prompt_packs("女款针织上衣", "fresh", 3, 3, True)
        electronics = build_quality_prompt_packs("无线蓝牙耳机", "tech", 3, 3, False)
        self.assertIn("ghost-mannequin", apparel["template_ids"])
        self.assertIn("try-on-virtual", apparel["template_ids"])
        self.assertIn("device-mockup", electronics["template_ids"])
        self.assertIn("multi-angle-grid", electronics["template_ids"])

    def test_style_lock_has_distinct_palette_and_accepts_ui_labels(self):
        minimal = build_campaign_style_lock("minimal")
        fresh = build_campaign_style_lock("清新自然")
        luxury = build_campaign_style_lock("luxury")
        tech = build_campaign_style_lock("tech")
        self.assertIn("style variant MINIMAL", minimal)
        self.assertIn("style variant FRESH", fresh)
        self.assertIn("style variant LUXURY", luxury)
        self.assertIn("style variant TECH", tech)
        self.assertIn("#EAF7F0", fresh)
        self.assertIn("#D4AF37", luxury)
        self.assertIn("#4778FF", tech)
        self.assertNotEqual(minimal, fresh)
        self.assertNotEqual(fresh, luxury)
        self.assertIn("callout lines", minimal)
        self.assertNotIn("consistent rounded rectangular info labels", minimal)

    def test_skill_default_pack_is_six_hero_and_nine_detail_candidates(self):
        pack = build_quality_prompt_packs("女款针织开衫", "minimal", 3, 3)
        self.assertEqual(pack["hero"]["panel_count"], 6)
        self.assertEqual(pack["detail"]["panel_count"], 9)
        self.assertIn("Panel 3: HERO 3: macro knit/fabric/print detail", pack["hero"]["prompt"])
        self.assertIn("Panel 6: HERO 6: three-panel editorial usage rhythm", pack["hero"]["prompt"])
        self.assertNotIn("尺码信息待补充", pack["detail"]["prompt"])
        self.assertNotIn("care-information infographic", pack["detail"]["prompt"])
        self.assertIn("Do not create care, wash-label, size-chart", pack["detail"]["prompt"])
        self.assertIn("product hint as tentative", pack["hero"]["prompt"])
        self.assertIn("Do not add a cardigan opening", pack["hero"]["prompt"])

    def test_ad_scene_prompt_includes_synthetic_model_and_verbatim_copy(self):
        pack = build_quality_prompt_packs("女款针织开衫", "fresh", 2, 3, True, "针织纹理｜日常穿搭｜尺码可选")
        self.assertTrue(pack["model_scene"])
        self.assertIn("model-showcase", pack["template_ids"])
        self.assertIn("poster-banner", pack["template_ids"])
        self.assertIn("synthetic adult model", pack["prompt"])
        self.assertIn("针织纹理", pack["prompt"])
        self.assertIn("not based on any reference person", pack["prompt"])
        self.assertIn("anti-AI", pack["prompt"])
        self.assertIn("Panel 4 only", pack["hero"]["prompt"])
        self.assertIn("「针织纹理｜日常穿搭｜尺码可选」", pack["prompt"])

    def test_ad_copy_without_model_does_not_sneak_a_face_into_hero(self):
        pack = build_quality_prompt_packs("保温杯", "minimal", 2, 3, False, "轻巧便携")
        self.assertIn("poster-banner", pack["template_ids"])
        self.assertIn("no person, face, or hand", pack["hero"]["prompt"])
        self.assertNotIn("synthetic adult model", pack["hero"]["prompt"])
        self.assertIn("「轻巧便携」", pack["hero"]["prompt"])

    def test_apparel_spec_direction_is_selectable_but_not_confirmed(self):
        metadata = _validate_metadata({"title": "针织开衫"}, "女款针织开衫")
        self.assertIn("尺码", metadata["spec_suggestions"])
        self.assertEqual(metadata["specs"], {"颜色": ["默认色"], "尺码": ["均码"]})
        self.assertIn("XL", metadata["spec_suggestions"]["尺码"])

    def test_care_instructions_are_candidates_without_a_visible_wash_label(self):
        metadata = _validate_metadata({
            "title": "针织开衫",
            "care_instructions": ["冷水轻柔洗涤", "避免高温烘干"],
        }, "女款针织开衫")
        self.assertEqual(metadata["care_status"], "candidate")
        self.assertEqual(metadata["care_instructions"], ["冷水轻柔洗涤", "避免高温烘干"])
        confirmed = _validate_metadata({
            "care_instructions": ["按洗标机洗"], "care_status": "confirmed"
        }, "针织开衫")
        self.assertEqual(confirmed["care_status"], "confirmed")

    def test_care_candidate_is_carried_into_detail_infographic_prompt(self):
        pack = build_quality_prompt_packs("针织上衣", "minimal", 3, 3)
        _inject_care_panel_prompt(pack, {
            "care_status": "candidate",
            "care_instructions": ["冷水轻柔洗涤", "以实物洗标为准"],
        })
        self.assertIn("护理建议（候选，请以洗标为准）", pack["detail"]["prompt"])
        self.assertIn("冷水轻柔洗涤", pack["detail"]["prompt"])

    def test_default_detail_prompt_does_not_add_care_or_size_panels(self):
        pack = build_quality_prompt_packs("针织上衣", "fresh", 3, 3)
        detail = pack["detail"]["prompt"]
        self.assertNotIn("care-information infographic", detail)
        self.assertNotIn("尺码信息待补充", detail)
        self.assertIn("Do not create care, wash-label, size-chart", detail)

    def test_scene_plan_is_bounded_and_reaches_image_prompt(self):
        plan = _clean_scene_plan([{
            "role": "场景",
            "setting": "周末咖啡店",
            "time_of_day": "上午",
            "camera": "三分之四侧面",
            "lighting": "窗边自然光",
            "mood": "轻松",
            "action": "自然使用商品",
            "copy_direction": "日常好搭",
        }] * 12)
        self.assertEqual(len(plan), 9)
        pack = build_quality_prompt_packs("针织上衣", "fresh", 3, 3)
        metadata = _validate_metadata({"scene_plan": plan}, "针织上衣")
        _inject_creative_brief_prompt(pack, metadata)
        self.assertIn("AUTO SCENE + CAMERA PLAN", pack["detail"]["prompt"])
        self.assertIn("周末咖啡店", pack["detail"]["prompt"])
        self.assertIn("窗边自然光", pack["detail"]["prompt"])

    def test_module_prompt_previews_are_available_for_each_panel(self):
        pack = build_quality_prompt_packs("针织上衣", "fresh", 3, 3)
        metadata = _validate_metadata({"scene_suggestions": ["咖啡店", "通勤"]}, "针织上衣")
        _inject_identity_guard_prompt(pack, metadata, reference_count=1)
        _inject_creative_brief_prompt(pack, metadata)
        previews = _build_module_prompt_previews(pack, metadata, "针织上衣")
        self.assertEqual(len(previews), 15)
        self.assertEqual(previews[0]["kind"], "hero")
        self.assertEqual(previews[6]["kind"], "detail")
        self.assertIn("Campaign Style Lock", previews[6]["prompt"])
        self.assertIn("Do not create care", previews[6]["prompt"])

    def test_creative_brief_enriches_scene_and_copy_without_unbounded_text(self):
        pack = build_quality_prompt_packs("针织上衣", "fresh", 3, 3, True)
        metadata = _validate_metadata({
            "selling_points": ["柔软针织纹理", "日常叠穿"],
            "scene_suggestions": ["通勤室内", "周末咖啡店"],
            "ad_headlines": ["自然穿搭", "柔软触感"],
        }, "针织上衣")
        _inject_creative_brief_prompt(pack, metadata)
        self.assertIn("CONVERSION CREATIVE BRIEF", pack["hero"]["prompt"])
        self.assertIn("柔软针织纹理", pack["detail"]["prompt"])
        self.assertIn("周末咖啡店", pack["detail"]["prompt"])
        self.assertIn("integrate typography with the scene", pack["detail"]["prompt"].lower())
        self.assertNotIn("local compositor", pack["detail"]["prompt"])
        self.assertNotIn("Only the designated model/ad panel may contain typography", pack["detail"]["prompt"])
        self.assertIn("Do not force every panel into the same rigid box or border", pack["detail"]["prompt"])
        self.assertIn("text-free", pack["hero"]["prompt"])
        self.assertLessEqual(len(metadata["selling_points"]), 6)
        self.assertLessEqual(len(metadata["ad_headlines"]), 5)

    def test_creative_density_switch_limits_candidates(self):
        metadata = {
            "selling_points": [str(i) for i in range(6)],
            "scene_suggestions": [str(i) for i in range(6)],
            "ad_headlines": [str(i) for i in range(5)],
        }
        self.assertEqual(len(_limit_creative_brief(dict(metadata), "conservative")["selling_points"]), 3)
        self.assertEqual(len(_limit_creative_brief(dict(metadata), "rich")["selling_points"]), 6)

    def test_structured_pdp_blocks_are_cleaned_and_mapped_to_prompt_modules(self):
        raw = [{
            "role": "mechanism",
            "headline": "看得见的细节",
            "subheadline": "只写照片可以证明的结构",
            "labels": ["真实纹理", "结构待核对", "不要编造"],
            "scene": "自然光桌面近景",
            "evidence": "以当前照片为准",
            "cta": "查看详情",
        }] * 12
        blocks = _clean_creative_blocks(raw)
        self.assertEqual(len(blocks), 9)
        self.assertEqual(blocks[0]["role"], "mechanism")
        self.assertEqual(blocks[0]["role_label"], "机制说明")
        self.assertLessEqual(len(blocks[0]["labels"]), 4)
        metadata = {
            "selling_points": ["真实纹理"],
            "scene_suggestions": ["自然光桌面近景"],
            "ad_headlines": ["看得见的细节"],
            "creative_blocks": blocks,
        }
        pack = build_quality_prompt_packs("保温杯", "fresh", 3, 3)
        _inject_creative_brief_prompt(pack, metadata)
        self.assertIn("PDP COPY + SCENE MAP", pack["detail"]["prompt"])
        self.assertIn("机制说明", pack["detail"]["prompt"])
        self.assertIn("自然光桌面近景", pack["detail"]["prompt"])
        self.assertIn("total copy under 50", pack["detail"]["prompt"])
        self.assertIn("Creative usage scenes may be reasonable ecommerce settings", pack["detail"]["prompt"])
        keyed = _clean_creative_blocks({"benefit": {"headline": "看得见"}, "scene": {"scene": "桌面"}})
        self.assertEqual([item["role"] for item in keyed], ["benefit", "scene"])

    def test_flat_candidates_get_a_bounded_pdp_sequence(self):
        blocks = _fallback_creative_blocks({
            "selling_points": ["纹理", "容量"],
            "scene_suggestions": ["通勤", "桌面"],
            "ad_headlines": ["轻松携带"],
        }, "rich")
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0]["role"], "hook")
        self.assertEqual(blocks[1]["role"], "pain_point")
        self.assertIn("通勤", blocks[0]["scene"])

    def test_rich_creative_brief_reaches_workflow_image_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            from io import BytesIO
            image = BytesIO(); Image.new("RGB", (640, 640), (180, 200, 220)).save(image, "JPEG")
            metadata_json = json.dumps({
                "title": "针织上衣",
                "selling_points": ["针织纹理", "日常叠穿", "自然光质感", "柔软触感"],
                "scene_suggestions": ["通勤室内", "咖啡店", "周末散步", "居家阅读"],
                "ad_headlines": ["自然穿搭", "日常好搭", "柔软触感"],
            }, ensure_ascii=False)
            response = {"choices": [{"message": {"content": metadata_json}}]}
            with patch.dict(os.environ, {
                "PDD_AI_PROVIDER": "mock",
                "PDD_PROMPT_API_URL": "https://example.test/v1/chat/completions",
                "PDD_PROMPT_API_KEY": "secret",
            }, clear=False), patch("ai_workflow._http_json", return_value=response):
                result = run_workflow(
                    image_bytes=image.getvalue(), filename="product.jpg", product_hint="针织上衣",
                    style="fresh", rows=3, cols=3, project_dir=root, creative_density="rich",
                )
            self.assertIn("CONVERSION CREATIVE BRIEF", result["prompt_pack"]["hero"]["prompt"])
            self.assertIn("咖啡店", result["prompt_pack"]["detail"]["prompt"])
            self.assertEqual(result["creative_density"], "rich")

    def test_creative_brief_override_reaches_prompt_and_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            from io import BytesIO
            image = BytesIO(); Image.new("RGB", (640, 640), (180, 200, 220)).save(image, "JPEG")
            metadata_json = json.dumps({"title": "针织上衣"}, ensure_ascii=False)
            response = {"choices": [{"message": {"content": metadata_json}}]}
            with patch.dict(os.environ, {
                "PDD_AI_PROVIDER": "mock",
                "PDD_PROMPT_API_URL": "https://example.test/v1/chat/completions",
                "PDD_PROMPT_API_KEY": "secret",
            }, clear=False), patch("ai_workflow._http_json", return_value=response):
                result = run_workflow(
                    image_bytes=image.getvalue(), filename="product.jpg", product_hint="针织上衣",
                    style="fresh", rows=3, cols=3, project_dir=root, creative_density="conservative",
                    creative_brief_override={
                        "selling_points": [f"人工确认卖点{i}" for i in range(10)],
                        "scene_suggestions": ["人工确认场景"],
                        "ad_headlines": ["人工标题"],
                    },
                )
            self.assertEqual(result["selling_points"], [f"人工确认卖点{i}" for i in range(3)])
            self.assertIn("人工确认场景", result["prompt_pack"]["hero"]["prompt"])
            self.assertIn("人工标题", result["prompt_pack"]["detail"]["prompt"])
            self.assertTrue(result["scene_plan"])
            self.assertIn("人工确认场景", result["prompt_pack"]["detail"]["prompt"])

    def test_crop_grid_returns_independent_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            sheet = Image.new("RGB", (1200, 800), "white")
            paths = crop_grid(sheet, out, 2, 3, upscale=1.0)
            self.assertEqual(len(paths), 6)
            for path in paths:
                self.assertTrue(Path(path).is_file())
                with Image.open(path) as image:
                    self.assertGreaterEqual(image.width, 480)
                    self.assertGreaterEqual(image.height, 480)
                self.assertLessEqual(Path(path).stat().st_size, 3 * 1024 * 1024)

    def test_mock_workflow_creates_pdd_bundle_without_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_image = Image.new("RGB", (800, 600), (220, 200, 180))
            from io import BytesIO
            data = BytesIO()
            input_image.save(data, "JPEG")
            result = run_workflow(
                image_bytes=data.getvalue(), filename="商品.jpg", product_hint="保温杯",
                style="fresh", rows=2, cols=3, project_dir=root,
            )
            self.assertEqual(result["mode"], "mock")
            self.assertEqual(result["generation_kind"], "offline_demo")
            self.assertFalse(result["generated_images_are_ai"])
            self.assertEqual(result["strategy"], "quality")
            self.assertEqual(result["provider_calls"], 0)
            self.assertEqual(len(result["generated_sheets"]), 2)
            self.assertEqual(result["specs_confirmed"], {})
            self.assertEqual(result["skus"][0]["name"], "默认")
            self.assertEqual(len(result["detail_images"]), 6)
            self.assertEqual(len(result["main_images"]), 6)
            self.assertTrue(set(result["main_images"]).isdisjoint(result["detail_images"]))
            # The deterministic offline mock intentionally reuses one sheet;
            # the audit must flag that rather than presenting it as a final
            # production-quality image pack.
            self.assertEqual(result["quality_audit"]["status"], "review")
            self.assertEqual(result["quality_audit"]["main"]["count"], 6)
            self.assertEqual(result["quality_audit"]["detail"]["count"], 6)
            self.assertGreater(result["quality_audit"]["main_detail_overlap"], 0)
            self.assertEqual(result["platform_constraints"]["pdd_main_max_bytes"], PDD_CAROUSEL_MAX_BYTES)
            self.assertEqual(result["platform_constraints"]["pdd_detail_max_bytes"], PDD_DETAIL_MAX_BYTES)
            for relative in result["main_images"]:
                self.assertLessEqual((root / relative).stat().st_size, PDD_CAROUSEL_MAX_BYTES)
                with Image.open(root / relative) as hero:
                    self.assertEqual(hero.width, hero.height)
            for relative in result["detail_images"]:
                self.assertLessEqual((root / relative).stat().st_size, PDD_DETAIL_MAX_BYTES)
            self.assertEqual(len(result["source_images"]), 1)
            self.assertNotIn(result["source_images"][0], result["main_images"])
            self.assertTrue((root / result["generated_sheet"]).is_file())
            self.assertEqual(result["title"], "保温杯")
            self.assertIn("颜色", result["specs"])
            # 任务结果只包含路径和业务字段，不出现任何环境变量密钥字段。
            serialized = json.dumps(result, ensure_ascii=False)
            self.assertNotIn("API_KEY", serialized)

    def test_builtin_demo_does_not_claim_real_ai_without_task_image_override(self):
        """The local adapter is a fixed Skill sample, even though it has an API URL/key."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            from io import BytesIO
            image = Image.new("RGB", (640, 640), (180, 200, 220))
            buf = BytesIO(); image.save(buf, "JPEG")

            def fake_sheet(config, prompt, input_image, out_path, rows, cols, progress, trace, reference_images=None):
                trace.update({"mode": "api", "endpoint_kind": "generations", "reference_sent": False})
                sheet = Image.new("RGB", (cols * 640, rows * 640), "white")
                out_path.parent.mkdir(parents=True, exist_ok=True)
                sheet.save(out_path, "JPEG")
                return sheet

            with patch.dict(os.environ, {
                "PDD_AI_DEMO": "1",
                "PDD_AI_PROVIDER": "openai",
                "PDD_IMAGE_API_URL": "http://127.0.0.1:3117/v1/images/generations",
                "PDD_IMAGE_API_KEY": "demo-local",
                "PDD_PROMPT_API_URL": "",
                "PDD_PROMPT_API_KEY": "",
                "PDD_IMAGE_REFERENCE_MODE": "generation",
            }, clear=False), patch("ai_workflow.generate_sheet", side_effect=fake_sheet):
                result = run_workflow(
                    image_bytes=buf.getvalue(), filename="product.jpg", product_hint="保温杯",
                    style="minimal", rows=2, cols=3, project_dir=root, strategy="quality",
                )
            self.assertEqual(result["generation_kind"], "demo_asset")
            self.assertFalse(result["generated_images_are_ai"])
            self.assertEqual(result["reference_mode"], "generation")
            self.assertFalse(result["reference_images_sent"])

    def test_multiple_reference_photos_and_larger_grid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            from io import BytesIO
            inputs = []
            for color in ((210, 180, 160), (160, 190, 215), (190, 210, 170)):
                image = Image.new("RGB", (720, 720), color)
                buf = BytesIO(); image.save(buf, "JPEG"); inputs.append(buf.getvalue())
            result = run_workflow(
                image_bytes=inputs, filename=["front.jpg", "side.jpg", "back.jpg"],
                product_hint="多角度商品", style="minimal", rows=3, cols=4, project_dir=root,
            )
            self.assertEqual(len(result["input_images"]), 3)
            self.assertEqual(len(result["source_images"]), 3)
            self.assertEqual(len(result["detail_images"]), 12)
            self.assertEqual(len(result["main_images"]), 6)
            self.assertTrue(set(result["main_images"]).isdisjoint(result["detail_images"]))
            self.assertTrue(set(result["main_images"]).isdisjoint(result["source_images"]))
            self.assertTrue((root / result["reference_board"]).is_file())

    def test_four_by_four_grid_is_not_silently_clamped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            from io import BytesIO
            image = Image.new("RGB", (640, 640), (180, 200, 220))
            buf = BytesIO(); image.save(buf, "JPEG")
            result = run_workflow(
                image_bytes=buf.getvalue(), filename="product.jpg", product_hint="商品",
                style="minimal", rows=4, cols=4, project_dir=root,
            )
            self.assertEqual(len(result["detail_images"]), 16)

    def test_openai_compatible_generation_and_vision_metadata_adapters(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = Image.new("RGB", (640, 640), (180, 200, 220))
            prompt_pack = build_prompt_pack("保温杯", "minimal", 2, 2)
            config = WorkflowConfig(project_dir=root, provider_mode="openai", image_api_url="https://example.test/v1/images/generations", prompt_api_url="https://example.test/v1/chat/completions", image_api_key="secret", prompt_api_key="secret")
            fake_sheet = Image.new("RGB", (800, 800), "white")
            buf = __import__('io').BytesIO(); fake_sheet.save(buf, "PNG")
            with patch.dict(os.environ, {"PDD_IMAGE_REFERENCE_MODE": "generation"}), patch("ai_workflow._http_json", side_effect=[{"data": [{"b64_json": __import__('base64').b64encode(buf.getvalue()).decode()}]}, {"choices": [{"message": {"content": '{\"title\":\"保温杯\",\"specs\":{\"颜色\":[\"蓝色\"]}}'}}]}]) as request:
                out = root / "sheet.jpg"
                generate_sheet(config, prompt_pack["prompt"], image, out, 2, 2)
                metadata = generate_metadata(config, "保温杯", prompt_pack, image)
            self.assertTrue(out.is_file())
            self.assertEqual(metadata["title"], "保温杯")
            self.assertEqual(metadata["specs"]["颜色"], ["蓝色"])
            self.assertEqual(request.call_count, 2)
            self.assertNotIn("response_format", request.call_args_list[0].args[1])
            self.assertEqual(request.call_args_list[0].args[1]["output_format"], "jpeg")
            vision_payload = request.call_args_list[1].args[1]
            self.assertIn("image_url", vision_payload["messages"][1]["content"][1])

    def test_auto_reference_mode_switches_generations_to_edits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = Image.new("RGB", (320, 320), (200, 190, 180))
            output = Image.new("RGB", (640, 640), "white")
            buf = __import__('io').BytesIO(); output.save(buf, "PNG")

            class FakeResponse:
                def raise_for_status(self):
                    return None
                def json(self):
                    return {"data": [{"b64_json": __import__('base64').b64encode(buf.getvalue()).decode()}]}

            config = WorkflowConfig(project_dir=root, provider_mode="openai", image_api_url="https://example.test/v1/images/generations", image_api_key="secret")
            with patch.dict(os.environ, {"PDD_IMAGE_REFERENCE_MODE": "auto", "PDD_IMAGE_API_MODE": ""}), patch("safe_http.request_json", return_value=FakeResponse().json()) as post:
                trace = {}
                generate_sheet(config, "product contact sheet", image, root / "edit.jpg", 2, 2, trace=trace)
            self.assertIn("/images/edits", post.call_args.args[1])
            # requests accepts either a mapping or a list of repeated multipart
            # fields.  The workflow uses repeated fields for multi-photo input;
            # keep the assertion compatible with both one-photo gateways and
            # the multi-reference implementation.
            files = post.call_args.kwargs["files"]
            if isinstance(files, dict):
                self.assertIn("image", files)
            else:
                self.assertTrue(any(item[0] == "image" for item in files))
            self.assertEqual(trace["endpoint_kind"], "edits")
            self.assertTrue(trace["reference_sent"])

    def test_identical_image_request_reuses_cache_without_second_provider_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = Image.new("RGB", (320, 320), (200, 190, 180))
            output = Image.new("RGB", (640, 640), "white")
            buf = __import__('io').BytesIO(); output.save(buf, "PNG")
            response = {"data": [{"b64_json": __import__('base64').b64encode(buf.getvalue()).decode()}]}
            config = WorkflowConfig(
                project_dir=root,
                provider_mode="openai",
                image_api_url="https://example.test/v1/images/generations",
                image_api_key="secret",
            )
            first_trace, second_trace = {}, {}
            with patch.dict(os.environ, {"PDD_IMAGE_REFERENCE_MODE": "generation", "PDD_AI_IMAGE_CACHE": "1"}), patch(
                "ai_workflow._http_json", return_value=response
            ) as request:
                generate_sheet(config, "stable ecommerce prompt", image, root / "first.jpg", 2, 2, trace=first_trace)
                generate_sheet(config, "stable ecommerce prompt", image, root / "second.jpg", 2, 2, trace=second_trace)
            self.assertEqual(request.call_count, 1)
            self.assertFalse(first_trace["cache_hit"])
            self.assertTrue(second_trace["cache_hit"])
            self.assertEqual(first_trace["cache_key"], second_trace["cache_key"])
            self.assertTrue((root / "second.jpg").is_file())

    def test_cache_namespace_separates_shared_rental_tenants(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = Image.new("RGB", (320, 320), (200, 190, 180))
            output = Image.new("RGB", (640, 640), "white")
            buf = __import__('io').BytesIO(); output.save(buf, "PNG")
            response = {"data": [{"b64_json": __import__('base64').b64encode(buf.getvalue()).decode()}]}
            config = WorkflowConfig(
                project_dir=root,
                provider_mode="openai",
                image_api_url="https://example.test/v1/images/generations",
                image_api_key="secret",
            )
            with patch.dict(os.environ, {"PDD_IMAGE_REFERENCE_MODE": "generation", "PDD_AI_IMAGE_CACHE": "1", "PDD_AI_CACHE_NAMESPACE": "tenant-a"}), patch(
                "ai_workflow._http_json", return_value=response
            ) as request:
                generate_sheet(config, "stable ecommerce prompt", image, root / "tenant-a.jpg", 2, 2)
                os.environ["PDD_AI_CACHE_NAMESPACE"] = "tenant-b"
                generate_sheet(config, "stable ecommerce prompt", image, root / "tenant-b.jpg", 2, 2)
            self.assertEqual(request.call_count, 2)

    def test_duplicate_cache_is_discarded_before_reuse(self):
        """A pre-validation cache must not bypass the source-echo guard."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = Image.new("RGB", (800, 800), (200, 80, 120))
            config = WorkflowConfig(
                project_dir=root,
                provider_mode="openai",
                image_api_url="https://example.test/v1/images/generations",
                image_api_key="secret",
            )
            prompt = "stale cache prompt"
            key = _image_cache_key(config, prompt, image, 2, 2, None, "generation", config.image_api_url)
            cache_path, meta_path = _image_cache_paths(config, key)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            image.save(cache_path, "JPEG")
            meta_path.write_text('{"trace": {"cache_hit": false}}', encoding="utf-8")
            generated = Image.new("RGB", (640, 640), "white")
            buf = __import__('io').BytesIO(); generated.save(buf, "PNG")
            response = {"data": [{"b64_json": __import__('base64').b64encode(buf.getvalue()).decode()}]}
            trace = {}
            with patch.dict(os.environ, {"PDD_IMAGE_REFERENCE_MODE": "generation", "PDD_AI_IMAGE_CACHE": "1"}), patch(
                "ai_workflow._http_json", return_value=response
            ) as request:
                generate_sheet(config, prompt, image, root / "fresh.jpg", 2, 2, trace=trace)
            self.assertEqual(request.call_count, 1)
            self.assertFalse(trace["cache_hit"])
            self.assertEqual(trace["output_validation"]["status"], "passed")

    def test_edit_request_sends_each_reference_photo_separately(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            from io import BytesIO
            first = Image.new("RGB", (320, 320), (200, 190, 180))
            second = Image.new("RGB", (320, 320), (80, 140, 220))
            output = Image.new("RGB", (640, 640), "white")
            out_buf = BytesIO(); output.save(out_buf, "PNG")

            class FakeResponse:
                def raise_for_status(self):
                    return None
                def json(self):
                    return {"data": [{"b64_json": __import__('base64').b64encode(out_buf.getvalue()).decode()}]}

            config = WorkflowConfig(project_dir=root, provider_mode="openai", image_api_url="https://example.test/v1/images/edits", image_api_key="secret")
            with patch.dict(os.environ, {"PDD_IMAGE_SEND_ALL_REFERENCES": "1"}), patch("safe_http.request_json", return_value=FakeResponse().json()) as post:
                trace = {}
                generate_sheet(config, "identity locked contact sheet", first, root / "edit.jpg", 2, 2, trace=trace, reference_images=[first, second])
            files = post.call_args.kwargs["files"]
            self.assertIsInstance(files, list)
            self.assertEqual(sum(1 for key, _value in files if key == "image"), 2)
            self.assertEqual(trace["reference_count"], 2)
            self.assertEqual(trace["reference_input_mode"], "separate")

    def test_api_echo_of_source_photo_is_rejected_before_persistence(self):
        """A successful API response must not let a gateway echo become a product image."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = Image.new("RGB", (800, 800), (200, 80, 120))
            output = __import__('io').BytesIO(); image.save(output, "PNG")

            class FakeResponse:
                def raise_for_status(self):
                    return None
                def json(self):
                    return {"data": [{"b64_json": __import__('base64').b64encode(output.getvalue()).decode()}]}

            config = WorkflowConfig(project_dir=root, provider_mode="openai", image_api_url="https://example.test/v1/images/edits", image_api_key="secret")
            trace = {}
            with patch("safe_http.request_json", return_value=FakeResponse().json()):
                with self.assertRaises(WorkflowError) as raised:
                    generate_sheet(config, "identity locked contact sheet", image, root / "echo.jpg", 2, 2, trace=trace, reference_images=[image])
            self.assertIn("基本相同", str(raised.exception))
            self.assertEqual(trace["output_validation"]["status"], "rejected_duplicate")
            self.assertFalse((root / "echo.jpg").exists())

    def test_repeated_source_grid_is_rejected(self):
        """Some adapters wrap the unchanged input in a nominal contact sheet."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = Image.new("RGB", (320, 320), (30, 150, 90))
            sheet = Image.new("RGB", (640, 640), "white")
            for row in range(2):
                for col in range(2):
                    sheet.paste(source, (col * 320, row * 320))
            output = __import__('io').BytesIO(); sheet.save(output, "PNG")

            class FakeResponse:
                def raise_for_status(self):
                    return None
                def json(self):
                    return {"data": [{"b64_json": __import__('base64').b64encode(output.getvalue()).decode()}]}

            config = WorkflowConfig(project_dir=root, provider_mode="openai", image_api_url="https://example.test/v1/images/edits", image_api_key="secret")
            trace = {}
            with patch("safe_http.request_json", return_value=FakeResponse().json()):
                with self.assertRaises(WorkflowError) as raised:
                    generate_sheet(config, "identity locked contact sheet", source, root / "repeat.jpg", 2, 2, trace=trace, reference_images=[source])
            self.assertTrue(any(text in str(raised.exception) for text in ("大部分仍是上传原图", "基本相同")))
            self.assertIn(trace["output_validation"]["status"], {"rejected_duplicate", "rejected_duplicate_grid"})

    def test_api_grid_resolution_is_rejected_when_cells_would_be_blurry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = Image.new("RGB", (800, 800), (30, 150, 90))
            sheet = Image.new("RGB", (400, 400), "white")
            output = __import__('io').BytesIO(); sheet.save(output, "PNG")

            class FakeResponse:
                def raise_for_status(self):
                    return None
                def json(self):
                    return {"data": [{"b64_json": __import__('base64').b64encode(output.getvalue()).decode()}]}

            config = WorkflowConfig(project_dir=root, provider_mode="openai", image_api_url="https://example.test/v1/images/edits", image_api_key="secret")
            trace = {}
            with patch("safe_http.request_json", return_value=FakeResponse().json()):
                with self.assertRaises(WorkflowError) as raised:
                    generate_sheet(config, "identity locked contact sheet", source, root / "small.jpg", 2, 2, trace=trace, reference_images=[source])
            self.assertIn("分辨率不足", str(raised.exception))
            self.assertEqual(trace["output_validation"]["status"], "rejected_grid_resolution")

    def test_api_grid_resolution_warns_before_it_becomes_unusable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = Image.new("RGB", (800, 800), (30, 150, 90))
            sheet = Image.new("RGB", (700, 700), "white")
            output = __import__('io').BytesIO(); sheet.save(output, "PNG")

            class FakeResponse:
                def raise_for_status(self):
                    return None
                def json(self):
                    return {"data": [{"b64_json": __import__('base64').b64encode(output.getvalue()).decode()}]}

            config = WorkflowConfig(project_dir=root, provider_mode="openai", image_api_url="https://example.test/v1/images/edits", image_api_key="secret")
            trace = {}
            with patch("safe_http.request_json", return_value=FakeResponse().json()):
                generate_sheet(config, "identity locked contact sheet", source, root / "warn.jpg", 2, 2, trace=trace, reference_images=[source])
            self.assertIn("quality_warning", trace["output_validation"])
            self.assertTrue((root / "warn.jpg").is_file())

    def test_result_proves_new_input_and_offline_reference_behavior(self):
        """A local mock result must identify its source and admit no reference was sent."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            from io import BytesIO
            image = Image.new("RGB", (640, 640), (33, 99, 180))
            buf = BytesIO(); image.save(buf, "JPEG")
            raw = buf.getvalue()
            result = run_workflow(
                image_bytes=raw, filename="new-upload.jpg", product_hint="保温杯",
                style="minimal", rows=2, cols=2, project_dir=root,
            )
            self.assertEqual(result["source_image_count"], 1)
            self.assertEqual(len(result["source_image_fingerprints"]), 1)
            self.assertEqual(result["reference_mode"], "offline")
            self.assertFalse(result["reference_images_sent"])
            self.assertEqual(len(result["generation_trace"]), 2)
            self.assertTrue(all(item["reference_sent"] is False for item in result["generation_trace"]))

    def test_output_path_and_model_text_are_not_emitted_as_secrets(self):
        # Regression guard for the boundary used by the PDD filler: model-provided
        # brand/attribute text must be JSON-encoded before Runtime.evaluate.
        from one_click_list import _js_string
        value = '品牌"; window.__injected = true; //'
        self.assertEqual(json.loads(_js_string(value)), value)

    def test_api_base_urls_are_normalized(self):
        self.assertEqual(_normalize_api_endpoint('https://api.example.com/v1', '/images/generations'), 'https://api.example.com/v1/images/generations')
        self.assertEqual(_normalize_api_endpoint('https://api.example.com', '/chat/completions'), 'https://api.example.com/v1/chat/completions')
        self.assertEqual(_normalize_api_endpoint('https://api.example.com/custom', '/images/generations'), 'https://api.example.com/custom')

    def test_task_api_url_blocks_obvious_ssrf_targets(self):
        from ai_workflow import WorkflowError, _validate_task_api_url
        with patch.dict(os.environ, {"PDD_AI_DEMO": "", "PDD_ALLOW_PRIVATE_API_URL": "0"}, clear=False):
            for url in ("http://127.0.0.1:3117/v1/images/generations", "http://localhost:8080", "http://192.168.1.10/api"):
                with self.assertRaises(WorkflowError):
                    _validate_task_api_url(url)
            _validate_task_api_url("https://api.example.com/v1/images/generations")


if __name__ == "__main__":
    unittest.main()
