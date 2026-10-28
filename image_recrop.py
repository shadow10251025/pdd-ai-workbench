"""Human-directed image recropping, versioned without an image API charge."""
import copy
import secrets
from pathlib import Path

from PIL import Image


def recrop_module(project_dir: Path, source_job_id: str, module_id: str, box: dict):
    from ai_review import _job_dir, _load_package, persist_review_package
    from ai_workflow import WorkflowError, _build_quality_audit, _save_jpeg, PDD_CAROUSEL_MAX_BYTES, PDD_DETAIL_MAX_BYTES
    root = Path(project_dir).resolve()
    source = _load_package(root, source_job_id)
    result = copy.deepcopy(source)
    item = next((row for row in result.get("module_outputs", []) if row.get("id") == module_id), None)
    if not item or not item.get("path"):
        raise WorkflowError("没有找到可调整的模块图片")
    path = (root / (item.get('source_sheet') or item["path"])).resolve()
    if (root / "data" / "ai_jobs").resolve() not in path.parents or not path.is_file():
        raise WorkflowError("图片已过期，请重新生成")
    try:
        left, top, right, bottom = (float(box[key]) for key in ("left", "top", "right", "bottom"))
    except (KeyError, TypeError, ValueError) as exc:
        raise WorkflowError("裁剪边界格式不正确") from exc
    if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
        raise WorkflowError("裁剪边界应在0到1之间，且右下边界大于左上边界")
    with Image.open(path) as image:
        image.load()
        bounds = (round(left * image.width), round(top * image.height), round(right * image.width), round(bottom * image.height))
        crop = image.convert("RGB").crop(bounds)
    if min(crop.size) < 480:
        raise WorkflowError("这次裁剪的有效像素不足480，请扩大所选区域；本工具不会通过强行放大冒充高清")
    if item.get("kind") in {"hero", "spec", "sku"} and crop.width != crop.height:
        # Pad rather than stretching product proportions or cutting features.
        canvas = Image.new("RGB", (max(crop.size), max(crop.size)), "white")
        canvas.paste(crop, ((canvas.width - crop.width) // 2, (canvas.height - crop.height) // 2))
        crop = canvas
    job_id = secrets.token_hex(8)
    output_dir = _job_dir(root, job_id)
    output_dir.mkdir(parents=True)
    output = output_dir / "manual_crop.jpg"
    budget = PDD_CAROUSEL_MAX_BYTES if item.get("kind") in {"hero", "spec", "sku"} else PDD_DETAIL_MAX_BYTES
    _save_jpeg(crop, output, max_bytes=budget)
    old_path = item["path"]
    new_path = output.relative_to(root).as_posix()
    item.update({"path": new_path, 'crop_box': list(bounds), "manual_crop": {"source": str(path.relative_to(root).as_posix()), "box": box}, "needs_review": True, 'review_status': 'pending_manual'})
    for field in ("main_images", "detail_images", "size_chart_files"):
        result[field] = [new_path if value == old_path else value for value in result.get(field, [])]
    result["spec_images"] = {key: new_path if value == old_path else value for key, value in result.get("spec_images", {}).items()}
    result.update({"job_id": job_id, "parent_job_id": source_job_id, "revision": int(result.get("revision") or 0) + 1, "provider_calls": 0, "last_operation_provider_calls": 0, "status": "done", "revised_module_id": module_id, "cost_note": "本次仅在本地调整裁剪，图片API请求0次；上一版已保留。"})
    result["quality_audit"] = _build_quality_audit([str(root / value) for value in result.get("main_images", [])], [str(root / value) for value in result.get("detail_images", [])])
    persist_review_package(root, result)
    return result
