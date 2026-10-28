"""Compile the pinned upstream ecommerce Skill; never silently use broken files."""
from __future__ import annotations
import hashlib
import json
import re
from pathlib import Path

UPSTREAM_COMMIT = "e7aa1996b962d4121b62d34199ed57234f8cf41a"


def _lock():
    from ai_workflow import WorkflowError
    try:
        return json.loads(Path(__file__).with_name("skill_template.lock.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WorkflowError("电商Skill版本清单缺失，请恢复发布文件", "template_version_changed") from exc


def version(root):
    files = sorted(Path(root).glob("*.json"))
    if not files:
        from ai_workflow import WorkflowError
        raise WorkflowError("电商Skill模板缺失，请恢复vendor/ecom-details-image后再生成")
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.name.encode()); digest.update(path.read_bytes())
    result = {"upstream_commit": UPSTREAM_COMMIT, "template_sha256": digest.hexdigest(), "installed_template_count": len(files)}
    expected = _lock()
    if any(expected.get(key) != value for key, value in result.items()):
        from ai_workflow import WorkflowError
        raise WorkflowError("电商Skill模板与锁定版本不一致，请恢复发布模板后再生成", "template_version_changed")
    return result


def compile_hint(root, template_id, *, product, category, style, model_scene=False, ad_copy=""):
    from ai_workflow import WorkflowError, build_campaign_style_lock
    path = next((path for path in Path(root).glob("*.json") if path.stem.endswith(template_id)), None)
    if not path:
        raise WorkflowError(f"电商Skill缺少模板{template_id}，请恢复模板后再生成")
    try:
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != _lock().get("files", {}).get(path.name):
            raise WorkflowError(f"电商Skill模板{template_id}已变更，请恢复锁定模板", "template_version_changed")
        data = json.loads(raw)
    except (ValueError, OSError) as exc:
        raise WorkflowError(f"电商Skill模板{template_id}读取失败，请恢复模板") from exc
    fields = dict(data.get("prompt_template") or {})
    variants = data.get("variants") or {}
    aliases = {"minimal": "feature-grid", "fresh": "morning"}
    variant = variants.get(style) or variants.get(aliases.get(style)) or {}
    fields.update(variant.get("overrides") or {})
    replacements = {"product_description": product, "product": product, "color": "#F5F1E8", "brand_name": "no invented brand",
                    "text": ad_copy or "short evidence-based headline", "headline": ad_copy or "visible product details",
                    "material_description": "preserve only visible photographed material and texture", "style": build_campaign_style_lock(style),
                    "scene": "a plausible context for the photographed product", "audience": "online shoppers", "platform": "PDD",
                    "model_description": "one generic synthetic adult" if model_scene else "no person", "aspect_ratio": "per module output canvas"}
    def expand(value):
        if isinstance(value, dict):
            return "; ".join(f"{key}: {expand(item)}" for key, item in value.items())
        if isinstance(value, list):
            return "; ".join(expand(item) for item in value)
        return re.sub(r"\{([^{}]+)\}", lambda m: replacements.get(m[1], "photographed product evidence for " + m[1].replace("_", " ")), str(value))
    parts = [expand(fields)]
    category_tip = (data.get("category_tips") or {}).get(category)
    if category_tip:
        parts.append("category guidance: " + expand(category_tip))
    if data.get("anti_ai_tips"):
        parts.append("anti-AI realism: " + expand(data["anti_ai_tips"]))
    parts.append("Template guidance yields to this module's explicit role, campaign style and reference evidence; no conflicting inherited layout, product facts or typography.")
    return "; ".join(parts)
