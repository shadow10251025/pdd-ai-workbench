"""Versioned, single-module revisions for the local image review workbench.

Only non-secret business manifests are persisted.  Provider credentials are
supplied again for each revision and never copied into the package on disk.
"""
from __future__ import annotations

import copy
import json
import os
import re
import secrets
import shutil
import time
from pathlib import Path
from typing import Any, Callable


_JOB_ID = re.compile(r"^[a-f0-9]{16}$")
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
_SECRET_FIELDS = {"api_key", "image_api_key", "prompt_api_key", "authorization", "provider_overrides", "traceback"}


def _without_secrets(value: Any) -> Any:
    from task_store import public_data
    return public_data(value)


def _job_dir(project_dir: Path, job_id: str) -> Path:
    from ai_workflow import WorkflowError
    if not _JOB_ID.fullmatch(str(job_id or "")):
        raise WorkflowError("图片任务编号无效，请从当前结果中选择图片")
    root = (Path(project_dir) / "data" / "ai_jobs").resolve()
    path = (root / job_id).resolve()
    if path.parent != root:
        raise WorkflowError("图片任务路径无效")
    return path


def persist_review_package(project_dir: Path, result: dict[str, Any]) -> None:
    """Atomically save a server-readable manifest, outside image previews."""
    directory = _job_dir(Path(project_dir), str(result.get("job_id") or ""))
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / "review_package.tmp"
    temporary.write_text(json.dumps(_without_secrets(result), ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(directory / "review_package.json")


def _load_package(project_dir: Path, job_id: str) -> dict[str, Any]:
    from ai_workflow import WorkflowError
    path = _job_dir(project_dir, job_id) / "review_package.json"
    if not path.is_file():
        raise WorkflowError("这份图片结果没有可编辑记录，或已过期，请重新生成商品包")
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WorkflowError("图片结果记录无法读取，请重新生成商品包") from exc
    if not isinstance(result, dict) or result.get("job_id") != job_id:
        raise WorkflowError("图片结果记录不匹配")
    return result


def _copy_package_assets(project_dir: Path, package: dict[str, Any], new_job_id: str) -> dict[str, Any]:
    """Retain validated immutable references, copying only the lightweight manifest."""
    from ai_workflow import WorkflowError
    project_dir = Path(project_dir).resolve()
    jobs_root = (project_dir / "data" / "ai_jobs").resolve()
    references = set(package.get("reference_job_ids") or [])
    def validate(value):
        if isinstance(value, dict):
            return {key: validate(item) for key, item in value.items()}
        if isinstance(value, list):
            return [validate(item) for item in value]
        if isinstance(value, str) and value.replace("\\", "/").startswith("data/ai_jobs/") and Path(value).suffix.lower() in _IMAGE_EXTS:
            path = (project_dir / value).resolve()
            if jobs_root not in path.parents or not path.is_file():
                raise WorkflowError("参考照片或上一版图片已过期，请重新上传后生成")
            references.add(path.relative_to(jobs_root).parts[0])
        return value
    result = validate(copy.deepcopy(package))
    result["reference_job_ids"] = sorted(references)
    return result


def regenerate_module(
    project_dir: Path,
    source_job_id: str,
    module_id: str,
    prompt: str = "",
    provider_overrides: dict[str, str] | None = None,
    progress: Callable[[str], None] | None = None,
    checkpoint: Callable[[dict], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Regenerate exactly one module, retaining the complete previous version."""
    from ai_workflow import (
        WorkflowConfig, WorkflowError, _build_quality_audit, _combine_reference_images,
        _identity_guard_text, _normalize_api_endpoint, _open_image,
        _render_verified_size_chart, _validate_task_api_url, crop_grid,
        generate_sheet, PDD_CAROUSEL_MAX_BYTES, PDD_DETAIL_MAX_BYTES,
    )
    progress = progress or (lambda _text: None)
    checkpoint = checkpoint or (lambda _value: None)
    should_cancel = should_cancel or (lambda: False)
    project_dir = Path(project_dir).resolve()
    source = _load_package(project_dir, source_job_id)
    outputs = source.get("module_outputs") or []
    original = next((item for item in outputs if isinstance(item, dict) and item.get("id") == module_id), None)
    if not original or not original.get("path"):
        raise WorkflowError("没有找到这张模块图片，请刷新生成结果后重试")
    requested_prompt = str(prompt or original.get("prompt") or "").strip()
    if not requested_prompt or len(requested_prompt) > 24000:
        raise WorkflowError("请填写本图提示词，长度不超过24000字")
    new_job_id = secrets.token_hex(8)
    progress("正在保留上一版，准备仅修改这张图片")
    revised = _copy_package_assets(project_dir, source, new_job_id)
    revised["job_id"] = new_job_id
    revised["parent_job_id"] = source_job_id
    revised["revision"] = int(source.get("revision") or 0) + 1
    revised["updated_at"] = time.time()
    target = next(item for item in revised["module_outputs"] if item.get("id") == module_id)
    old_path = target["path"]
    output_dir = _job_dir(project_dir, new_job_id) / "revision"
    output_dir.mkdir(parents=True, exist_ok=True)
    trace: dict[str, Any] = {"kind": target.get("kind", "detail"), "module_id": module_id, "rows": 1, "cols": 1, "revision": True}
    model_kind = "offline_demo"
    if target.get("module_type") == "size_chart":
        path = output_dir / "size_chart.jpg"
        page = target.get("table_page") or {}
        _render_verified_size_chart(page.get("rows") or revised.get("size_chart_rows") or [], path, style=revised.get("style") or "minimal", columns=page.get("columns"), page_label=target.get("page_label") or "")
        final_prompt = "本地精确排版，使用本页真实测量数据，不调用图片API"
        trace.update({"mode": "local", "reference_sent": False, "operation": "verified_size_chart"})
        model_kind = "verified_table"
    else:
        config = WorkflowConfig.from_env(project_dir)
        override = provider_overrides or {}
        custom_url = str(override.get("image_api_url") or "").strip()
        custom_key = str(override.get("image_api_key") or "").strip()
        if custom_url:
            _validate_task_api_url(custom_url)
            config.image_api_url = _normalize_api_endpoint(custom_url, "/images/generations")
        for field in ("image_model", "image_api_key"):
            value = str(override.get(field) or "").strip()
            if value:
                setattr(config, field, value[:600])
        mode = str(override.get("image_reference_mode") or "auto").strip().lower()
        if mode not in {"auto", "edit"}:
            raise WorkflowError("单图修改需要使用自动或编辑接口，才能保持商品外观")
        config.image_reference_mode = mode
        if custom_url or custom_key:
            config.provider_mode = "openai"
        demo_environment = os.getenv("PDD_AI_DEMO", "").strip().lower() in {"1", "true", "yes"}
        config.demo_asset_mode = bool(demo_environment and not (custom_url or custom_key))
        real = bool(config.image_api_url and config.provider_mode not in {"mock", "local"} and not config.demo_asset_mode)
        if source.get("generation_kind") == "ai" and not real:
            raise WorkflowError("请重新填写本次图片API配置后再修改，上一版图片已保留")
        source_paths = revised.get("source_images") or revised.get("input_images") or []
        raw_indices = original.get("reference_indices") or list(range(1, len(source_paths) + 1))
        indices = [index for index in raw_indices if isinstance(index, int) and 1 <= index <= len(source_paths)]
        if target.get("kind") in {"spec", "sku"} and not original.get("reference_indices"):
            raise WorkflowError("该规格缺少绑定的实物参考图，请先绑定对应颜色/款式的照片")
        if not indices:
            raise WorkflowError("这张图片缺少有效参考照片，请重新上传商品照片")
        refs = []
        for index in indices:
            ref_path = (project_dir / source_paths[index - 1]).resolve()
            if (project_dir / "data" / "ai_jobs").resolve() not in ref_path.parents:
                raise WorkflowError("参考照片路径无效")
            refs.append(_open_image(ref_path.read_bytes()))
        reference = _combine_reference_images(refs)
        style_lock = str((revised.get("prompt_pack") or {}).get("style_lock") or "").strip()
        from workflow_engine import _reference_guard
        import ai_workflow as w
        identity = _reference_guard(w, revised.get("metadata") or {}, indices, target.get("kind"), target.get("spec_value") or "")
        final_prompt = "\n".join([
            style_lock, requested_prompt, identity,
            "EXECUTION FORMAT OVERRIDE: Create exactly ONE standalone ecommerce image for this module. No contact sheet, no multi-cell grid, no inset copy of the reference photo. Preserve the photographed product identity.",
            "Use natural readable Chinese typography only where this module permits it. Never invent a brand, material, measurement, claim or a different colorway.",
            f"Revision direction {revised['revision']}: create a fresh composition while keeping the campaign style and product identity.",
        ])
        if target.get("kind") in {"hero", "spec", "sku"}:
            final_prompt += "\nOutput one square 1:1 image; no table, contact sheet or inset reference photo."
            if target.get("kind") != "hero" or target.get("module_type") != "scene":
                final_prompt += " No advertising typography, model or infographic labels."
        if target.get("kind") in {"spec", "sku"}:
            final_prompt += "\nThis is the exact variant " + str(target.get("spec_value") or "") + "; use only its bound reference photos and never recolor the product."
        sheet_path = output_dir / "generated.jpg"
        config.request_output_size = "1024x1024"
        def request_hook(state):
            if state == 'request_sent' and should_cancel():
                raise WorkflowError('任务已取消，本图请求未发送', 'cancelled')
            checkpoint({'job_id': new_job_id, 'module_id': module_id, 'request_state': state,
                        'provider_calls': int(real and state == 'response_received')})
        config.request_hook = request_hook
        sheet = generate_sheet(config, final_prompt, reference, sheet_path, 1, 1, progress, trace, refs)
        if min(sheet.width, sheet.height) - 32 < 480:
            raise WorkflowError("单图原生分辨率不足480px，上一版保留，请提高API输出尺寸后再修改")
        limit = PDD_CAROUSEL_MAX_BYTES if target.get("kind") in {"hero", "spec", "sku"} else PDD_DETAIL_MAX_BYTES
        paths = crop_grid(sheet, output_dir / "image", 1, 1, upscale=1.0, max_bytes=limit, square=target.get("kind") in {"hero", "spec", "sku"})
        path = Path(paths[0])
        margin=max(2,min(sheet.width//20,sheet.height//20,16))
        target.update({'source_sheet':sheet_path.relative_to(project_dir).as_posix(), 'source_width':sheet.width,'source_height':sheet.height,
                       'crop_box':[margin,margin,sheet.width-margin,sheet.height-margin], 'grid_cell':{'row':0,'col':0,'rows':1,'cols':1}})
        model_kind = "ai" if real else "demo_asset" if config.demo_asset_mode else "offline_demo"
    new_path = path.relative_to(project_dir).as_posix()
    versions = list(target.get("versions") or [])
    versions.append({"path": old_path, "prompt": target.get("prompt", ""), "batch_prompt": target.get("batch_prompt", ""), "generation_kind": target.get("generation_kind", source.get("generation_kind")), "revision": target.get("revision", 0)})
    target.update({"path": new_path, "prompt": requested_prompt, "batch_prompt": final_prompt, "needs_review": True, "generation_kind": model_kind, "revision": revised["revision"], "versions": versions, "style": revised.get("style") or "minimal"})
    revised["module_prompts"] = [dict(item, index=index + 1, role=item.get("label", item.get("module_type"))) for index, item in enumerate(revised["module_outputs"])]
    kinds = {item.get("generation_kind", source.get("generation_kind", "offline_demo")) for item in revised["module_outputs"] if item.get("render_source", "provider") == "provider"}
    revised["generation_kind"] = next(iter(kinds)) if len(kinds) == 1 else "mixed"
    revised["generated_images_are_ai"] = kinds == {"ai"}
    revised["content_review_status"] = "pending_manual"
    for field in ("main_images", "detail_images", "size_chart_files"):
        if isinstance(revised.get(field), list):
            revised[field] = [new_path if value == old_path else value for value in revised[field]]
    revised["spec_images"] = {key: new_path if value == old_path else value for key, value in (revised.get("spec_images") or {}).items()}
    calls = int(trace.get("mode") == "api" and not trace.get("cache_hit"))
    revised["generation_trace"] = list(revised.get("generation_trace") or []) + [trace]
    revised["provider_calls"] = calls
    revised["last_operation_provider_calls"] = calls
    revised["revised_module_id"] = module_id
    revised["cost_note"] = f"本次只修改1张图片，实际图片API请求{calls}次；其他图片与上一版保留。尺码数据精确排版不调用图片API。"
    revised["quality_audit"] = _build_quality_audit(
        [str(project_dir / value) for value in revised.get("main_images") or []],
        [str(project_dir / value) for value in revised.get("detail_images") or []],
        spec_paths=[str(project_dir / value) for value in (revised.get("spec_images") or {}).values()],
    )
    revised["status"] = "done"
    persist_review_package(project_dir, revised)
    return revised
