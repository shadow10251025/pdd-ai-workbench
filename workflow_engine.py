"""Executable, versioned image-pack workflow.

The planner owns every output ID and paid batch.  This module executes that
exact plan; metadata, assets and checkpoints remain free of provider secrets.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import secrets
import time
from itertools import islice, product
from pathlib import Path
from typing import Any


def _atomic(path: Path, value: Any) -> None:
    from ai_review import _without_secrets
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(_without_secrets(value), ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _config(w, project_dir, overrides):
    config = w.WorkflowConfig.from_env(project_dir)
    overrides = overrides or {}
    demo = w._flag(os.getenv("PDD_AI_DEMO", ""))
    image_override = bool(overrides.get("image_api_url") or overrides.get("image_api_key"))
    for field, suffix in (("image_api_url", "/images/generations"), ("prompt_api_url", "/chat/completions")):
        value = str(overrides.get(field) or "").strip()
        if value:
            w._validate_task_api_url(value)
            setattr(config, field, w._normalize_api_endpoint(value, suffix))
    for field in ("image_api_key", "prompt_api_key", "image_model", "prompt_model"):
        value = str(overrides.get(field) or "").strip()
        if value:
            setattr(config, field, value[:600])
    mode = str(overrides.get("image_reference_mode") or config.image_reference_mode or "auto").lower()
    if mode not in {"auto", "edit", "generation"}:
        raise w.WorkflowError("参考图模式无效")
    config.image_reference_mode = mode
    if image_override:
        config.provider_mode = "openai"
    config.demo_asset_mode = demo and not image_override
    real = bool(config.image_api_url and config.provider_mode not in {"mock", "local"} and not config.demo_asset_mode)
    kind = "ai" if real else "demo_asset" if config.demo_asset_mode and config.image_api_url else "offline_demo"
    return config, kind


def _reference_guard(w, metadata, indices, kind, value):
    if kind == "spec":
        # Main-product metadata may describe another color.  Bound images
        # are the sole authority for the variant's actual color/artwork.
        meta = dict(metadata)
        meta["identity_evidence"] = []
        meta["identity_constraints"] = []
        guard = w._identity_guard_text(meta, len(indices))
        return guard + f" Only the bound reference photo(s) for variant「{value}」determine its exact colorway; do not use any other variant's colors or recolor it."
    return w._identity_guard_text(metadata, len(indices))


def _module_prompt(w, entry, index, pack, metadata, product_hint, model_scene, ad_copy, chart_rows):
    lock = pack["style_lock"]
    blocks = metadata.get("creative_blocks") or []
    scenes = metadata.get("scene_plan") or []
    block = next((block for block in blocks if block.get("role") == entry["module_type"]), blocks[index % len(blocks)] if blocks else {})
    scene = scenes[index % len(scenes)] if scenes else {}
    kinds = {
        "hook": "E-commerce infographic opening: one clear benefit headline and the full product, generous whitespace.",
        "pain_point": "E-commerce infographic problem/use context: show a reasonable usage situation without exaggerating an unsupported problem or result.",
        "mechanism": "E-commerce infographic construction: callout lines to visible supported parts; do not invent mechanisms, ports or hidden structure.",
        "benefit": "E-commerce infographic benefit: one photograph with a short headline and 2-3 evidence-supported labels integrated naturally.",
        "proof": "E-commerce infographic macro: show real visible texture, material appearance or craftsmanship, with sparse detail callouts.",
        "angle": "An alternate evidence-supported product angle with a restrained editorial caption; no invented hidden surfaces.",
        "carousel_sell": "E-commerce supporting carousel benefit composition with one short approved caption, no price or unverified claim.",
        "usage": "E-commerce infographic usage: numbered steps only for observable, supported usage; avoid undocumented safety or performance promises.",
        "scene": "E-commerce lifestyle image: natural product interaction in a suitable creative setting, with an integrated short headline.",
        "comparison": "E-commerce infographic choice guide: compare only verified variants or visible details of this product; no fabricated competitor claims.",
        "packaging": "E-commerce infographic packaging: show only included items and packaging visible in references; omit unconfirmed accessories.",
        "trust_faq": "E-commerce infographic FAQ: address visible product details and seller-confirmed facts; no invented testimonials or trust badges.",
        "closing_cta": "E-commerce editorial closing image: product and one neutral call to view options, no invented prices, offers, urgency or guarantees.",
        "sku_preview": "Variant overview: show only the individually bound variants listed in the plan. Missing variants remain marked 待补充; never synthesize a color.",
        "size_chart": "Verified size-chart module. All numbers and units come from the merchant rows and are rendered deterministically, not invented by the image model.",
        "white_pack": "Pure white #FFFFFF background, complete centered product, no text, collage, props or people. Square first carousel image.",
        "flat_lay": "Evidence-supported top-down product flat lay, no unsupported accessories, clean lighting and generous whitespace.",
    }
    role = entry["module_type"]
    purpose = kinds.get(role, "An independent ecommerce product image using visible evidence.")
    if entry["kind"] == "hero":
        blueprints = w._category_blueprints(product_hint, "hero")
        purpose = blueprints[index % len(blueprints)]
        if index == 0:
            purpose = kinds["white_pack"]
    if entry["kind"] == "spec":
        purpose = f"Exactly one square SKU preview for「{entry['spec_value']}」. Full physical product on #FFFFFF, no advertising text or other variants. Use only its bound reference photos."
    template_ids, template_hints = w._template_hints(product_hint, 9, pack.get("style") or "minimal", model_scene, ad_copy)
    template = template_hints[index % len(template_hints)] if template_hints else ""
    entry['template_id'] = template_ids[index % len(template_ids)] if template_ids else ''
    parts = [lock, f"MODULE {entry['id']} [{entry['label']}]. Product hint is tentative: {product_hint or 'the photographed product'}.", purpose,
             "Skill template: " + template,
             "Composition: one focal point, product occupies 35-45% in contextual shots and 55-65% in detail shots. Keep generous whitespace. Vary close/medium/context framing and use natural captions/callout lines instead of identical boxes."]
    if entry["kind"] == "detail":
        if scene:
            parts.append("Scene/camera: " + " / ".join(str(scene.get(key) or "") for key in ("setting", "time_of_day", "camera", "lighting", "action") if scene.get(key)))
        parts.append(f"Independent variation {index + 1}; do not repeat the same crop, background, slogan or label layout as neighboring panels.")
        copy_lines = {key: block[key] for key in ("headline", "subheadline", "labels", "cta") if block.get(key)}
        parts.append("Approved short copy: " + json.dumps(copy_lines, ensure_ascii=False) if copy_lines else "Use a short neutral Chinese headline about visible product details; omit unsupported copy.")
        parts.append("Keep one readable headline, at most 2-4 short labels and under 50 Chinese characters in total. Integrate the typography in scene whitespace; no rigid repeated card.")
    elif entry["kind"] == "hero" and role == "scene" and (model_scene or ad_copy):
        parts.append(w._ad_scene_instruction(model_scene, ad_copy))
    else:
        parts.append("Clean product image: no advertising text, table, model, collage, icons or QR code.")
    if not model_scene or entry["kind"] == "spec" or role == "white_pack":
        parts.append("No person, face or hand in this module.")
    elif role == "scene":
        parts.append("Use one synthetic adult model where appropriate, generic identity, natural pose and realistic product fit, never copying a reference person's identity.")
    parts.append(_reference_guard(w, metadata, entry.get("reference_indices") or [1], entry["kind"], entry.get("spec_value") or ""))
    parts.append("Never add watermarks, fake logos, unverified certification, dimensions, ingredients, performance numbers, ratings, sales, medical claims or extra products. Preserve physical product color, shape and artwork. No care or washing-information panel.")
    if role == "size_chart":
        parts.append("EXACT MERCHANT DATA: " + json.dumps(chart_rows, ensure_ascii=False))
    return "\n".join(parts)


def execute_workflow(**kwargs):
    import ai_workflow as w
    from ai_review import persist_review_package, _job_dir
    project_dir = Path(kwargs["project_dir"]).resolve()
    progress = kwargs.get("progress") or (lambda _text: None)
    checkpoint = kwargs.get("checkpoint") or (lambda _data: None)
    should_cancel = kwargs.get("should_cancel") or (lambda: False)
    if should_cancel():
        raise w.WorkflowError('任务已取消，尚未发送识别或生图请求', 'cancelled')
    strategy = (kwargs.get("strategy") or os.getenv("PDD_AI_GENERATION_STRATEGY", "quality")).strip().lower()
    strategy = "grid" if strategy in {"grid", "economy"} else "quality"
    rows, cols = max(1, min(int(kwargs.get("rows") or 3), 4)), max(1, min(int(kwargs.get("cols") or 3), 4))
    hint, style = str(kwargs.get("product_hint") or "")[:200], str(kwargs.get("style") or "minimal")[:30]
    model_scene = w._flag(kwargs.get("model_scene"))
    ad_copy = str(kwargs.get("ad_copy") or "")[:160]
    density = "conservative" if str(kwargs.get("creative_density") or "rich") == "conservative" else "rich"
    config, generation_kind = _config(w, project_dir, kwargs.get("provider_overrides"))
    approved=kwargs.get('approved_proposal')
    deadline = time.monotonic() + max(1, min(int(kwargs.get("deadline_seconds") or 1200), 7200))
    config.timeout_seconds = min(config.timeout_seconds, max(1, int(deadline - time.monotonic())))
    resume = str(kwargs.get("resume_job_id") or "")
    previous = None
    if resume:
        out_dir = _job_dir(project_dir, resume)
        path = out_dir / "workflow_checkpoint.json"
        if not path.is_file():
            raise w.WorkflowError("没有可恢复的出图记录，请重新生成")
        previous = json.loads(path.read_text(encoding="utf-8"))
        job_id = resume
        input_paths = [project_dir / value for value in previous["source_images"]]
        if any(out_dir not in path.resolve().parents for path in input_paths):
            raise w.WorkflowError("恢复任务参考照片路径无效")
        raw_images = [path.read_bytes() for path in input_paths]
    else:
        data = kwargs.get("image_bytes") or []
        raw_images = data if isinstance(data, list) else [data]
        if not raw_images or len(raw_images) > 12 or sum(len(data) for data in raw_images) > w.MAX_UPLOAD_BYTES:
            raise w.WorkflowError("请上传1到12张商品照片，总大小不超过限制")
        job_id = secrets.token_hex(8)
        out_dir = _job_dir(project_dir, job_id)
        out_dir.mkdir(parents=True, exist_ok=True)
        input_paths = []
    images = [w._open_image(raw) for raw in raw_images]
    reference = w._combine_reference_images(images)
    if not previous:
        for index, image in enumerate(images, 1):
            path = out_dir / f"input_{index:02d}.jpg"
            w._save_jpeg(image, path)
            input_paths.append(path)
    reference_path = out_dir / "reference_board.jpg"
    w._save_jpeg(reference, reference_path, max_edge=w.MAX_LONG_EDGE * 2)
    rel = lambda path: Path(path).relative_to(project_dir).as_posix()
    prompt_pack = w.build_quality_prompt_packs(hint, style, rows, cols, model_scene, ad_copy) if strategy == "quality" else w.build_prompt_pack(hint, style, rows, cols, model_scene, ad_copy)
    prompt_pack["style"] = style
    skill_version = w.skill_template_version()
    if previous:
        if previous.get("skill_version") and previous["skill_version"] != skill_version:
            raise w.WorkflowError("电商Skill模板版本已变更，不能以新提示词续跑旧任务；请恢复原版本或建立新方案", "template_version_changed")
        metadata = previous["metadata"]
        plan = previous["plan"]
        size_chart_rows = previous.get("size_chart_rows") or []
    elif approved:
        if approved.get('stage')!='confirmed' or approved.get('skill_version')!=skill_version:
            raise w.WorkflowError('图片方案尚未确认或模板版本已变化，请重新确认','proposal_not_confirmed')
        if [hashlib.sha256(raw).hexdigest() for raw in raw_images]!=approved.get('input_fingerprints'):
            raise w.WorkflowError('照片与已确认方案不一致，请重新识图策划','proposal_inputs_changed')
        metadata=copy.deepcopy(approved['metadata']);plan=copy.deepcopy(approved['generation_plan']);size_chart_rows=copy.deepcopy(approved.get('size_chart_rows') or [])
        prompt_pack=copy.deepcopy(approved['prompt_pack'])
        progress('正在执行已确认方案；不再重新调用识图或策划AI')
    else:
        if should_cancel():
            raise w.WorkflowError('任务已取消，未发送商品识别请求', 'cancelled')
        progress("正在识别商品并准备可审核的图片方案")
        metadata = w.generate_metadata(config, hint, prompt_pack, reference, progress, reference_images=images, creative_density=density)
        metadata = w._limit_creative_brief(metadata, density, hint)
        if isinstance(kwargs.get("creative_brief_override"), dict):
            for field in ("selling_points", "scene_suggestions", "ad_headlines"):
                if field in kwargs["creative_brief_override"]:
                    metadata[field] = w._clean_text_items(kwargs["creative_brief_override"][field], max_items=6 if density == "rich" else 3, max_len=80 if field != "ad_headlines" else 20)
            metadata["creative_blocks"] = w._fallback_creative_blocks(metadata, density)
            metadata["scene_plan"] = w._fallback_scene_plan(metadata, hint, max_items=20)
        selected = w._clean_spec_map(kwargs.get("selected_specs")) if kwargs.get("selected_specs") is not None else metadata.get("specs_confirmed") or {}
        metadata["specs_confirmed"] = selected
        metadata["specs"] = selected or metadata.get("specs") or {}
        metadata["specs_status"] = "confirmed" if selected else "needs_confirmation"
        size_chart_rows = w._clean_size_chart_specs(kwargs.get("size_chart_specs"))
        sizes = w._selected_size_values(selected)
        if w._flag(kwargs.get("size_chart_only_selected"), True) and sizes:
            data_by_size = {item["size"]: item for item in size_chart_rows}
            size_chart_rows = [data_by_size.get(size, {"size": size, "measurements": {}, "needs_measurement": True}) for size in sizes]
        if not size_chart_rows and sizes:
            size_chart_rows = [{"size": size, "measurements": {}, "needs_measurement": True} for size in sizes]
        plan = w.build_generation_plan(product_hint=hint, style=style, rows=rows, cols=cols, strategy=strategy,
            detail_modules=kwargs.get("detail_modules"), generation_rounds=kwargs.get("generation_rounds") or 0,
            round_strategy=kwargs.get("round_strategy") or "auto", auto_module_plan=kwargs.get("auto_module_plan", True),
            size_chart_enabled=kwargs.get("size_chart_enabled", False), size_chart_specs=size_chart_rows,
            selected_specs=selected, variant_references=kwargs.get("variant_references"), reference_count=len(images),
            module_recommendations=metadata.get("module_recommendations"), hero_count=kwargs.get("hero_count", 6),
            max_image_requests=kwargs.get("max_image_requests", 0), output_size=kwargs.get("output_size") or "", size_chart_only_selected=kwargs.get("size_chart_only_selected", True))
    combinations = 1
    for values in (metadata.get("specs_confirmed") or {}).values():
        combinations *= len(values)
    if combinations > 200:
        raise w.WorkflowError("规格组合超过200个，请减少已选规格后再生成")
    w._inject_identity_guard_prompt(prompt_pack, metadata, len(images))
    w._inject_creative_brief_prompt(prompt_pack, metadata)
    w._inject_detail_module_prompt(prompt_pack, plan["detail_module_plan"], w._flag(kwargs.get("size_chart_enabled")), size_chart_rows, w._selected_variant_values(metadata.get("specs_confirmed") or {}))
    entries = copy.deepcopy(plan["module_outputs"])
    for index, entry in enumerate(entries):
        if not (approved or (previous or {}).get('approved')):
            entry["prompt"] = _module_prompt(w, entry, index, prompt_pack, metadata, hint, model_scene, ad_copy, size_chart_rows)
    lookup = {entry["id"]: entry for entry in entries}
    state = previous or {"schema_version": 2, "job_id": job_id, "source_images": [rel(path) for path in input_paths], "metadata": metadata, "plan": plan, "skill_version": skill_version, "size_chart_rows": size_chart_rows, "batches": {}, "created_at": time.time(), 'approved':bool(approved),'proposal_id':(approved or {}).get('proposal_id',''),'confirmation_digest':(approved or {}).get('confirmation_digest','')}
    state["status"] = "running"
    def save(stage):
        state["stage"] = stage; state["updated_at"] = time.time()
        _atomic(out_dir / "workflow_checkpoint.json", state)
        current = next(reversed(state["batches"].values()), {}) if state["batches"] else {}
        request_state = current.get("status", stage)
        checkpoint({"job_id": job_id, "stage": stage, "request_state": request_state, "batch": current.get("round"), "status": state["status"], "planned_image_requests": len(plan["round_plan"]),
                    "provider_calls": sum(int(batch.get("api_response_received", False)) for batch in state["batches"].values()),
                    "sent_requests": sum(int(batch.get("api_sent", False)) for batch in state["batches"].values()),
                    "completed_batches": sum(1 for batch in state["batches"].values() if batch.get("status") == "done")})
    save("planned")
    traces, sheets = [], []
    try:
        for batch in plan["round_plan"]:
            batch_id = str(batch["round"])
            cached = state["batches"].get(batch_id)
            if cached and cached.get("status") == "done":
                assets = cached.get("assets") or {}
                if all((project_dir / path).is_file() for path in assets.values()) and set(assets) == set(batch["module_ids"]):
                    for module_id, path in assets.items():
                        lookup[module_id].update((cached.get("outputs") or {}).get(module_id) or {})
                        lookup[module_id].update({"path": path, "generation_kind": cached.get("generation_kind", generation_kind), "batch_prompt": cached.get("prompt", "")})
                    trace = dict(cached.get("trace") or {}); trace["resumed"] = True
                    traces.append(trace)
                    if cached.get("sheet"):
                        sheets.append(cached["sheet"])
                    continue
            if cached and cached.get("status") in {"sent", "billing_unknown", "failed_charged"}:
                message = "上一请求已返回但图片未通过检查，本次已发生图片请求；请审核服务商记录后建立新方案，不会自动重复计费" if cached.get("status") == "failed_charged" else "上一请求已发送但扣费或返回状态未知；请先核对服务商记录后重新建立任务，避免重复计费"
                raise w.WorkflowError(message, "failed_charged" if cached.get("status") == "failed_charged" else "billing_unknown")
            if time.monotonic() >= deadline:
                raise w.WorkflowError("本次处理时间已达上限，已完成批次保留；可恢复尚未发送的批次", "deadline_exceeded")
            if should_cancel():
                state["status"] = "cancelled"; save("cancelled")
                raise w.WorkflowError("任务已取消，已完成图片保留；尚未发送的图片请求未执行", "cancelled")
            members = [lookup[module_id] for module_id in batch["module_ids"]]
            indices = batch.get("reference_indices") or list(range(1, len(images) + 1))
            refs = [images[index - 1] for index in indices]
            board = w._combine_reference_images(refs)
            from creative_proposal import batch_prompt as compose_batch_prompt
            batch_prompt=compose_batch_prompt(prompt_pack,batch,members)
            trace = {"kind": batch["role"], "round": batch["round"], "rows": batch["rows"], "cols": batch["cols"], "module_ids": batch["module_ids"], "requested_panels": len(members), "request_status": "sent"}
            sheet_path = out_dir / f"generated_batch_{batch['round']:02d}.jpg"
            state["batches"][batch_id] = {"status": "preparing", "round": batch["round"], "module_ids": batch["module_ids"], "prompt": batch_prompt}
            save("preparing")
            progress(f"正在生成第{batch['round']}/{len(plan['round_plan'])}批，共{len(members)}张")
            # Per-request output size is passed through config to avoid global
            # environment mutation across tenants or concurrent tasks.
            config.request_output_size = batch["output_size"]
            config.timeout_seconds = min(config.timeout_seconds, max(1, int(deadline - time.monotonic())))
            def request_hook(request_state):
                if should_cancel() and request_state == "request_sent":
                    state["status"] = "cancelled"; save("cancelled")
                    raise w.WorkflowError("任务已取消，未发送本批图片请求", "cancelled")
                record = state["batches"][batch_id]
                record["status"] = "sent" if request_state == "request_sent" else "response_received"
                record["api_sent"] = generation_kind == "ai"
                record["api_response_received"] = generation_kind == "ai" and request_state == "response_received"
                save(request_state)
            config.request_hook = request_hook
            sheet = w.generate_sheet(config, batch_prompt, board, sheet_path, batch["rows"], batch["cols"], progress, trace, refs)
            if generation_kind == "ai" and trace.get("mode") == "api" and not trace.get("cache_hit"):
                state["batches"][batch_id].update(status="response_received", api_sent=True, api_response_received=True)
                save("response_received")
            margin = max(2, min(sheet.width // (batch["cols"] * 20), sheet.height // (batch["rows"] * 20), 16))
            native_edge = min(sheet.width // batch["cols"], sheet.height // batch["rows"]) - margin * 2
            if native_edge < w.PDD_MIN_IMAGE_EDGE:
                raise w.WorkflowError(f"本批裁剪后原生边长约{native_edge}px，低于480px；已停止，不能靠放大冒充清晰图片，请提高输出尺寸或减少每批数量")
            crops = w.crop_grid(sheet, out_dir / f"batch_{batch['round']:02d}", batch["rows"], batch["cols"], upscale=1.0, max_bytes=min(w.PDD_CAROUSEL_MAX_BYTES, w.PDD_DETAIL_MAX_BYTES))
            assets = {}
            for cell_index, (entry, path) in enumerate(zip(members, crops)):
                target = out_dir / "outputs" / f"{entry['id']}.jpg"
                with w.Image.open(path) as crop:
                    if entry["kind"] in {"hero", "spec"}:
                        side = max(crop.width, crop.height)
                        crop = w._fit_image(crop, (side, side), bg=(255, 255, 255))
                    w._save_jpeg(crop, target, max_bytes=w.PDD_CAROUSEL_MAX_BYTES if entry["kind"] in {"hero", "spec"} else w.PDD_DETAIL_MAX_BYTES)
                row, col = divmod(cell_index, batch["cols"])
                box = [round(col * sheet.width / batch["cols"]) + margin, round(row * sheet.height / batch["rows"]) + margin,
                       round((col + 1) * sheet.width / batch["cols"]) - margin, round((row + 1) * sheet.height / batch["rows"]) - margin]
                diagnostics = _crop_diagnostics(w, sheet, box, batch["rows"], batch["cols"])
                entry.update({"path": rel(target), "generation_kind": generation_kind, "batch_prompt": batch_prompt, "native_min_edge": native_edge,
                              "source_sheet": rel(sheet_path), "source_width": sheet.width, "source_height": sheet.height, "crop_box": box, "grid_cell": {"row": row, "col": col, "rows": batch["rows"], "cols": batch["cols"]},
                              "crop_diagnostics": diagnostics, "crop_review": "manual", "needs_review": True})
                assets[entry["id"]] = entry["path"]
            trace["request_status"] = "completed"
            traces.append(trace); sheets.append(rel(sheet_path))
            sent = generation_kind == "ai" and trace.get("mode") == "api" and not trace.get("cache_hit")
            state["batches"][batch_id].update({"status": "done", "assets": assets, "outputs": {entry["id"]: dict(entry) for entry in members}, "sheet": rel(sheet_path), "trace": trace, "generation_kind": generation_kind, "api_sent": sent, "api_response_received": sent})
            save("batch_completed")
    except Exception as exc:
        if state["status"] != "cancelled":
            state["status"] = "error"
            for batch in state["batches"].values():
                if batch.get("status") == "sent":
                    batch["status"] = "billing_unknown" if generation_kind == "ai" else "failed"
                elif batch.get("status") == "response_received":
                    batch["status"] = "failed_charged" if generation_kind == "ai" else "failed"
            state["error_code"] = "provider_or_output_error"
            if any(batch.get("status") == "billing_unknown" for batch in state["batches"].values()):
                state["error_code"] = "billing_unknown"
            elif any(batch.get("status") == "failed_charged" for batch in state["batches"].values()):
                state["error_code"] = "failed_charged"
            if isinstance(exc, w.WorkflowError) and state["error_code"] != "provider_or_output_error":
                exc.code = state["error_code"]
            save("error")
        raise
    for entry in entries:
        if entry["render_source"] == "provider":
            continue
        path = out_dir / "outputs" / f"{entry['id']}.jpg"
        if entry["module_type"] == "size_chart":
            page = entry.get("table_page") or {"rows": size_chart_rows, "columns": []}
            w._render_verified_size_chart(page.get("rows") or size_chart_rows, path, style=style, columns=page.get("columns") or None, page_label=entry.get("page_label") or "")
        else:
            _variant_overview(w, path, metadata.get("specs_confirmed") or {}, entries, project_dir, style, entry.get("variant_values"))
        entry.update({"path": rel(path), "generation_kind": "verified_table" if entry["module_type"] == "size_chart" else "reference_overview", "needs_review": True, "batch_prompt": "本地精确排版，不调用图片API"})
    main = [entry["path"] for entry in entries if entry["kind"] == "hero"]
    detail = [entry["path"] for entry in entries if entry["kind"] == "detail"]
    specs = {entry["spec_value"]: entry["path"] for entry in entries if entry["kind"] == "spec"}
    confirmed = metadata.get("specs_confirmed") or {}
    dimensions = list(confirmed.items())
    skus = []
    combos = product(*(values for _, values in dimensions)) if dimensions else [("默认",)]
    for combo in islice(combos, 201):
        values = dict(zip((name for name, _ in dimensions), combo))
        from product_package import sku_id_for_specs
        sku_id = sku_id_for_specs(values)
        skus.append({"id": sku_id, "spec_values": values, "name": " ".join(combo), "price": 0, "single_price": 0, "stock": 0, "enabled": True, "spec_code": str(len(skus) + 1)})
    if len(skus) > 200:
        raise w.WorkflowError("规格组合超过200个，请减少已选规格，避免静默丢失库存组合")
    api_calls = sum(int(batch.get("api_response_received", False)) for batch in state["batches"].values())
    references_sent = bool(traces) and all(trace.get("reference_sent") for trace in traces)
    quality = w._build_quality_audit([str(project_dir / path) for path in main], [str(project_dir / path) for path in detail], spec_paths=[str(project_dir / path) for path in specs.values()])
    if generation_kind != "ai":
        quality["status"] = "review"
    prompt_pack.update({"mode": strategy if strategy == "quality" else "grid", "module_plan": plan["module_plan"], "round_plan": plan["round_plan"], "generation_rounds": len(traces), "round_strategy": kwargs.get("round_strategy") or "auto", "skill_version": skill_version, "installed_template_count": skill_version["installed_template_count"],
                        'template_ids':list(dict.fromkeys(entry['template_id'] for entry in entries if entry.get('template_id') and entry['render_source']=='provider'))})
    previews = [dict(entry, index=index + 1, role=entry["label"]) for index, entry in enumerate(entries)]
    result = {"schema_version": 2, "status": "done", "job_id": job_id, "style": style, "mode": config.provider_mode, "strategy": strategy,
        'proposal_id':state.get('proposal_id',''),'confirmation_digest':state.get('confirmation_digest',''),'confirmed_before_generation':bool(state.get('approved')),
        "model_scene": model_scene, "ad_copy": ad_copy, "creative_density": density,
        "generation_kind": generation_kind, "generated_images_are_ai": generation_kind == "ai", "content_review_status": "pending_manual",
        "source_image_count": len(images), "source_image_fingerprints": [hashlib.sha256(raw).hexdigest()[:16] for raw in raw_images],
        "reference_coverage": w._reference_coverage(len(images)), "identity_risk_flags": w._reference_coverage(len(images))["risk_flags"],
        "generation_trace": traces, "generation_plan": plan, "round_plan": plan["round_plan"], "generation_rounds": len(traces), "round_strategy": kwargs.get("round_strategy") or "auto",
        "planned_image_requests": plan["planned_image_requests"], "module_plan_source": plan["module_plan_source"], "plan_warnings": plan["warnings"],
        "detail_module_plan": plan["detail_module_plan"], "requested_detail_count": plan["detail_count"], "generated_detail_count": len(detail), "module_outputs": entries,
        "size_chart_enabled": w._flag(kwargs.get("size_chart_enabled")), "size_chart_rows": size_chart_rows,
        "size_chart_files": [entry["path"] for entry in entries if entry["module_type"] == "size_chart"],
        "reference_images_sent": references_sent, "reference_mode": "edit" if references_sent else "generation" if any(trace.get("endpoint_kind") == "generations" for trace in traces) else "offline",
        "provider_calls": api_calls if generation_kind == "ai" else 0, "cache_hits": sum(1 for trace in traces if trace.get("cache_hit")), "cache_policy": "reuse_same_input" if w._image_cache_enabled() else "disabled",
        "cost_note": f"计划{plan['planned_image_requests']}次图片请求，本次实际发送{api_calls if generation_kind == 'ai' else 0}次外部图片请求；未配置真实API时为离线演示。仅人工内容审核，不追加视觉AI费用。",
        "quality_note": "按裁剪后原生像素检查，放大不恢复细节。文件检查不代表商品、颜色或中文内容已审核。", "quality_warnings": plan["warnings"], "quality_audit": quality,
        "prompt_pack": prompt_pack, "module_prompts": previews, "skill": "ecom-details-image", "skill_source": prompt_pack.get("skill_source"), "skill_template_count": prompt_pack.get("skill_template_count", 25),
        "metadata": metadata, "selected_specs": confirmed, "skus": skus, "spec_images": specs, "spec_preview_missing": plan["spec_preview_missing"], "spec_images_evidence_bound": bool(specs),
        "spec_images_note": "只有绑定对应实物参考图的专图进入SKU；缺失规格需补充参考，不使用普通详情图代替。",
        "main_images": main, "detail_images": detail, "source_images": [rel(path) for path in input_paths], "input_images": [rel(path) for path in input_paths], "input_image": rel(input_paths[0]), "reference_board": rel(reference_path), "generated_sheet": sheets[0] if sheets else "", "generated_sheets": sheets,
        "platform_constraints": {"pdd_main_max_count": 10, "pdd_main_min_edge": 480, "pdd_main_max_bytes": w.PDD_CAROUSEL_MAX_BYTES, "pdd_main_recommended_ratios": ["1:1"], "pdd_detail_max_count": 20, "pdd_detail_min_width": 480, "pdd_detail_max_bytes": w.PDD_DETAIL_MAX_BYTES, "pdd_detail_preferred_width": 750, "pdd_detail_soft_max_height": 1500, "max_file_bytes": max(w.PDD_CAROUSEL_MAX_BYTES, w.PDD_DETAIL_MAX_BYTES), "notes": "保守PDD预设，最终以当前商家发布页校验为准；不自动点击发布。"}}
    for field in ("title", "category", "brand", "attrs", "description", "care_instructions", "care_status", "care_note", "identity_evidence", "identity_constraints", "uncertain_features", "identity_confidence", "identity_note", "selling_points", "scene_suggestions", "scene_plan", "ad_headlines", "creative_blocks", "specs", "specs_confirmed", "specs_status", "spec_suggestions", "spec_direction_note"):
        result[field] = metadata.get(field)
    state["status"] = "done"; save("done")
    persist_review_package(project_dir, result)
    return result


def _crop_diagnostics(w, sheet, box, rows, cols):
    """Cheap gutter evidence only; it cannot certify product-content boundaries."""
    if rows * cols == 1:
        return {"status": "single_image", "requires_manual_review": True}
    left, top, right, bottom = box
    strips = [sheet.crop((max(0, left - 4), top, left, bottom)), sheet.crop((right, top, min(sheet.width, right + 4), bottom)),
              sheet.crop((left, max(0, top - 4), right, top)), sheet.crop((left, bottom, right, min(sheet.height, bottom + 4)))]
    white_fractions = []
    for strip in strips:
        if min(strip.size) <= 0:
            continue
        sample = strip.convert("RGB"); sample.thumbnail((128, 128))
        pixels = list(getattr(sample,'get_flattened_data',sample.getdata)())
        white_fractions.append(sum(min(pixel) >= 235 for pixel in pixels) / max(1, len(pixels)))
    suspect = not white_fractions or min(white_fractions) < .8
    return {"status": "suspected_cross_cell_or_missing_gutter" if suspect else "gutters_detected", "edge_white_fraction": [round(value, 3) for value in white_fractions],
            "requires_manual_review": True, "note": "本地仅检查边界留白；请放大核对跨格文字、商品完整性后采用"}


def _variant_overview(w, path, specs, entries, project_dir, style, variant_values=None):
    """A cheap exact overview made only from generated bound SKU previews."""
    from PIL import Image, ImageDraw, ImageFont
    values = variant_values if variant_values is not None else w._selected_variant_values(specs)
    if not values:
        values = ["待补充规格"]
    width, row_height = 750, 220
    height = max(600, 120 + row_height * min(8, len(values)))
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", 26)
    except OSError:
        font = ImageFont.load_default()
    draw.text((40, 35), "规格选项 · 请按实物核对", font=font, fill="#2D2D2D")
    for index, value in enumerate(values[:8]):
        top = 100 + index * row_height
        entry = next((entry for entry in entries if entry["kind"] == "spec" and entry["spec_value"] == value and entry.get("path")), None)
        if entry:
            with Image.open(project_dir / entry["path"]) as source:
                image.paste(w._fit_image(source, (180, 180), bg=(255, 255, 255)), (40, top))
        draw.text((260, top + 60), value + ("" if entry else " · 待补充实物图"), font=font, fill="#2D2D2D")
    w._save_jpeg(image, path, max_edge=2250, max_bytes=w.PDD_DETAIL_MAX_BYTES)
