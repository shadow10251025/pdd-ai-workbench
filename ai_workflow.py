"""AI 电商生图与商品资料流水线。

这个模块把「一张用户自有商品照片」变成可供 PDD 上架器消费的商品包：

* 先生成一份可复用的 Campaign Style Lock 和分镜 Prompt；
* 默认只调用一次生图 API，要求模型返回 2x3 网格详情图；
* 本地按网格裁剪、放大、转成 PDD 友好的 JPEG；
* 再用提示词 API 生成标题/规格/属性，未知字段保持为空，不编造证明；
* 没有配置 API 时使用本地 mock，方便先跑通完整流程和验证上架器。

真实 API 密钥只从环境变量读取，绝不写入任务 JSON、日志或前端响应。
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import io
import json
import os
import re
import secrets
import shutil
from urllib.parse import urlparse
import textwrap
import time
import urllib.request
from dataclasses import dataclass
from itertools import islice, product as iter_product
from pathlib import Path
from typing import Any, Callable

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps, ImageStat, UnidentifiedImageError


MAX_UPLOAD_BYTES = int(os.getenv("PDD_AI_MAX_UPLOAD_BYTES", str(12 * 1024 * 1024)))
MAX_LONG_EDGE = int(os.getenv("PDD_AI_MAX_LONG_EDGE", "4096"))
MAX_OUTPUT_BYTES = int(os.getenv("PDD_AI_MAX_OUTPUT_BYTES", str(3 * 1024 * 1024)))
# Keep the internal provider sheet generous, then enforce the stricter PDD
# budget only on the files that enter the upload package.  The limits can be
# raised per merchant after checking the current logged-in merchant console.
PDD_CAROUSEL_MAX_BYTES = int(os.getenv("PDD_CAROUSEL_MAX_BYTES", str(1 * 1024 * 1024)))
PDD_DETAIL_MAX_BYTES = int(os.getenv("PDD_DETAIL_MAX_BYTES", str(1 * 1024 * 1024)))
PDD_MIN_IMAGE_EDGE = int(os.getenv("PDD_MIN_IMAGE_EDGE", "480"))
PDD_DETAIL_PREFERRED_WIDTH = int(os.getenv("PDD_DETAIL_WIDTH", "750"))
PDD_DETAIL_SOFT_MAX_HEIGHT = int(os.getenv("PDD_DETAIL_SOFT_MAX_HEIGHT", "1500"))
CACHE_VERSION = "image-cache-v1"
DEFAULT_ROWS = 2
DEFAULT_COLS = 3

# Detail-page modules are intentionally explicit and user-editable.  Counts
# describe how many panels should be allocated to a role; they are not claims
# about the product.  ``size_chart`` stays disabled by default because an AI
# must never invent garment measurements.
DETAIL_MODULE_CATALOG = {
    "hook": "首屏卖点",
    "pain_point": "痛点场景",
    "mechanism": "结构/原理",
    "benefit": "核心卖点",
    "usage": "使用步骤",
    "scene": "生活场景",
    "comparison": "对比说明",
    "proof": "材质/细节证据",
    "packaging": "包装/随附物",
    "trust_faq": "信任/FAQ",
    "closing_cta": "收尾 CTA",
    "size_chart": "尺码表（需真实数据）",
    "sku_preview": "规格预览",
    "carousel_sell": "轮播卖点",
    "angle": "角度展示",
}
_DETAIL_MODULE_ALIASES = {
    "carousel": "carousel_sell",
    "scene_coverage": "scene", "lifestyle": "scene", "usage_steps": "usage",
    "steps": "usage", "carousel": "carousel_sell", "hero": "carousel_sell",
    "angle_view": "angle", "model_showcase": "scene",
    "feature": "benefit", "feature_macro": "proof", "material": "proof",
    "faq": "trust_faq", "faq_cta": "closing_cta", "cta": "closing_cta",
    "size-spec": "size_chart", "size": "size_chart", "sku": "sku_preview",
}
# Keep the shared layout rules in one place, but do not force every style onto
# the same off-white background.  The old lock said "fixed off-white" and then
# appended a luxury/tech direction, which made style changes easy for an image
# model to ignore.  Each profile below is an explicit visual grammar; it is
# still shared by every panel in a campaign.
_STYLE_PROFILES = {
    "minimal": (
        "style variant MINIMAL: clean white #FFFFFF and soft ivory #F7F5F0 backgrounds, "
        "deep charcoal #2D2D2D text, restrained gray accent #7A8490, neutral-cool 5200K "
        "diffused studio light, soft contact shadow, no decorative props"
    ),
    "fresh": (
        "style variant FRESH: fresh natural daylight, pale mint #EAF7F0 (pale green) and warm white #FFFFFF backgrounds, "
        "deep green-gray #29443A text, sage accent #8FC7A5, bright 5600K natural daylight "
        "with soft directional shadows, airy seasonal feel and only one subtle natural texture "
        "in a lifestyle panel"
    ),
    "luxury": (
        "style variant LUXURY: luxury editorial lighting, deep charcoal #1E1E1E and warm ivory #F5F1E8 backgrounds, "
        "ivory #F5F1E8 text, restrained champagne gold accent #D4AF37, 4300K Rembrandt key "
        "light with a controlled rim highlight, premium editorial contrast and no cheap glossy clutter"
    ),
    "tech": (
        "style variant TECH: technology ecommerce photography, deep navy #0B1220 and graphite #182333 backgrounds, "
        "cool white #F8FAFC text, electric blue #4778FF accent, cool 6000K crisp edge light, "
        "precise geometric layout and controlled reflections"
    ),
}
_STYLE_ALIASES = {
    "极简棚拍": "minimal", "极简": "minimal",
    "清新自然": "fresh", "清新": "fresh",
    "高级质感": "luxury", "奢华": "luxury",
    "科技产品": "tech", "科技": "tech",
}
DEFAULT_STYLE_LOCK = (
    "Campaign Style Lock: consistent premium ecommerce visual system across the entire image set; "
    "the selected style variant below is mandatory for every panel; modern geometric sans-serif "
    "headline typography; consistent restrained label and icon language, with rounded pills, editorial captions, "
    "callout lines or table headers chosen per panel; consistent thin-line icon style; stable product scale and placement; generous whitespace; no color palette changes, "
    "no mixed fonts, no random backgrounds, no inconsistent lighting, no mismatched icon styles."
)

_BUILTIN_TEMPLATE_HINTS = {
    "hero-image": "clean product photography, centered hero crop, pure or softly tinted background",
    "lifestyle-scene": "realistic use context, product remains clearly identifiable and proportionate",
    "model-showcase": "fashion/editorial model showcase, natural expression, realistic fit and skin texture",
    "flat-lay": "top-down flat lay, clean arrangement, no unverified accessories",
    "detail-macro": "macro material and craftsmanship close-up, visible texture without invented parts",
    "poster-banner": "ecommerce poster/banner composition with one short headline and generous text-safe space",
    "infographic": "mobile-friendly feature layout with short labels and only evidence-supported facts",
    "size-spec": "dimensions or size guide only when visible or supplied by the user",
    "packaging": "packaging or included-items view only when present in the reference photos",
}

# The reference skill describes a full PDP sequence rather than a set of
# near-identical product shots.  Keep this sequence deterministic so a single
# contact-sheet request still gives the image model a useful editorial brief.
# The first five cells are hero candidates; later cells are detail/infographic
# candidates.  These strings are deliberately short because they are composed
# into one API prompt and must remain readable to both Chinese and English
# capable image models.
_PANEL_BLUEPRINTS = (
    "HERO 1: clean front three-quarter product hero, full product visible, 35-40% scale, neutral studio background",
    "HERO 2: clean 90-degree side profile, show depth and silhouette, same product scale and lighting",
    "HERO 3: elevated 45-degree view or direct overhead view, reveal top/layout and included parts only if visible",
    "HERO 4: realistic use context with one plausible environment, product remains dominant and fully identifiable",
    "HERO 5: close-up craftsmanship or material detail, sharp texture, no invented seams, buttons, ports, or ingredients",
    "DETAIL 1: E-commerce infographic benefits screen, two-column layout, product angle plus 3-4 short evidence-based callouts",
    "DETAIL 2: E-commerce infographic mechanism or feature screen, structured grid with thin-line icons and numbered callouts",
    "DETAIL 3: E-commerce infographic usage steps, 3-4 numbered steps and a final product view, no medical or performance claims",
    "DETAIL 4: E-commerce infographic scene coverage, 2-3 small context cards using consistent style and realistic scale",
    "DETAIL 5: E-commerce infographic material/quality screen, macro crop with restrained labels tied to visible details",
    "DETAIL 6: E-commerce infographic comparison screen, neutral comparison of observable differences only, no superiority claims",
    "DETAIL 7: E-commerce infographic packaging or included-items screen, show only visible items and no size or quantity claims",
    "DETAIL 8: E-commerce infographic trust/FAQ screen, use proof placeholders when evidence is unavailable, no fake badges",
    "DETAIL 9: E-commerce infographic closing screen, product hero plus short neutral value summary and CTA placeholder",
)

_CATEGORY_HERO_BLUEPRINTS = {
    "fashion": (
        "HERO 1: clean front three-quarter garment hero, full silhouette visible, copy-safe whitespace",
        "HERO 2: 90-degree side profile, show shoulder, sleeve, hem and drape without inventing closures",
        "HERO 3: macro knit/fabric/print detail, preserve real texture and artwork as visible evidence",
        "HERO 4: realistic wardrobe lifestyle scene such as commute, cafe or weekend walk, product remains dominant",
        "HERO 5: styled flat-lay or ghost-mannequin arrangement using only visible garment pieces and restrained props",
        "HERO 6: three-panel editorial usage rhythm covering indoor, cafe and outdoor context with the same colorway",
    ),
    "home": (
        "HERO 1: clean three-quarter product hero in a calm room, full silhouette and scale visible",
        "HERO 2: side/height/depth view on a simple surface, preserve every visible edge and opening",
        "HERO 3: material/finish macro with real grain, weave, glaze or texture only",
        "HERO 4: realistic living-room, desk or entryway use scene chosen from the product's visible function",
        "HERO 5: tidy flat-lay of visible included pieces, no invented accessories or packaging",
        "HERO 6: three-context editorial strip showing daily use, storage and close-up detail without claims",
    ),
    "electronics": (
        "HERO 1: clean product hero on a precise desk, full device and proportions visible",
        "HERO 2: side/back/port view, show only visible ports, buttons and openings",
        "HERO 3: screen/material/finish macro, preserve actual controls and artwork",
        "HERO 4: realistic desk, commute or work-from-home use scene with one generic adult only if enabled",
        "HERO 5: restrained flat-lay of visible cable/adapter/accessories only",
        "HERO 6: three use moments such as desk, travel and nightstand, no invented compatibility claims",
    ),
    "beauty": (
        "HERO 1: clean packshot with bottle/tube/jar fully visible and label area preserved",
        "HERO 2: cap/nozzle/texture side view, show only visible construction",
        "HERO 3: macro of real product texture and finish, never invent ingredients or test results",
        "HERO 4: realistic vanity, bathroom or travel pouch routine scene, no medical or efficacy claim",
        "HERO 5: restrained flat-lay of visible packaging and tools only",
        "HERO 6: three routine moments with short neutral labels, avoiding before/after promises",
    ),
    "food": (
        "HERO 1: clean packshot with package front and actual artwork preserved",
        "HERO 2: side/back packaging view, do not invent nutrition, ingredients or certification text",
        "HERO 3: macro of visible texture, serving surface or seal detail",
        "HERO 4: realistic breakfast, desk snack or picnic context selected from the product type",
        "HERO 5: restrained serving flat-lay using only plausible, non-claiming props",
        "HERO 6: three serving moments with short neutral labels, no health or superiority claims",
    ),
}

_CATEGORY_DETAIL_BLUEPRINTS = {
    "fashion": (
        "DETAIL 1: benefit card for texture/fit only when visible, with 2-3 short labels",
        "DETAIL 2: front/side construction card, mark unseen structure as 待补充",
        "DETAIL 3: commute/cafe/weekend styling triptych with different scenes",
        "DETAIL 4: material and craftsmanship macro card",
        "DETAIL 5: collar/cuff/hem detail card using visible evidence",
        "DETAIL 6: lifestyle scene variation with a different time of day and natural product interaction",
        "DETAIL 7: packaging or visible included-items composition, only when supported by the references",
        "DETAIL 8: outfit pairing card with restrained visible accessories",
        "DETAIL 9: closing value card with one neutral headline and product hero",
    ),
    "home": (
        "DETAIL 1: observable material/finish benefit card",
        "DETAIL 2: scale-in-space composition using natural visual context, without numeric measurements",
        "DETAIL 3: living-room/desk/entryway usage triptych",
        "DETAIL 4: mechanism or storage detail card only when visible",
        "DETAIL 5: included-items card based on visible packaging",
        "DETAIL 6: maintenance-free lifestyle scene or material close-up, without care instructions",
        "DETAIL 7: setup/usage steps without unsupported performance claims",
        "DETAIL 8: space styling card with realistic scale",
        "DETAIL 9: neutral closing card with product hero",
    ),
    "electronics": (
        "DETAIL 1: visible feature card with 2-3 short labels",
        "DETAIL 2: ports/buttons/controls map only for visible parts",
        "DETAIL 3: desk/commute/work-from-home usage triptych",
        "DETAIL 4: setup or usage steps with no invented compatibility",
        "DETAIL 5: finish/material macro card",
        "DETAIL 6: included cable/accessory card only from references",
        "DETAIL 7: desk placement or workflow scene with visual context, without numeric specifications",
        "DETAIL 8: scale and desk-placement card",
        "DETAIL 9: neutral closing card with product hero",
    ),
    "beauty": (
        "DETAIL 1: visible texture/finish card without efficacy claims",
        "DETAIL 2: packaging/nozzle/usage area detail card",
        "DETAIL 3: vanity/bathroom/travel routine triptych",
        "DETAIL 4: application steps using neutral language",
        "DETAIL 5: product texture macro card",
        "DETAIL 6: packaging and routine scene with visible product evidence, without ingredient or certification claims",
        "DETAIL 7: storage or travel routine scene, without size or volume claims",
        "DETAIL 8: storage and routine card",
        "DETAIL 9: neutral closing card with product hero",
    ),
    "food": (
        "DETAIL 1: visible texture/taste-neutral product card",
        "DETAIL 2: package front/back evidence card",
        "DETAIL 3: breakfast/desk/picnic serving triptych",
        "DETAIL 4: serving steps without nutrition or health claims",
        "DETAIL 5: macro texture/seal card",
        "DETAIL 6: ingredients/nutrition placeholder unless label is legible",
        "DETAIL 7: packaging and serving context, without size or quantity claims",
        "DETAIL 8: storage/serving lifestyle card without care or nutrition claims",
        "DETAIL 9: neutral closing card with product hero",
    ),
}


def _category_blueprints(product_hint: str, kind: str) -> tuple[str, ...]:
    category = _guess_skill_category(product_hint)
    if kind == "hero":
        return _CATEGORY_HERO_BLUEPRINTS.get(category, _PANEL_BLUEPRINTS[:6])
    return _CATEGORY_DETAIL_BLUEPRINTS.get(category, _PANEL_BLUEPRINTS[5:])


def _guess_skill_category(product_hint: str) -> str:
    text = (product_hint or "").lower()
    if any(word in text for word in ("衣服", "服装", "针织", "衬衫", "卫衣", "外套", "裙", "裤", "鞋")):
        return "fashion"
    if any(word in text for word in ("手机", "电脑", "耳机", "键盘", "电子", "充电")):
        return "electronics"
    if any(word in text for word in ("面霜", "口红", "护肤", "化妆", "洗发", "香水")):
        return "beauty"
    if any(word in text for word in ("食品", "零食", "咖啡", "茶", "饼干", "果干")):
        return "food"
    if any(word in text for word in ("沙发", "收纳", "杯", "家居", "家具", "灯")):
        return "home"
    return ""


def _skill_template_root() -> tuple[Path, str]:
    """Use the checked-in GitHub clone first, with the installed Skill as fallback.

    The project-local clone is the deployable source of truth for the rented
    admin service.  The user-directory installation remains useful for a
    development checkout created without ``vendor/``.
    """
    bundled = Path(__file__).resolve().parent / "vendor" / "ecom-details-image" / "references" / "templates"
    if bundled.exists():
        return bundled, "project vendor skill: ecom-details-image (GitHub clone)"
    installed = Path.home() / ".codex" / "skills" / "ecom-details-image"
    installed_templates = installed / "references" / "templates"
    if installed_templates.exists():
        return installed_templates, "installed Codex skill: ecom-details-image"
    return bundled, "bundled vendor skill: ecom-details-image"


def _template_hints(product_hint: str, count: int, style: str = "minimal", model_scene: bool = False, ad_copy: str = "") -> tuple[list[str], list[str]]:
    """读取 GitHub Skill 模板的结构、品类提示、风格变体和 anti-AI 规则。"""
    lowered = (product_hint or "").lower()
    category = _guess_skill_category(product_hint)
    ids = ["hero-image", "detail-macro", "lifestyle-scene", "flat-lay", "infographic", "packaging", "size-spec"]
    # Pull in the Skill's more specific scene systems instead of repeating the
    # same generic lifestyle cue for every product category.
    category_templates = {
        "fashion": ["ghost-mannequin", "try-on-virtual", "seasonal-campaign", "magazine-editorial"],
        "electronics": ["multi-angle-grid", "device-mockup", "livestream", "ugc-style"],
        "beauty": ["ugc-style", "seasonal-campaign", "packaging", "social-media"],
        "food": ["seasonal-campaign", "social-media", "packaging", "ugc-style"],
        "home": ["storefront", "ugc-style", "packaging", "magazine-editorial"],
    }
    category_ids = [template_id for template_id in category_templates.get(category, []) if template_id not in ids]
    # Put category-specific systems near the front so the compact six-panel
    # request still receives at least two genuinely different scene templates.
    ids = ids[:2] + category_ids + ids[2:]
    if model_scene:
        if "model-showcase" not in ids:
            ids.insert(2, "model-showcase")
        if "try-on-virtual" not in ids and category == "fashion":
            ids.insert(3, "try-on-virtual")
    # A supplied ad headline is a real poster/banner brief. Include the
    # template explicitly so the prompt receives its layout and typography
    # guidance instead of treating copy as an incidental sentence.
    if ad_copy.strip() and "poster-banner" not in ids:
        ids.insert(1, "poster-banner")
    if any(word in lowered for word in ("包装", "礼盒", "套装")):
        ids = ["hero-image", "packaging", "detail-macro", "lifestyle-scene", "infographic", "size-spec"]
        if model_scene:
            ids.insert(3, "model-showcase")
    # Keep the request compact while ensuring a 3x3 detail plan can receive
    # nine distinct template cues from the installed Skill.
    ids = ids[:max(4, min(count, len(ids)))]
    variant_key = _STYLE_ALIASES.get((style or "minimal").strip().lower(), (style or "minimal").strip().lower())
    variant_alias = {"minimal": "feature-grid", "fresh": "morning", "luxury": "luxury", "tech": "tech"}.get(variant_key, variant_key)
    root, _skill_source = _skill_template_root()
    from skill_templates import compile_hint
    hints = [f"{template_id}: " + compile_hint(root, template_id, product=product_hint or "the photographed product", category=category, style=variant_key, model_scene=model_scene, ad_copy=ad_copy) for template_id in ids]
    return ids, hints


def skill_template_version() -> dict[str, Any]:
    from skill_templates import version
    return version(_skill_template_root()[0])


class WorkflowError(RuntimeError):
    """用户可理解的流水线错误。"""
    def __init__(self, message: str, code: str = "workflow_error"):
        super().__init__(message)
        self.code = code


def _normalize_api_endpoint(url: str, suffix: str) -> str:
    """允许用户填服务商 base URL，也允许直接填完整 endpoint。"""
    url = (url or '').strip().rstrip('/')
    if not url:
        return ''
    if url.endswith(suffix) or url.endswith('/images/generations') or url.endswith('/images/edits') or url.endswith('/chat/completions'):
        return url
    if url.endswith('/v1'):
        return url + suffix
    # 常见的 https://api.example.com 形式，自动补 OpenAI-compatible 路径。
    if url.count('/') == 2:
        return url + '/v1' + suffix
    return url


def _validate_task_api_url(url: str) -> None:
    """Reject obvious SSRF targets in tenant-supplied API URLs.

    Server-side environment URLs are trusted deployment configuration.  This
    guard is only applied to per-task form values, which are controlled by a
    renter and must not be allowed to call loopback, link-local, private or
    metadata addresses.  Local demo mode can opt into loopback for its own
    in-process adapter.
    """
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise WorkflowError("API 地址必须是 http 或 https URL")
    if parsed.username or parsed.password:
        raise WorkflowError("API 地址不能把密钥写在 URL 中，请单独填写 API Key")
    host = parsed.hostname.strip().lower().rstrip(".")
    if (
        os.getenv("PDD_AI_DEMO", "").strip().lower() in {"1", "true", "yes"}
        or os.getenv("PDD_ALLOW_PRIVATE_API_URL", "").strip().lower() in {"1", "true", "yes"}
    ):
        return
    blocked_names = {"localhost", "localhost.localdomain", "metadata", "metadata.google.internal"}
    if host in blocked_names or host.endswith(".localhost") or host.endswith(".local"):
        raise WorkflowError("为防止内网探测，本次任务不允许使用本机或局域网 API 地址")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and (
        address.is_private or address.is_loopback or address.is_link_local
        or address.is_reserved or address.is_multicast or address.is_unspecified
    ):
        raise WorkflowError("为防止内网探测，本次任务不允许使用本机或局域网 API 地址")


@dataclass
class WorkflowConfig:
    project_dir: Path
    provider_mode: str = "mock"
    image_api_url: str = ""
    image_api_key: str = ""
    prompt_api_url: str = ""
    prompt_api_key: str = ""
    image_model: str = "gpt-image-2.5-flare"
    prompt_model: str = "gpt-5.1-mini"
    image_reference_mode: str = ""
    timeout_seconds: int = 180
    # Set per task by run_workflow for the fixed local Skill-sample adapter;
    # this avoids treating a tenant-supplied real API override as a demo.
    demo_asset_mode: bool = False

    @classmethod
    def from_env(cls, project_dir: Path) -> "WorkflowConfig":
        mode = os.getenv("PDD_AI_PROVIDER", "mock").strip().lower()
        image_url = _normalize_api_endpoint(os.getenv("PDD_IMAGE_API_URL", ""), "/images/generations")
        image_key = os.getenv("PDD_IMAGE_API_KEY", "").strip()
        prompt_url = _normalize_api_endpoint(os.getenv("PDD_PROMPT_API_URL", ""), "/chat/completions")
        prompt_key = os.getenv("PDD_PROMPT_API_KEY", "").strip()
        # 只有同时有地址和密钥才切到真实通道，避免半配置导致付费请求报错。
        if mode == "auto":
            mode = "openai" if image_url and image_key else "mock"
        return cls(
            project_dir=project_dir,
            provider_mode=mode,
            image_api_url=image_url,
            image_api_key=image_key,
            prompt_api_url=prompt_url,
            prompt_api_key=prompt_key,
            image_model=os.getenv("PDD_IMAGE_MODEL", "gpt-image-2.5-flare"),
            prompt_model=os.getenv("PDD_PROMPT_MODEL", "gpt-5.1-mini"),
            image_reference_mode=os.getenv("PDD_IMAGE_REFERENCE_MODE", "auto").strip().lower(),
            timeout_seconds=max(30, min(int(os.getenv("PDD_AI_TIMEOUT", "180")), 900)),
        )


def _safe_slug(value: str) -> str:
    value = re.sub(r"[^\w\-\u4e00-\u9fff]+", "-", value or "商品")
    return value.strip("-_")[:48] or "商品"


def _cleanup_old_jobs(project_dir: Path) -> None:
    """清理过期中间产物，避免出租版被用户批量任务撑满磁盘。"""
    root = project_dir / "data" / "ai_jobs"
    if not root.exists():
        return
    try:
        retention = max(1, int(os.getenv("PDD_AI_RETENTION_HOURS", "72"))) * 3600
    except ValueError:
        retention = 72 * 3600
    cutoff = time.time() - retention
    for child in root.iterdir():
        try:
            if child.is_dir() and child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
        except OSError:
            continue


def _image_cache_enabled() -> bool:
    """Whether identical image requests may reuse a locally cached sheet.

    Reuse is enabled by default because the admin UI can be retried after a
    transient browser error.  Set ``PDD_AI_IMAGE_CACHE=0`` to force a fresh
    provider request.  The cache key never contains the raw API key or image
    bytes, only their SHA-256 digests.
    """
    return os.getenv("PDD_AI_IMAGE_CACHE", "1").strip().lower() not in {"0", "false", "no", "off"}


def _image_cache_key(
    config: WorkflowConfig,
    prompt: str,
    input_image: Image.Image,
    rows: int,
    cols: int,
    reference_images: list[Image.Image] | None,
    reference_mode: str,
    endpoint: str,
) -> str:
    """Build a content-addressed key for one billable image request.

    Provider credentials are represented by a digest so an operator can use
    the same project directory without leaking a key into filenames or JSON.
    Output settings and all reference photos are included so a 1:1 retry can
    reuse safely while a changed photo, model, style or resolution cannot.
    """
    digest = hashlib.sha256()
    digest.update(CACHE_VERSION.encode("utf-8"))
    fields = {
        "endpoint": endpoint,
        "model": config.image_model,
        "provider": config.provider_mode,
        "reference_mode": reference_mode,
        "rows": rows,
        "cols": cols,
        "prompt": prompt,
        "size": getattr(config, "request_output_size", "") or os.getenv("PDD_IMAGE_SIZE", ""),
        "quality": os.getenv("PDD_IMAGE_QUALITY", "medium"),
        "output_format": os.getenv("PDD_IMAGE_OUTPUT_FORMAT", "jpeg"),
        "output_compression": os.getenv("PDD_IMAGE_OUTPUT_COMPRESSION", "90"),
        # A hosted/rental deployment should set this to a per-tenant opaque
        # id.  The local project path remains the safe default for the admin
        # app, while tenants sharing one backend directory stay isolated.
        "cache_namespace": os.getenv("PDD_AI_CACHE_NAMESPACE", str(config.project_dir)),
        # Same API endpoint with a different account must not share outputs.
        "key_digest": hashlib.sha256((config.image_api_key or "").encode("utf-8")).hexdigest(),
    }
    digest.update(json.dumps(fields, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    refs = reference_images or [input_image]
    for ref in refs[:12]:
        buf = io.BytesIO()
        ref.save(buf, "PNG")
        digest.update(hashlib.sha256(buf.getvalue()).digest())
    return digest.hexdigest()


def _image_cache_paths(config: WorkflowConfig, key: str) -> tuple[Path, Path]:
    root = config.project_dir / "data" / "ai_image_cache"
    return root / f"{key}.jpg", root / f"{key}.json"


def _json_from_text(text: str) -> dict[str, Any] | None:
    """解析模型可能包在 markdown fence 里的 JSON。"""
    if not text:
        return None
    candidates = [text.strip()]
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S | re.I)
    if match:
        candidates.insert(0, match.group(1))
    for item in candidates:
        try:
            value = json.loads(item)
            if isinstance(value, dict):
                return value
        except (ValueError, TypeError):
            continue
    return None


_APPAREL_HINTS = (
    "衣服", "服装", "上衣", "衬衫", "t恤", "t-shirt", "卫衣", "毛衣", "针织",
    "外套", "夹克", "羽绒", "大衣", "风衣", "连衣裙", "裙子", "半身裙", "短裤",
    "长裤", "牛仔裤", "裤子", "童装", "内衣", "睡衣", "开衫", "西装",
)
_SHOE_HINTS = ("鞋", "运动鞋", "靴", "凉鞋", "拖鞋", "sneaker", "boots")


def _clean_spec_map(raw: Any, max_names: int = 12, max_values: int = 24) -> dict[str, list[str]]:
    """清洗已识别规格或规格建议，统一成前端/SKU 生成器消费的字典。"""
    if not isinstance(raw, dict):
        return {}
    result: dict[str, list[str]] = {}
    for raw_name, raw_values in list(raw.items())[:max_names]:
        name = str(raw_name or "").strip()[:20]
        if not name:
            continue
        if isinstance(raw_values, dict):
            raw_values = raw_values.get("values") or raw_values.get("options") or []
        if not isinstance(raw_values, (list, tuple)):
            raw_values = [raw_values] if raw_values else []
        values: list[str] = []
        for raw_value in list(raw_values)[:max_values]:
            value = str(raw_value or "").strip()[:30]
            if value and value not in values:
                values.append(value)
        if values:
            result[name] = values
    return result


def _clean_text_items(raw: Any, max_items: int = 8, max_len: int = 120) -> list[str]:
    """清洗模型返回的短句列表，避免把大段幻觉文案带进上架资料。"""
    if isinstance(raw, str):
        values = re.split(r"[\r\n；;]+", raw)
    elif isinstance(raw, (list, tuple)):
        values = list(raw)
    else:
        values = []
    result: list[str] = []
    for value in values[:max_items]:
        text = re.sub(r"\s+", " ", str(value or "")).strip()[:max_len]
        if text and text not in result:
            result.append(text)
    return result


# These roles mirror the Skill's PDP sequence: a detail set should move from
# the first visual promise through proof and usage to the final action.  Keeping
# the role vocabulary bounded makes a model's creative output editable and
# prevents a long, unstructured paragraph from becoming image text.
_CREATIVE_ROLE_META = {
    "hook": "首屏承接",
    "pain_point": "痛点场景",
    "mechanism": "机制说明",
    "benefit": "核心利益",
    "usage": "使用步骤",
    "scene": "场景覆盖",
    "comparison": "对比选择",
    "proof": "信任证据",
    "faq_cta": "FAQ与行动",
}
_CREATIVE_ROLE_ALIASES = {
    "first_screen": "hook", "hero": "hook", "opening": "hook", "首屏": "hook",
    "pain": "pain_point", "problem": "pain_point", "痛点": "pain_point",
    "feature": "mechanism", "how_it_works": "mechanism", "机制": "mechanism",
    "key_benefit": "benefit", "卖点": "benefit", "利益": "benefit",
    "steps": "usage", "how_to": "usage", "步骤": "usage",
    "use_case": "scene", "lifestyle": "scene", "场景": "scene",
    "compare": "comparison", "对比": "comparison",
    "trust": "proof", "evidence": "proof", "证据": "proof",
    "faq": "faq_cta", "cta": "faq_cta", "收尾": "faq_cta",
}


def _clean_creative_blocks(raw: Any, max_items: int = 9) -> list[dict[str, Any]]:
    """Normalize structured PDP copy/scene modules from a prompt model."""
    if isinstance(raw, dict):
        nested = raw.get("blocks") or raw.get("items") or raw.get("modules")
        if nested is not None:
            raw = nested
        else:
            # Accept a role-keyed object as well as the documented list form:
            # {"hook": {...}, "benefit": {...}}.
            raw = [dict(value, role=key) for key, value in raw.items() if isinstance(value, dict)]
    if not isinstance(raw, (list, tuple)):
        return []
    result: list[dict[str, Any]] = []
    for index, item in enumerate(list(raw)[:max_items]):
        if not isinstance(item, dict):
            continue
        role_raw = str(item.get("role") or item.get("module") or item.get("type") or "").strip().lower()
        role = _CREATIVE_ROLE_ALIASES.get(role_raw, role_raw)
        if role not in _CREATIVE_ROLE_META:
            role = list(_CREATIVE_ROLE_META)[index % len(_CREATIVE_ROLE_META)]
        headline = re.sub(r"\s+", " ", str(item.get("headline") or item.get("title") or item.get("copy") or "").strip())[:20]
        subheadline = re.sub(r"\s+", " ", str(item.get("subheadline") or item.get("subtitle") or item.get("description") or "").strip())[:48]
        labels = _clean_text_items(item.get("labels") or item.get("proof_points") or item.get("tags"), max_items=4, max_len=24)
        scene = re.sub(r"\s+", " ", str(item.get("scene") or item.get("setting") or item.get("usage_scene") or "").strip())[:80]
        cta = re.sub(r"\s+", " ", str(item.get("cta") or item.get("action") or "").strip())[:8]
        evidence = re.sub(r"\s+", " ", str(item.get("evidence") or item.get("support") or "").strip())[:80]
        if not any((headline, subheadline, labels, scene, cta, evidence)):
            continue
        result.append({
            "role": role,
            "role_label": _CREATIVE_ROLE_META[role],
            "headline": headline,
            "subheadline": subheadline,
            "labels": labels,
            "scene": scene,
            "cta": cta,
            "evidence": evidence,
        })
    return result


def _clean_scene_plan(raw: Any, max_items: int = 9) -> list[dict[str, Any]]:
    """Normalize an AI-generated shot plan into bounded prompt ingredients."""
    if isinstance(raw, dict):
        raw = raw.get("scenes") or raw.get("items") or raw.get("plan") or [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    result: list[dict[str, Any]] = []
    for index, item in enumerate(list(raw)[:max_items]):
        if isinstance(item, str):
            item = {"setting": item}
        if not isinstance(item, dict):
            continue
        role = re.sub(r"\s+", " ", str(item.get("role") or item.get("purpose") or f"scene_{index + 1}").strip())[:24]
        setting = re.sub(r"\s+", " ", str(item.get("setting") or item.get("scene") or item.get("environment") or "").strip())[:80]
        time_of_day = re.sub(r"\s+", " ", str(item.get("time_of_day") or item.get("time") or "").strip())[:32]
        camera = re.sub(r"\s+", " ", str(item.get("camera") or item.get("framing") or "").strip())[:48]
        lighting = re.sub(r"\s+", " ", str(item.get("lighting") or "").strip())[:48]
        mood = re.sub(r"\s+", " ", str(item.get("mood") or "").strip())[:40]
        action = re.sub(r"\s+", " ", str(item.get("action") or item.get("interaction") or "").strip())[:64]
        copy_direction = re.sub(r"\s+", " ", str(item.get("copy_direction") or item.get("copy") or "").strip())[:48]
        if not any((setting, time_of_day, camera, lighting, mood, action, copy_direction)):
            continue
        result.append({
            "role": role,
            "setting": setting,
            "time_of_day": time_of_day,
            "camera": camera,
            "lighting": lighting,
            "mood": mood,
            "action": action,
            "copy_direction": copy_direction,
        })
    return result


def _clean_detail_modules(raw: Any, max_items: int = 13) -> list[dict[str, Any]]:
    """Normalize an editable detail-module plan.

    The admin UI may submit a list of ``{"type": ..., "count": ...}``
    objects, a role-keyed mapping, or a simple list of strings.  Unknown
    modules are dropped and counts are bounded so a malformed request cannot
    create an unbounded number of paid image panels.
    """
    if isinstance(raw, dict):
        nested = raw.get("modules") or raw.get("items") or raw.get("plan")
        if nested is not None:
            raw = nested
        else:
            raw = [{"type": key, "count": value} for key, value in raw.items()]
    if isinstance(raw, str):
        raw = [part.strip() for part in raw.split(",") if part.strip()]
    if not isinstance(raw, (list, tuple)):
        return []
    result: list[dict[str, Any]] = []
    total = 0
    for item in list(raw)[:max_items]:
        if isinstance(item, str):
            item = {"type": item, "count": 1}
        if not isinstance(item, dict):
            continue
        raw_type = str(item.get("type") or item.get("module") or item.get("role") or item.get("name") or "").strip().lower()
        module_type = _DETAIL_MODULE_ALIASES.get(raw_type, raw_type)
        if module_type not in DETAIL_MODULE_CATALOG:
            continue
        try:
            count = int(item.get("count", item.get("quantity", 1)) or 0)
        except (TypeError, ValueError):
            count = 1
        count = max(0, min(count, 20))
        # A requested zero count is a useful way for the operator to disable
        # a suggested module, so retain it in the returned plan.
        if total + count > 20:
            count = max(0, 20 - total)
        result.append({
            "type": module_type,
            "label": DETAIL_MODULE_CATALOG[module_type],
            "count": count,
            "enabled": bool(item.get("enabled", count > 0)),
            "note": re.sub(r"\s+", " ", str(item.get("note") or item.get('reason') or "").strip())[:120],
        })
        total += count
        if total >= 20:
            break
    return result


def _default_detail_modules(product_hint: str = "", size_chart_enabled: bool = False) -> list[dict[str, Any]]:
    """Return the conservative Skill-inspired PDP sequence.

    The default deliberately omits size charts and SKU preview panels: those
    require merchant-provided measurements or variant evidence.  The caller
    can opt in to either module explicitly.
    """
    # Nine panels is the default 3x3 detail pack.  Keep the default plan and
    # the default grid in lock-step; an explicit UI plan may request more and
    # will be split over additional rounds below.
    category = _guess_skill_category(product_hint)
    modules = {
        "fashion": [("hook", 1), ("angle", 1), ("scene", 3), ("proof", 2), ("benefit", 1), ("closing_cta", 1)],
        "electronics": [("hook", 1), ("mechanism", 2), ("benefit", 1), ("usage", 1), ("scene", 1), ("proof", 1), ("trust_faq", 1), ("closing_cta", 1)],
        "beauty": [("hook", 1), ("proof", 2), ("benefit", 1), ("usage", 1), ("scene", 2), ("trust_faq", 1), ("closing_cta", 1)],
        "food": [("hook", 1), ("proof", 2), ("scene", 3), ("benefit", 1), ("trust_faq", 1), ("closing_cta", 1)],
        "home": [("hook", 1), ("angle", 1), ("scene", 2), ("benefit", 2), ("usage", 1), ("proof", 1), ("closing_cta", 1)],
    }.get(category, [("hook", 1), ("pain_point", 1), ("mechanism", 1), ("benefit", 1), ("usage", 1), ("scene", 1), ("proof", 1), ("packaging", 1), ("closing_cta", 1)])
    if size_chart_enabled:
        modules.insert(-1, ("size_chart", 1))
    return [{"type": kind, "label": DETAIL_MODULE_CATALOG[kind], "count": count, "enabled": True, "note": ""} for kind, count in modules]


def _clean_size_chart_specs(raw: Any, selected_sizes: Any = None) -> list[dict[str, Any]]:
    """Normalize merchant-entered size rows without inventing measurements.

    Values are kept as strings because size charts often contain units and
    ranges.  Rows without at least a size label are discarded; empty
    measurements remain empty and are marked ``needs_measurement``.
    """
    if isinstance(raw, dict):
        raw = raw.get("rows") or raw.get("items") or [raw]
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raw = []
    if not isinstance(raw, (list, tuple)):
        raw = []
    selected = {str(v).strip() for v in (selected_sizes or []) if str(v).strip()} if isinstance(selected_sizes, (list, tuple, set)) else set()
    rows: list[dict[str, Any]] = []
    if len(raw) > 100:
        raise WorkflowError("尺码表超过100行，请拆成独立商品资料，避免数据截断")
    for item in raw:
        if isinstance(item, str):
            item = {"size": item}
        if not isinstance(item, dict):
            continue
        size = re.sub(r"\s+", " ", str(item.get("size") or item.get("name") or item.get("label") or "").strip())
        if len(size) > 40:
            raise WorkflowError("尺码名称超过40字，请缩短后保存")
        if not size or (selected and size not in selected):
            continue
        values = {}
        source_values = item.get("measurements") if isinstance(item.get("measurements"), dict) else item
        for raw_key, raw_value in source_values.items():
            key = str(raw_key).strip()
            if key in {"size", "name", "label", "needs_measurement", "measurements"}:
                continue
            value = re.sub(r"\s+", " ", str(raw_value if raw_value is not None else "").strip())
            if len(key) > 40 or len(value) > 120 or len(values) >= 20:
                raise WorkflowError("尺码表字段过长或超过20列，请缩短或拆分；原始数值不会自动截断")
            if key and value:
                values[key] = value
        rows.append({"size": size, "measurements": values, "needs_measurement": not bool(values)})
    return rows


def _build_detail_module_plan(raw: Any, product_hint: str = "", size_chart_enabled: bool = False) -> list[dict[str, Any]]:
    cleaned = _clean_detail_modules(raw)
    if raw is None:
        cleaned = _default_detail_modules(product_hint, size_chart_enabled=size_chart_enabled)
    if not size_chart_enabled:
        cleaned = [item for item in cleaned if item.get("type") != "size_chart"]
    # Disabled entries remain visible to the UI but are not assigned panels.
    return [item for item in cleaned if item.get("enabled", True) and int(item.get("count", 0)) > 0]


def _build_round_plan(
    strategy: str,
    generation_rounds: int,
    modules: list[dict[str, Any]],
    rows: int,
    cols: int,
    round_strategy: str = "auto",
) -> list[dict[str, Any]]:
    """Describe paid generation passes and their module ownership."""
    try:
        requested = int(generation_rounds or 0)
    except (TypeError, ValueError):
        requested = 0
    requested = max(1, min(requested or (2 if strategy == "quality" else 1), 4))
    # Quality mode defaults to two distinct visual jobs (hero carousel and
    # detail page), while an explicit one-round selection is honored as a
    # lower-cost mixed review pack by the workflow below.
    module_names = [item["type"] for item in modules for _ in range(max(0, int(item.get("count", 0))))]
    if not module_names:
        module_names = ["scene"] * max(1, rows * cols)
    # A quality run reserves round one for the clean hero carousel whenever
    # it has at least two rounds.  The remaining panels are distributed by
    # the operator's round strategy; each chunk is one billable contact-sheet
    # request and is cropped locally afterwards.
    reserve_hero = strategy == "quality" and requested >= 2
    detail_rounds = requested - 1 if reserve_hero else requested
    chunks = [[] for _ in range(max(1, detail_rounds))]
    mode = str(round_strategy or "auto").strip().lower()
    if mode == "separate":
        # Keep module types together where possible, then balance the number
        # of panels.  This gives the user a predictable higher-cost option.
        grouped: list[list[str]] = []
        for item in modules:
            group = [item["type"]] * max(0, int(item.get("count", 0) or 0))
            if group:
                grouped.append(group)
        for index, group in enumerate(grouped):
            chunks[index % len(chunks)].extend(group)
    else:
        for index, name in enumerate(module_names[:20]):
            chunks[index % len(chunks)].append(name)
    plan: list[dict[str, Any]] = []
    for index in range(requested):
        if reserve_hero and index == 0:
            role = "hero"
            assigned = ["white_pack", "carousel_sell", "angle", "lifestyle", "feature_macro", "closing_cta"]
        else:
            role = "detail"
            assigned = chunks[index - 1 if reserve_hero else index] or ["scene"]
        plan.append({
            "round": index + 1,
            "role": role,
            "modules": assigned,
            "requested_panels": len(assigned),
            "api_request": True,
            "cost_note": "每轮通常对应 1 次生图 API 请求；本地裁剪不增加请求次数。",
        })
    return plan


def _grid_for_panel_count(panel_count: int, base_rows: int, base_cols: int) -> tuple[int, int]:
    """Choose a provider grid that can contain a selected round's panels.

    The provider still receives a regular rectangular contact sheet.  We keep
    the merchant's chosen grid when it has enough cells, otherwise expand to a
    bounded 4x4 sheet and crop only the requested number of cells locally.
    This prevents a plan of seven panels from silently becoming nine billable
    output files while preserving the one-request cost advantage.
    """
    target = max(1, int(panel_count or 1))
    rows = max(1, min(int(base_rows or 1), 4))
    cols = max(1, min(int(base_cols or 1), 4))
    if target <= rows * cols:
        return rows, cols
    cols = min(4, max(cols, int((target ** 0.5) + 0.999)))
    rows = min(4, max(rows, int((target + cols - 1) // cols)))
    return rows, cols


def _flag(value: Any, default: bool = False) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"} if isinstance(value, str) else (default if value is None else bool(value))


def _selected_size_values(specs: dict[str, Any]) -> list[str]:
    return [str(value) for key, values in specs.items() if any(token in str(key).lower() for token in ("尺码", "鞋码", "尺寸", "size")) for value in values]


def _selected_variant_values(specs: dict[str, Any]) -> list[str]:
    return list(dict.fromkeys(str(value) for key, values in specs.items() if any(token in str(key).lower() for token in ("颜色", "色号", "款式", "花色", "color", "colour", "style")) for value in values))


def _clean_variant_references(raw: Any, reference_count: int = 12) -> dict[str, list[int]]:
    if not isinstance(raw, dict):
        return {}
    result: dict[str, list[int]] = {}
    for value, indices in list(raw.items())[:24]:
        if not isinstance(indices, (list, tuple)):
            indices = [indices]
        valid = []
        for index in indices:
            try:
                number = int(index)
            except (TypeError, ValueError):
                continue
            if 1 <= number <= min(12, reference_count) and number not in valid:
                valid.append(number)
        name = str(value).strip()[:40]
        if name and valid:
            result[name] = valid
    return result


def _strict_module_request(raw: Any) -> list[dict[str, Any]]:
    """Bound paid work explicitly instead of silently discarding requested images."""
    if isinstance(raw, dict):
        nested = next((raw[key] for key in ("modules", "items", "plan") if key in raw), None)
        raw = nested if nested is not None else [{"type": key, "count": value} for key, value in raw.items()]
    if not isinstance(raw, (list, tuple)):
        raise WorkflowError("图片模块计划格式不正确，请重新选择图片类型")
    if len(raw) > 30:
        raise WorkflowError("图片模块过多，一份商品包最多20张详情图")
    total = 0
    cleaned = []
    for item in raw:
        if isinstance(item, str):
            item = {"type": item, "count": 1}
        if not isinstance(item, dict):
            raise WorkflowError("图片模块格式不正确")
        if not _flag(item.get("enabled"), True):
            continue
        kind = str(item.get("type") or item.get("module") or item.get("role") or "").strip().lower()
        kind = _DETAIL_MODULE_ALIASES.get(kind, kind)
        if kind not in DETAIL_MODULE_CATALOG:
            raise WorkflowError("存在不支持的图片类型，请重新选择")
        try:
            count = int(item.get("count", item.get("quantity", 1)))
        except (TypeError, ValueError):
            raise WorkflowError("图片数量必须是0到20之间的整数")
        if count < 0 or count > 20:
            raise WorkflowError("每种图片数量应在0到20之间")
        total += count
        if count:
            cleaned.append({"type": kind, "label": DETAIL_MODULE_CATALOG[kind], "count": count, "enabled": True, "note": str(item.get("note") or item.get("reason") or "")[:500]})
    if total > 20:
        raise WorkflowError("详情图合计最多20张，请减少数量或分成另一份商品包")
    return cleaned


def _resize_module_plan(modules: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    names = [item["type"] for item in modules for _ in range(max(0, int(item.get("count", 0))))]
    names = names[:count]
    while len(names) < count:
        names.append("scene" if len(names) % 2 else "proof")
    result: list[dict[str, Any]] = []
    for name in names:
        if result and result[-1]["type"] == name:
            result[-1]["count"] += 1
        else:
            result.append({"type": name, "label": DETAIL_MODULE_CATALOG[name], "count": 1, "enabled": True, "note": ""})
    return result


def build_generation_plan(
    *, product_hint: str = "", style: str = "minimal", rows: int = 3, cols: int = 3,
    strategy: str = "grid", detail_modules: Any = None, generation_rounds: int = 0,
    round_strategy: str = "auto", auto_module_plan: bool = True, size_chart_enabled: bool = False,
    size_chart_specs: Any = None, selected_specs: Any = None, variant_references: Any = None,
    module_recommendations: Any = None, reference_count: int = 12, hero_count: int = 6,
    max_image_requests: int = 0, output_size: str = "", size_chart_only_selected: bool = True,
) -> dict[str, Any]:
    """Return the exact network-free plan used by execution, including native pixel budgets."""
    from core_planner import build_plan
    return build_plan(**locals())


def _fallback_scene_plan(metadata: dict[str, Any], product_hint: str = "", max_items: int = 9) -> list[dict[str, Any]]:
    """Build useful scene/camera directions when the prompt API omits a plan."""
    scenes = _clean_text_items(metadata.get("scene_suggestions"), max_items=max_items, max_len=70)
    blocks = _clean_creative_blocks(metadata.get("creative_blocks"), max_items=max_items)
    for block in blocks:
        scene = str(block.get("scene") or "").strip()
        if scene and scene not in scenes:
            scenes.append(scene)
    if not scenes:
        category = _guess_skill_category(product_hint)
        scenes = {
            "fashion": ["通勤室内", "咖啡店", "周末街头", "自然光居家", "旅行出行"],
            "electronics": ["整洁桌面", "通勤办公", "居家工作", "夜间床头", "收纳出行"],
            "beauty": ["晨间梳妆台", "浴室洗手台", "旅行收纳包", "自然光近景"],
            "food": ["早餐桌面", "下午茶", "办公室零食", "周末野餐"],
            "home": ["客厅一角", "卧室床头", "玄关收纳", "书桌使用"],
        }.get(category, ["自然光桌面", "日常室内", "周末户外", "近景细节"])
    cameras = ["eye-level three-quarter view", "side profile", "top-down flat lay", "macro close-up", "wide lifestyle frame", "editorial vertical crop", "overhead detail", "candid handheld frame", "clean product close-up"]
    lights = ["soft window daylight", "bright diffused daylight", "warm afternoon light", "cool clean studio light", "gentle golden-hour light"]
    moods = ["clean and calm", "fresh and practical", "warm and inviting", "focused and premium", "relaxed and authentic"]
    plan: list[dict[str, Any]] = []
    for index, setting in enumerate(scenes[:max_items]):
        block = blocks[index] if index < len(blocks) else {}
        plan.append({
            "role": str(block.get("role_label") or block.get("role") or f"scene_{index + 1}"),
            "setting": setting,
            "time_of_day": "morning" if index % 3 == 0 else "daytime" if index % 3 == 1 else "late afternoon",
            "camera": cameras[index % len(cameras)],
            "lighting": lights[index % len(lights)],
            "mood": moods[index % len(moods)],
            "action": str(block.get("scene") or "自然使用动作").strip()[:64],
            "copy_direction": str(block.get("headline") or "短标题与可见卖点").strip()[:48],
        })
    return plan


def _fallback_creative_blocks(metadata: dict[str, Any], density: str = "rich") -> list[dict[str, Any]]:
    """Build a bounded PDP map when a provider returns only flat candidates."""
    points = _clean_text_items(metadata.get("selling_points"), max_items=6, max_len=60)
    scenes = _clean_text_items(metadata.get("scene_suggestions"), max_items=6, max_len=70)
    headlines = _clean_text_items(metadata.get("ad_headlines"), max_items=5, max_len=20)
    available = max(len(points), len(scenes), len(headlines))
    limit = 9 if str(density or "rich").strip().lower() in {"rich", "丰富", "full"} else 5
    roles = list(_CREATIVE_ROLE_META)
    blocks: list[dict[str, Any]] = []
    for index in range(min(limit, available)):
        role = roles[index]
        block: dict[str, Any] = {"role": role, "role_label": _CREATIVE_ROLE_META[role], "headline": "", "subheadline": "", "labels": [], "scene": "", "cta": "", "evidence": ""}
        if index < len(headlines):
            block["headline"] = headlines[index]
        if index < len(points):
            block["labels"] = [points[index]]
        if index < len(scenes):
            block["scene"] = scenes[index]
        # A neutral closing action is a layout cue, not a product claim.
        if role == "faq_cta":
            block["cta"] = "查看详情"
        blocks.append(block)
    return blocks


def _clean_identity_items(raw: Any, max_items: int = 12, max_len: int = 160) -> list[str]:
    """清洗视觉模型给出的身份/结构证据。

    这些字段只用于提示词约束和人工复核，不能直接当成商品事实。单独
    清洗可以避免模型把一大段推理、营销文案或 JSON 嵌套带进生图请求。
    """
    return _clean_text_items(raw, max_items=max_items, max_len=max_len)


def _clean_identity_confidence(raw: Any) -> float | None:
    """将模型置信度收敛到 0..1；无法确认时返回 None。"""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value > 1:
        value /= 100.0
    return round(max(0.0, min(1.0, value)), 3)


def _reference_coverage(reference_count: int) -> dict[str, Any]:
    """给商家一个可解释的输入覆盖度提示，不把照片数量冒充视觉证明。"""
    count = max(0, int(reference_count or 0))
    if count <= 1:
        return {
            "level": "low",
            "label": "单视角，覆盖度低",
            "risk_flags": ["背面、侧面和遮挡结构无法从一张照片确认", "版型/闭合方式不要根据品类名称补全"],
        }
    if count == 2:
        return {
            "level": "partial",
            "label": "双视角，覆盖度部分",
            "risk_flags": ["仍可能缺少背面、细节或洗标证据", "两张照片不一致时以可见共同结构为准"],
        }
    return {
        "level": "multi_view",
        "label": "多视角，覆盖度较高",
        "risk_flags": ["照片数量不等于结构已证实，遮挡/反光/分辨率不足仍需人工复核"],
    }


def _identity_guard_text(metadata: dict[str, Any], reference_count: int = 1) -> str:
    """生成跨视角商品身份锁，防止品类提示词覆盖照片证据。"""
    evidence = _clean_identity_items(
        metadata.get("identity_evidence") or metadata.get("visual_evidence"),
        max_items=10,
        max_len=150,
    )
    constraints = _clean_identity_items(
        metadata.get("identity_constraints") or metadata.get("visible_constraints"),
        max_items=10,
        max_len=150,
    )
    uncertain = _clean_identity_items(
        metadata.get("uncertain_features") or metadata.get("uncertainties"),
        max_items=8,
        max_len=150,
    )
    confidence = _clean_identity_confidence(metadata.get("identity_confidence"))
    coverage = _reference_coverage(reference_count)
    lines = [
        "REFERENCE IDENTITY LOCK (highest priority):",
        f"There are {max(1, int(reference_count or 1))} independent seller reference photo(s). Match one physical product across every view; never treat the reference board, a crop, or a seller hint as a second product.",
        f"REFERENCE COVERAGE RISK ({coverage['level']}): {coverage['label']}. " + "；".join(coverage["risk_flags"]) + ".",
        "The product hint/category is only a search label. If words such as 开衫/cardigan conflict with what is visibly shown, ignore the words and preserve the photographed construction. Never add a front opening, button placket, zipper, pocket, collar, seam, sleeve shape, ingredient, port, or accessory that is not visibly supported.",
        "When a surface or construction is occluded, out of frame, too small, or ambiguous, keep it plain/neutral and mark the corresponding infographic as 待补充; do not guess to make the panel look complete.",
        "Preserve the same colorway, proportions, printed artwork, texture, closures, and logo placement across all panels. Do not use a generic category prototype as a replacement for the photographed item.",
    ]
    if evidence:
        lines.append("Photo-supported evidence (preserve exactly): " + "；".join(evidence))
    if constraints:
        lines.append("Photo-supported exclusions/constraints (must remain absent): " + "；".join(constraints))
    if uncertain:
        lines.append("Uncertain or occluded features (do not invent): " + "；".join(uncertain))
    if confidence is not None:
        lines.append(f"Visual identity confidence from the review pass: {confidence:.2f}; below 0.75, prefer conservative plain construction over a guessed subtype.")
    if not evidence and not constraints:
        lines.append("No structured visual evidence was confirmed by the metadata pass; use the photos themselves as the only authority and keep all unsupported construction neutral.")
    return " ".join(lines)


def _inject_identity_guard_prompt(prompt_pack: dict[str, Any], metadata: dict[str, Any], reference_count: int = 1) -> None:
    """将视觉身份锁追加到每个生图请求，避免 hint 诱导结构幻觉。"""
    note = _identity_guard_text(metadata, reference_count)
    if isinstance(prompt_pack.get("hero"), dict):
        prompt_pack["hero"]["prompt"] = f"{prompt_pack['hero'].get('prompt', '')}\n{note}"
    if isinstance(prompt_pack.get("detail"), dict):
        prompt_pack["detail"]["prompt"] = f"{prompt_pack['detail'].get('prompt', '')}\n{note}"
    prompt_pack["prompt"] = (
        f"主图组：{prompt_pack['hero']['prompt']}\n\n详情信息图组：{prompt_pack['detail']['prompt']}"
        if prompt_pack.get("mode") == "quality" and isinstance(prompt_pack.get("hero"), dict) and isinstance(prompt_pack.get("detail"), dict)
        else f"{prompt_pack.get('prompt', '')}\n{note}"
    )
    prompt_pack["identity_guard"] = note


def _inject_creative_brief_prompt(prompt_pack: dict[str, Any], metadata: dict[str, Any]) -> None:
    """Add reviewable copy and creative scenes without contaminating hero rules."""
    points = _clean_text_items(metadata.get("selling_points"), max_items=6, max_len=60)
    scenes = _clean_text_items(metadata.get("scene_suggestions"), max_items=6, max_len=70)
    headlines = _clean_text_items(metadata.get("ad_headlines"), max_items=5, max_len=20)
    blocks = _clean_creative_blocks(metadata.get("creative_blocks"), max_items=9)
    scene_plan = _clean_scene_plan(metadata.get("scene_plan"), max_items=9)
    if not blocks:
        blocks = _fallback_creative_blocks(metadata, "rich")
    if not (points or scenes or headlines or blocks or scene_plan):
        return

    module_lines: list[str] = []
    if blocks:
        module_lines.append("PDP COPY + SCENE MAP (assign one module to each matching detail panel; do not merge all modules into one poster):")
        for index, block in enumerate(blocks, start=1):
            parts = [f"Module {index} [{block['role_label']}]"]
            if block.get("headline"):
                parts.append(f"headline「{block['headline']}」")
            if block.get("subheadline"):
                parts.append(f"subheadline「{block['subheadline']}」")
            if block.get("labels"):
                parts.append("labels " + " / ".join(f"「{item}」" for item in block["labels"]))
            if block.get("scene"):
                parts.append(f"creative scene「{block['scene']}」")
            if block.get("evidence"):
                parts.append(f"photo-supported evidence「{block['evidence']}」")
            if block.get("cta"):
                parts.append(f"CTA「{block['cta']}」")
            module_lines.append("; ".join(parts))

    detail_lines = [
        "CONVERSION CREATIVE BRIEF for DETAIL PANELS (reviewable candidates, lower priority than product identity):",
        "AI should directly render the approved short copy and typography in each detail panel. Use a natural editorial layout chosen for that panel; vary the composition instead of repeating one rigid card.",
        "Creative usage scenes may be reasonable ecommerce settings such as a commute, cafe, bedroom, desk, kitchen or weekend outing when they fit the category. Do not claim a product function, material, measurement or result unless it is supported by the photos or seller input.",
        "Photo-supported evidence is authoritative. Scene descriptions are creative direction, not proof. Omit unsupported claims while keeping a useful scene.",
    ]
    if points:
        detail_lines.append("Evidence-bound selling-point candidates: " + "；".join(f"「{item}」" for item in points))
    if scenes:
        detail_lines.append("Creative usage-scene candidates (choose and adapt naturally): " + "；".join(f"「{item}」" for item in scenes))
    if headlines:
        detail_lines.append("Short headline candidates (render verbatim when legible): " + "；".join(f"「{item}」" for item in headlines))
    if scene_plan:
        scene_parts = []
        for index, scene in enumerate(scene_plan, start=1):
            parts = [f"Panel {index}"]
            for label, key in (("role", "role"), ("setting", "setting"), ("time", "time_of_day"), ("camera", "camera"), ("lighting", "lighting"), ("mood", "mood"), ("action", "action"), ("copy", "copy_direction")):
                if scene.get(key):
                    parts.append(f"{label}={scene[key]}")
            scene_parts.append(" / ".join(parts))
        detail_lines.append("AUTO SCENE + CAMERA PLAN (generated from product category, visible evidence and conversion goal): " + "；".join(scene_parts))
    detail_lines.extend(module_lines)
    detail_lines.extend([
        "Use one module per named detail panel in this order when enough panels exist: hook, pain point, mechanism, benefit, usage steps, scene coverage, comparison, proof, then FAQ/CTA.",
        "Each detail panel may render one 6-15 character Chinese headline, one short subheadline, 2-4 concise labels and one optional CTA; keep total copy under 50 Chinese characters.",
        "Integrate typography with the scene using wall/table negative space, callout lines, numbered steps, comparison columns, fabric or package labels, or a restrained translucent caption area. Natural integration is preferred; do not force identical boxes, borders or alignments.",
        "Vary setting, time of day, camera angle, distance, interaction and visual hierarchy across modules. Do not reuse the same background or slogan in every panel.",
        "Final priority reminder: creative scenes and copy may change the setting and layout, but never change the photographed product's colorway, proportions, artwork, visible construction or evidence status.",
    ])
    detail_note = " ".join(detail_lines)

    hero_lines = [
        "CONVERSION CREATIVE BRIEF for HERO PANELS (keep the PDD carousel clean):",
        "Use product identity and composition candidates to enrich the hero set. Only the designated lifestyle/ad panel may contain one short seller headline; Panels 1-3 and the remaining clean hero candidates stay text-free.",
        "Creative scene candidates may guide the designated lifestyle panel, but do not turn them into unsupported product claims.",
    ]
    if scenes:
        hero_lines.append("Optional lifestyle scene direction for the designated panel: " + "；".join(f"「{item}」" for item in scenes[:3]))
    if headlines:
        hero_lines.append("Optional seller headline for the designated panel, render at most one verbatim: " + "；".join(f"「{item}」" for item in headlines[:2]))
    hero_note = " ".join(hero_lines)

    if prompt_pack.get("mode") == "quality" and isinstance(prompt_pack.get("hero"), dict) and isinstance(prompt_pack.get("detail"), dict):
        prompt_pack["hero"]["prompt"] = f"{prompt_pack['hero'].get('prompt', '')}\n{hero_note}"
        prompt_pack["detail"]["prompt"] = f"{prompt_pack['detail'].get('prompt', '')}\n{detail_note}"
        prompt_pack["prompt"] = f"主图组：{prompt_pack['hero']['prompt']}\n\n详情信息图组：{prompt_pack['detail']['prompt']}"
    else:
        prompt_pack["prompt"] = f"{prompt_pack.get('prompt', '')}\n{detail_note}"
    prompt_pack["creative_brief"] = detail_note
    prompt_pack["hero_creative_brief"] = hero_note


def _build_module_prompt_previews(
    prompt_pack: dict[str, Any],
    metadata: dict[str, Any],
    product_hint: str,
    detail_modules: list[dict[str, Any]] | None = None,
    size_chart_enabled: bool = False,
    size_chart_rows: list[dict[str, Any]] | None = None,
    sku_values: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Expose copyable per-panel prompts for human review.

    The provider still receives the campaign-level contact-sheet prompt. These
    module previews make the generated plan explainable: each panel shows its
    purpose, scene, camera, approved copy and the identity/compliance rules
    that must remain in force.
    """
    blocks = _clean_creative_blocks(metadata.get("creative_blocks"), max_items=9)
    scenes = _clean_scene_plan(metadata.get("scene_plan"), max_items=9)
    style_lock = str(prompt_pack.get("style_lock") or "").strip()
    identity = str(prompt_pack.get("identity_guard") or "").strip()
    size_chart_note = ""
    if size_chart_enabled:
        size_chart_note = " A size-chart panel is allowed only with the supplied measurement rows; never invent a missing value."
    common = (
        f"Product: {product_hint.strip() or 'the photographed product'}. "
        "Use the supplied photos as identity/evidence references; preserve colorway, proportions, visible construction and artwork. "
        "Generate a new ecommerce visual with natural typography integration, no rigid repeated card, no unsupported claims, no care information panel."
        + (" Include a size chart only when explicitly enabled and use the provided rows verbatim." if size_chart_enabled else " Do not create a size-chart or measurement panel.")
    )
    previews: list[dict[str, Any]] = []

    hero_plan = list(prompt_pack.get("hero_shot_plan") or [])
    for index, shot in enumerate(hero_plan[:6]):
        scene = scenes[index] if index < len(scenes) else {}
        scene_text = " / ".join(str(scene.get(key)) for key in ("setting", "time_of_day", "camera", "lighting", "mood", "action") if scene.get(key))
        headline = str((blocks[index] if index < len(blocks) else {}).get("headline") or "").strip()
        prompt = "\n".join(filter(None, [
            style_lock,
            "HERO MODULE PROMPT",
            common,
            shot,
            f"Optional scene direction: {scene_text}" if scene_text else "",
            f"Optional short headline for the designated lifestyle panel only: 「{headline}」" if headline and index == 3 else "",
            "Panel 1 must be pure white #FFFFFF, centered full product, no text or people. Keep other clean hero panels text-free; only the designated lifestyle/ad panel may contain one short headline.",
            identity,
        ]))
        previews.append({"kind": "hero", "index": index + 1, "role": "主图候选", "scene": scene, "prompt": prompt})

    assigned_modules = [item["type"] for item in (detail_modules or []) for _ in range(max(0, int(item.get("count", 0) or 0)))]
    detail_plan = list(prompt_pack.get("shot_plan") or [])
    detail_count = len(assigned_modules) if detail_modules is not None else int((prompt_pack.get("detail") or {}).get("panel_count") or len(detail_plan) or 0)
    for index in range(detail_count):
        shot = detail_plan[index] if index < len(detail_plan) else f"Panel {index + 1}: ecommerce detail module"
        scene = scenes[index] if index < len(scenes) else {}
        block = blocks[index] if index < len(blocks) else {}
        assigned_type = assigned_modules[index] if index < len(assigned_modules) else "scene"
        assigned_label = DETAIL_MODULE_CATALOG.get(assigned_type, "详情信息图")
        parts = []
        for key, label in (("role_label", "模块"), ("headline", "标题"), ("subheadline", "副标题"), ("labels", "标签"), ("scene", "场景"), ("evidence", "证据"), ("cta", "CTA")):
            value = block.get(key)
            if isinstance(value, list):
                value = " / ".join(map(str, value))
            if value:
                parts.append(f"{label}={value}")
        scene_text = " / ".join(str(scene.get(key)) for key in ("setting", "time_of_day", "camera", "lighting", "mood", "action") if scene.get(key))
        prompt = "\n".join(filter(None, [
            style_lock,
            "DETAIL MODULE PROMPT",
            common,
            f"Assigned business module: {assigned_label} ({assigned_type}). Follow this module's purpose and do not silently substitute another module.",
            shot,
            f"Automatic scene/camera direction: {scene_text}" if scene_text else "",
            "Approved module copy: " + "；".join(parts) if parts else "Use one short evidence-bound headline, 2-4 labels and optional CTA.",
            "AI must render the typography directly and integrate it naturally with wall/table whitespace, callout lines, numbered steps, comparison columns, material labels or a restrained translucent caption. Vary the layout; do not repeat the same frame.",
            ("Do not create care, wash-label, size-chart, measurement, volume or quantity panels." if not size_chart_enabled else "Create a clean size chart only if this module is assigned; use only supplied size rows and mark missing measurements as 待补充."),
            identity,
        ]))
        if assigned_type == "size_chart" and size_chart_enabled:
            prompt += "\nSIZE CHART DATA (render verbatim; blank cells must say 待补充): " + json.dumps(size_chart_rows or [], ensure_ascii=False)
        if assigned_type == "sku_preview":
            prompt += "\nSKU PREVIEW DATA (use only selected confirmed variant values; do not recolor or invent a variant): " + json.dumps(sku_values or [], ensure_ascii=False)
        previews.append({"kind": "detail", "index": index + 1, "module_type": assigned_type, "role": assigned_label, "scene": scene, "prompt": prompt})
    return previews


def _inject_detail_module_prompt(
    prompt_pack: dict[str, Any],
    modules: list[dict[str, Any]],
    size_chart_enabled: bool = False,
    size_chart_rows: list[dict[str, Any]] | None = None,
    sku_values: list[str] | None = None,
) -> None:
    """Attach the operator's module plan to provider prompts.

    This is deliberately separate from the Skill's campaign lock: the lock
    defines visual quality while this plan defines which business panels may
    appear.  It also makes size-chart opt-in explicit in the final prompt.
    """
    if not modules:
        return
    assignments: list[str] = []
    for item in modules:
        count = int(item.get("count", 0) or 0)
        if count <= 0:
            continue
        line = f"{item.get('label') or item.get('type')} ({item.get('type')}): {count} panel(s)"
        if item.get("note"):
            line += f"; note={item['note']}"
        assignments.append(line)
    if not assignments:
        return
    note_lines = [
        "OPERATOR DETAIL MODULE PLAN (follow the selected counts; one module per panel): " + "；".join(assignments),
        "Do not silently replace a selected module with a different business module. If a selected module lacks evidence, render a restrained 待补充 placeholder rather than inventing a claim.",
    ]
    if size_chart_enabled:
        if size_chart_rows:
            note_lines.append("SIZE CHART IS ENABLED. Render only these user-supplied rows and values, keep units visible, and write 待补充 for blank cells: " + json.dumps(size_chart_rows, ensure_ascii=False))
        else:
            note_lines.append("SIZE CHART IS ENABLED BUT HAS NO MEASUREMENTS. Render a clearly marked 待补充 measurement table; do not invent any number.")
    else:
        note_lines.append("SIZE CHART IS DISABLED. Do not render a size, measurement, care or wash-label panel.")
    if any(item.get("type") == "sku_preview" for item in modules):
        if sku_values:
            note_lines.append("SKU PREVIEW IS ENABLED. Show only these confirmed variant values and preserve the photographed product; never synthesize an unprovided colorway: " + json.dumps(sku_values, ensure_ascii=False))
        else:
            note_lines.append("SKU PREVIEW IS ENABLED BUT NO CONFIRMED VARIANT VALUES ARE AVAILABLE. Use a neutral 待补充 preview placeholder; do not recolor the product.")
    note = "\n".join(note_lines)
    if isinstance(prompt_pack.get("detail"), dict):
        detail_prompt = str(prompt_pack["detail"].get("prompt", ""))
        if size_chart_enabled:
            # The base Skill prompt disables measurement panels by default.
            # Replace that default sentence when the operator explicitly opts
            # into a verified chart; otherwise the two instructions conflict.
            detail_prompt = detail_prompt.replace(
                "Do not create care, wash-label, size-chart, measurement, volume or quantity panels; use the available panel for a richer scene, material detail, packaging context or FAQ/CTA instead.",
                "Do not create care or wash-label panels. A size-chart panel is allowed only when assigned below and must use the supplied rows verbatim; keep all missing cells as 待补充.",
            )
        prompt_pack["detail"]["prompt"] = f"{detail_prompt}\n{note}"
    if prompt_pack.get("mode") == "quality" and isinstance(prompt_pack.get("hero"), dict):
        prompt_pack["prompt"] = f"主图组：{prompt_pack['hero'].get('prompt', '')}\n\n详情信息图组：{prompt_pack.get('detail', {}).get('prompt', '')}"
    else:
        prompt_pack["prompt"] = f"{prompt_pack.get('prompt', '')}\n{note}"
    prompt_pack["detail_module_plan"] = modules
    prompt_pack["size_chart_enabled"] = bool(size_chart_enabled)

def _default_spec_suggestions(product_hint: str, category: str) -> dict[str, list[str]]:
    """按品类给出可勾选的规格方向；这些候选不是事实，必须由人工确认。"""
    text = f"{product_hint or ''} {category or ''}".lower()
    suggestions: dict[str, list[str]] = {}
    if any(token in text for token in _APPAREL_HINTS):
        suggestions["尺码"] = ["XS", "S", "M", "L", "XL", "XXL", "XXXL"]
        suggestions["颜色"] = ["黑色", "白色", "灰色", "米白色", "藏蓝色", "卡其色"]
    elif any(token in text for token in _SHOE_HINTS):
        suggestions["鞋码"] = [str(size) for size in range(35, 45)]
        suggestions["颜色"] = ["黑色", "白色", "灰色", "米白色"]
    # 只有检测到明确品类时才给方向，避免把任意商品强行套上服装规格。
    return suggestions


def _validate_metadata(raw: dict[str, Any] | None, hint: str) -> dict[str, Any]:
    """把模型输出收敛成旧上架器能消费的字段，拒绝异常大数组和空值污染。"""
    raw = raw if isinstance(raw, dict) else {}
    title = str(raw.get("title") or hint or "AI生成商品").strip()
    title = re.sub(r"\s+", " ", title)[:60]
    confirmed_specs = _clean_spec_map(raw.get("specs"))
    specs = confirmed_specs or {"颜色": ["默认色"], "尺码": ["均码"]}
    attrs = {}
    if isinstance(raw.get("attrs"), dict):
        attrs = {str(k)[:30]: str(v)[:80] for k, v in raw["attrs"].items() if str(k).strip() and str(v).strip()}
    description = str(raw.get("description") or "").strip()[:1200]
    brand = str(raw.get("brand") or "").strip()[:40]
    category = str(raw.get("category") or "").strip()[:80]
    care_instructions = _clean_text_items(
        raw.get("care_instructions") or raw.get("care_info") or raw.get("care"),
        max_items=8,
        max_len=140,
    )
    raw_care_status = str(raw.get("care_status") or "").strip().lower()
    # 只有模型明确看到可读洗标/包装说明时才允许 confirmed；材质推断默认是候选。
    care_status = "confirmed" if raw_care_status == "confirmed" and care_instructions else ("candidate" if care_instructions else "needs_confirmation")
    care_note = str(raw.get("care_note") or "").strip()[:240]
    if care_instructions and not care_note:
        care_note = "AI 根据照片中可见的材质、织法或洗标整理；没有清晰洗标时请以实物洗标为准。"
    # `specs` 是模型确认或用户明确提供的值；`spec_suggestions` 是给商家勾选的
    # 规格方向，两者分开，避免把“服装常见 XL”误当成这件商品确实有 XL。
    spec_suggestions = _clean_spec_map(raw.get("spec_suggestions") or raw.get("spec_direction"))
    for name, values in _default_spec_suggestions(hint, category).items():
        merged = list(spec_suggestions.get(name, []))
        for value in values:
            if value not in merged:
                merged.append(value)
        spec_suggestions[name] = merged
    spec_direction_note = str(raw.get("spec_direction_note") or "").strip()[:300]
    if not spec_direction_note and spec_suggestions:
        spec_direction_note = "以下是按品类整理的候选方向，勾选前请按实物、包装或供应链资料核对。"
    identity_evidence = _clean_identity_items(
        raw.get("identity_evidence") or raw.get("visual_evidence") or raw.get("visible_features"),
        max_items=12,
        max_len=160,
    )
    identity_constraints = _clean_identity_items(
        raw.get("identity_constraints") or raw.get("visible_constraints") or raw.get("must_not_invent"),
        max_items=10,
        max_len=160,
    )
    uncertain_features = _clean_identity_items(
        raw.get("uncertain_features") or raw.get("uncertainties") or raw.get("occluded_features"),
        max_items=8,
        max_len=160,
    )
    identity_confidence = _clean_identity_confidence(raw.get("identity_confidence") or raw.get("visual_confidence"))
    identity_note = str(raw.get("identity_note") or "").strip()[:300]
    if not identity_note:
        identity_note = (
            "视觉证据不足时保持版型/结构中性，不能因品类名称补出纽扣、门襟、口袋等结构。"
            if not identity_evidence else
            "生图必须以照片可见结构为准；品类名称只作检索标签。"
        )
    selling_points = _clean_text_items(
        raw.get("selling_points") or raw.get("sellingPoints") or raw.get("benefits"),
        max_items=6,
        max_len=60,
    )
    scene_suggestions = _clean_text_items(
        raw.get("scene_suggestions") or raw.get("sceneSuggestions") or raw.get("usage_scenarios"),
        max_items=6,
        max_len=70,
    )
    ad_headlines = _clean_text_items(
        raw.get("ad_headlines") or raw.get("adHeadlines") or raw.get("short_headlines"),
        max_items=5,
        max_len=20,
    )
    creative_blocks = _clean_creative_blocks(
        raw.get("creative_blocks") or raw.get("pdp_blocks") or raw.get("copy_blocks"),
        max_items=9,
    )
    scene_plan = _clean_scene_plan(raw.get("scene_plan") or raw.get("scenePlan") or raw.get("shot_plan"), max_items=9)
    if not scene_plan:
        scene_plan = _fallback_scene_plan({
            "scene_suggestions": scene_suggestions,
            "creative_blocks": creative_blocks,
        }, hint, max_items=9)
    return {
        "title": title,
        "category": category,
        "brand": brand,
        "attrs": attrs,
        "specs": specs,
        "specs_confirmed": confirmed_specs,
        "specs_status": "confirmed" if confirmed_specs else "needs_confirmation",
        "spec_suggestions": spec_suggestions,
        "spec_direction_note": spec_direction_note,
        "description": description,
        "care_instructions": care_instructions,
        "care_status": care_status,
        "care_note": care_note,
        "identity_evidence": identity_evidence,
        "identity_constraints": identity_constraints,
        "uncertain_features": uncertain_features,
        "identity_confidence": identity_confidence,
        "identity_note": identity_note,
        "selling_points": selling_points,
        "scene_suggestions": scene_suggestions,
        "ad_headlines": ad_headlines,
        "creative_blocks": creative_blocks,
        "scene_plan": scene_plan,
        "module_recommendations": _clean_detail_modules(raw.get("module_recommendations")),
        'module_recommendation_reason': str(raw.get('module_recommendation_reason') or '')[:240],
        "module_recommendation_reason": str(raw.get("module_recommendation_reason") or "")[:500],
        "evidence_note": "仅保留照片或用户输入可支持的信息；未识别字段请人工核对。",
    }


def _limit_creative_brief(metadata: dict[str, Any], density: str = "rich", product_hint: str = "") -> dict[str, Any]:
    """Apply the user-selected copy/scene density without changing facts."""
    limit = 6 if str(density or "rich").strip().lower() in {"rich", "丰富", "full"} else 3
    for field in ("selling_points", "scene_suggestions"):
        metadata[field] = _clean_text_items(metadata.get(field), max_items=limit, max_len=80)
    metadata["ad_headlines"] = _clean_text_items(metadata.get("ad_headlines"), max_items=min(5, limit + 1), max_len=20)
    metadata["creative_blocks"] = _clean_creative_blocks(metadata.get("creative_blocks"), max_items=9 if limit == 6 else 5)
    if not metadata["creative_blocks"]:
        metadata["creative_blocks"] = _fallback_creative_blocks(metadata, density)
    metadata["scene_plan"] = _clean_scene_plan(metadata.get("scene_plan"), max_items=9 if limit == 6 else 5)
    if not metadata["scene_plan"]:
        metadata["scene_plan"] = _fallback_scene_plan(metadata, product_hint, max_items=9 if limit == 6 else 5)
    return metadata


def build_campaign_style_lock(style: str = "minimal") -> str:
    """返回固定文本，确保批量图的风格不漂移。"""
    style = (style or "minimal").strip().lower()
    # Accept the labels used by the UI as well as common API callers.  Unknown
    # labels intentionally fall back to minimal instead of creating a partially
    # specified palette that can drift between hero and detail requests.
    style = _STYLE_ALIASES.get(style, style)
    profile = _STYLE_PROFILES.get(style, _STYLE_PROFILES["minimal"])
    return f"{DEFAULT_STYLE_LOCK} {profile}."


def _ad_scene_instruction(model_scene: bool, ad_copy: str, *, detail: bool = False) -> str:
    """Keep the model/copy in one named panel so image-model rules do not conflict."""
    copy_note = (ad_copy or "").strip()[:120]
    if not model_scene and not copy_note:
        if detail:
            return "All panels: no people, faces, hands, testimonials, seller advertising copy, fake badges, or decorative text; short evidence-based infographic labels and icon captions are allowed."
        return "All panels: no people, faces, hands, testimonials, advertising copy, badges, or decorative text."
    if not model_scene:
        # The seller may want a typographic ad panel without enabling a face/model.
        return (
            "Panel 4 only may contain the seller's advertising typography; every panel must contain no person, face, or hand. "
            "Render the seller copy exactly in a high-contrast text-safe block using readable Simplified Chinese; "
            f"seller copy (verbatim): 「{copy_note}」. Do not add prices, rankings, medical/performance claims, testimonials, fake badges, or unverified promises."
        )
    panel_name = "one designated detail panel" if detail else "Panel 4 only"
    rules = [
        f"{panel_name} may contain one synthetic adult model; every other panel must contain no person, face, or hand.",
        "The model is generic and not based on any reference person's identity; use a real-camera look (Canon EOS R5 or Sony A7 IV), natural window light, visible pores and small natural asymmetry, NOT retouched, NOT an AI-looking beauty face.",
        "The product remains the visual subject with the photographed colorway, construction, logo placement, drape, and realistic fit preserved; do not let the model hide it.",
    ]
    if detail:
        rules.append(
            "Every detail panel may contain one short headline, 2-4 evidence labels and an optional CTA. Let the typography integrate naturally into the composition: use varied editorial layouts, quiet wall space, fabric/packaging labels, callout lines, numbered steps, comparison columns or a soft translucent caption area as appropriate. Do not force every panel into the same card or border. Keep Chinese copy large, readable and sparse."
        )
    else:
        rules.append("Only the designated model/ad panel may contain typography; keep the other hero panels text-free so they remain valid PDD main-image candidates.")
    if copy_note:
        if detail:
            rules.append(
                "Use the seller copy in the designated model/lifestyle panel, integrated into the scene with one headline, 2-3 short labels and an optional CTA; preserve the copy verbatim and do not invent claims. "
                f"Seller copy (verbatim): 「{copy_note}」."
            )
        else:
            rules.append(
                "In that one panel, render the seller copy exactly as written in a high-contrast text-safe area using readable Simplified Chinese; "
                "one headline is at most 15 characters, optional evidence labels are 2-3 short phrases, and CTA is at most 8 characters. "
                f"Seller copy (verbatim, do not invent words): 「{copy_note}」."
            )
    else:
        rules.append("In that panel, use only a short neutral headline supported by the reference; never invent a claim or number.")
    rules.append("Do not add prices, rankings, medical/performance claims, testimonials, fake badges, certifications, or unverified promises.")
    return " ".join(rules)


def build_prompt_pack(product_hint: str, style: str, rows: int, cols: int, model_scene: bool = False, ad_copy: str = "") -> dict[str, Any]:
    """构造一次调用可用的网格生图 Prompt。

    网格图只承载可观察的卖点与细节，不要求模型生成大段中文，以减少乱码和虚构承诺。
    """
    lock = build_campaign_style_lock(style)
    product = product_hint.strip() or "the photographed product"
    panel_count = rows * cols
    template_ids, template_hints = _template_hints(product_hint, panel_count, style, model_scene, ad_copy)
    # Blend the skill's category templates with an explicit conversion sequence.
    # A template hint alone tends to make image models repeat the input photo;
    # the blueprint supplies a different camera/layout purpose for each cell.
    shot_plan = []
    hero_blueprints = _category_blueprints(product_hint, "hero")
    for i in range(panel_count):
        blueprint = hero_blueprints[i % len(hero_blueprints)]
        if model_scene and i == 3:
            blueprint = "HERO 4: one synthetic adult model wearing/using the product in a realistic lifestyle scene; product dominant, face generic, natural fit, no other people"
        template_hint = template_hints[i % len(template_hints)]
        shot_plan.append(f"Panel {i + 1}: {blueprint}; template cue: {template_hint}")
    shot_plan_text = "\n".join(shot_plan)
    prompt = textwrap.dedent(
        f"""
        {lock}
        Create one NEW ecommerce image contact sheet for {product}, using all supplied product photos only as identity and evidence references.
        Use an exact {rows} by {cols} grid with {panel_count} equal panels, thin neutral dividers, and no content crossing panel boundaries. Output a square 1:1 contact sheet at the highest available resolution; each crop must remain at least 480×480 before local post-processing, and target 800×800 or larger when the provider size allows.
        Each panel must be independently useful after cropping. Follow this conversion-oriented shot plan exactly:
        {shot_plan_text}
        Panel 1 is the conservative PDD carousel first frame: pure white #FFFFFF background, centered full product, no people, no props, no advertising text, no logo or watermark, no collage, and only a soft natural contact shadow when the category requires it.
        The reference may be a multi-photo board; ignore its borders, grid lines, labels, and camera UI. This is a creative transformation task:
        do not reproduce any supplied photo as an unchanged copy, screenshot, collage, or contact sheet inside a cell.
        Every cell must change at least two of camera angle, crop, lighting, background, composition, or use context while preserving the same physical product identity.
        Treat the seller's product hint as a tentative category label, never as proof of construction. Photos are authoritative: preserve only evidence-supported shape, color, logo/artwork placement, printed motifs, accessories, materials, visible closures and proportions. If a button placket, pocket, seam, ingredient, port, measurement or other structure is not visible, keep it generic and do not invent it just to fill a panel. Treat any unreadable lettering in the reference as non-semantic artwork: never replace it with new gibberish, a fake brand, or a different slogan.
        Detail cells must be true mobile-friendly ecommerce infographics (headline, 2-4 short labels, thin-line icons, callout lines, timeline, comparison, or proof placeholder as appropriate),
        not merely another product photo. Use the approved short headline, labels and CTA candidates in the matching detail panel; render them as clean, sparse, readable Chinese typography integrated with the scene. If a candidate is unsupported or would require dense copy, omit that candidate rather than inventing claims or outputting gibberish.
        Make the set visually premium: one focal point per cell, product scale 25-45% in lifestyle views and 50-70% in macro/feature views, realistic depth, controlled shadows, a restrained two-to-three-color palette, varied close/medium/contextual framing, and no repeated template card or identical icon row.
        never invent certifications, test results, ratings, sales, medical claims, performance numbers, dimensions, ingredients, warranties, brand marks, or competitor claims.
        Keep at least 45% whitespace in hero panels and 35% in detail panels, use clear visual hierarchy, sharp product edges, realistic shadows, and consistent lighting. Reserve the top-center 200×100 area and upper-left 200×100 area as empty platform/logo-safe zones.
        Do not add hands, {('additional people,' if model_scene else 'people,')} extra products, fake logos, watermarks, QR codes, dense paragraphs, illegible microtext, gradients, or decorative clutter.
        Product identity has priority over style: no melting, duplicated parts, warped geometry, changed colorway, unreadable logo, or contradictory accessories.
        {_ad_scene_instruction(model_scene, ad_copy)}
        """
    ).strip()
    return {
        "skill": "ecom-details-image",
        "skill_source": _skill_template_root()[1],
        "skill_template_count": 25,
        "style_lock": lock,
        "prompt": prompt,
        "rows": rows,
        "cols": cols,
        "panel_count": panel_count,
        "hero_shot_plan": shot_plan[:min(5, panel_count)],
        "template_ids": template_ids,
        "shot_plan": shot_plan,
        "negative_constraints": [
            "unchanged source photo", "duplicate product", "fake logo", "watermark", "dense text",
            "gibberish text", "unsupported claims", "invented dimensions", "extra products",
            "warped geometry", "melted edges", "inconsistent colorway",
        ],
        "model_scene": bool(model_scene),
        "ad_copy": ad_copy.strip()[:120],
    }


def build_quality_prompt_packs(product_hint: str, style: str, rows: int, cols: int, model_scene: bool = False, ad_copy: str = "") -> dict[str, Any]:
    """高质量模式的双请求提示词：主图与详情信息图分开，避免网格任务互相稀释。"""
    lock = build_campaign_style_lock(style)
    product = product_hint.strip() or "the photographed product"
    _template_ids, template_hints = _template_hints(product_hint, max(6, rows * cols), style, model_scene, ad_copy)
    hero_blueprints = _category_blueprints(product_hint, "hero")
    detail_blueprints = _category_blueprints(product_hint, "detail")
    hero_prompt = textwrap.dedent(
        f"""
        {lock}
        Create a premium ecommerce hero-image contact sheet for {product}, using all supplied photos only as identity and evidence references.
        Use an exact 2 by 3 grid with six equal panels and thin neutral dividers; no content crosses panel boundaries. Output a square 1:1 contact sheet at the highest available resolution; each crop must remain at least 480×480 before local post-processing, and target 800×800 or larger when the provider size allows.
        Panel 1: {hero_blueprints[0]}.
        Panel 2: {hero_blueprints[1]}.
        Panel 3: {hero_blueprints[2]}.
        Panel 4: {hero_blueprints[3]}; if model scene is enabled, use exactly one generic adult model here.
        Panel 5: {hero_blueprints[4]}.
        Panel 6: {hero_blueprints[5]}.
        Panel 1 is the conservative PDD carousel first frame: pure white #FFFFFF background, centered full product, no people, no props, no advertising text, no logo or watermark, no collage, and only a soft natural contact shadow when the category requires it.
        Every panel must be a new composition and must change at least two of camera angle, crop, lighting, background, composition, or context. Reserve the top-center 200×100 area and upper-left 200×100 area as empty platform/logo-safe zones.
        Treat the seller's product hint as tentative. Photos are authoritative: preserve the same physical product identity, colorway, materials, logo/artwork placement, printed motif, visible closures and accessories. Do not add a cardigan opening, buttons, pockets or other construction that the photos do not show. Do not copy any input photo unchanged; do not redraw or alter printed lettering.
        Keep hero panels clean and suitable for a PDD main-image carousel. Panels 1-3 must contain no advertising text; only the single lifestyle panel may contain the seller copy. No fake logo, watermark, extra products, hands, {('additional people,' if model_scene else 'people,')}
        unsupported claims, invented dimensions, altered geometry, color drift, duplicate objects, or illegible details.
        {_ad_scene_instruction(model_scene, ad_copy)}
        """
    ).strip()
    detail_plan = []
    detail_blueprints = _category_blueprints(product_hint, "detail")
    for i in range(rows * cols):
        blueprint = detail_blueprints[i % len(detail_blueprints)]
        if model_scene and i == 3:
            blueprint = "DETAIL 4: one model-showcase lifestyle card, synthetic adult model with visible natural skin texture wearing/using the product; product remains dominant and fit is realistic"
        detail_plan.append(
            f"Panel {i + 1}: {blueprint}; "
            f"template cue: {template_hints[i % len(template_hints)]}"
        )
    detail_prompt = textwrap.dedent(
        f"""
        {lock}
        Create one mobile-first PDD product-detail infographic contact sheet for {product}, using all supplied photos only as identity and evidence references.
        Use an exact {rows} by {cols} grid with {rows * cols} equal panels, thin neutral dividers, and no content crossing panel boundaries.
        Follow this information-design plan exactly:
        {chr(10).join(detail_plan)}
        E-commerce infographic layout: every panel must be a true e-commerce infographic, not a plain repeated product photo: use a clear visual hierarchy, one short headline area,
        2-4 short label slots, simple consistent line icons, callout lines, numbered steps, comparison blocks, or proof placeholders as appropriate.
        Make the result look like a polished mobile commerce page: one focal point per panel, product scale around 25-45% in lifestyle panels and 50-70% in macro/feature panels, realistic depth and shadows, a restrained two-to-three-color palette, and no more than 2-3 supporting props when props are justified. Build visual rhythm across the set with alternating close, medium and contextual views; every panel must have a clear reason to exist.
        Let AI complete the typography and layout inside the image. Every panel may use one short headline, a short subheadline, 2-4 labels and an optional CTA, with copy taken from the approved PDP module map. Integrate the text naturally into the scene: vary between editorial whitespace, wall or tabletop captions, product callout lines, numbered steps, comparison columns and quiet translucent labels. Do not force every panel into the same rigid box or border, and do not place a large text block over the product. Render the seller-provided ad copy only in the one advertising/lifestyle panel when present.
        Keep Chinese copy sparse, high-contrast and readable at mobile size; use natural line breaks and leave enough breathing room around each text group.
        Avoid template-like repetition: do not reuse the same green pill, same crop, same background, same icon row or same headline treatment in every panel. Use full-bleed photography, edge-aligned captions, callout lines and asymmetrical editorial composition when they improve the story while keeping the product easy to understand.
        Do not repeat the same headline in every panel. Do not create care, wash-label, size-chart, measurement, volume or quantity panels; use the available panel for a richer scene, material detail, packaging context or FAQ/CTA instead.
        Use only facts visible in the reference or supplied by the seller. Never invent certifications, dimensions, ingredients, performance numbers,
        ratings, sales, warranties, medical claims, competitor claims, or brand marks. Preserve product identity, printed artwork and colorway, and use realistic scale; never redraw a logo or slogan into gibberish.
        Keep at least 35% whitespace in every panel and reserve a clear text-safe block for any short labels. No watermark, QR code, dense paragraphs, gibberish, extra products, duplicated parts,
        melting, warped geometry, contradictory accessories, or color drift.
        {_ad_scene_instruction(model_scene, ad_copy, detail=True)}
        """
    ).strip()
    return {
        "skill": "ecom-details-image",
        "skill_source": _skill_template_root()[1],
        "skill_template_count": 25,
        "mode": "quality",
        "style_lock": lock,
        "hero": {"prompt": hero_prompt, "rows": 2, "cols": 3, "panel_count": 6},
        "detail": {"prompt": detail_prompt, "rows": rows, "cols": cols, "panel_count": rows * cols},
        "prompt": f"主图组：{hero_prompt}\n\n详情信息图组：{detail_prompt}",
        "hero_shot_plan": [f"Panel {index + 1}: {hero_blueprints[index]}" for index in range(6)],
        "template_ids": _template_ids,
        "shot_plan": detail_plan,
        "negative_constraints": [
            "unchanged source photo", "plain repeated product photo", "fake logo", "watermark", "dense text",
            "gibberish text", "unsupported claims", "invented dimensions", "extra products", "warped geometry",
            "melted edges", "inconsistent colorway", "duplicate product",
        ],
        "model_scene": bool(model_scene),
        "ad_copy": ad_copy.strip()[:120],
    }


def _open_image(data: bytes) -> Image.Image:
    if not data:
        raise WorkflowError("没有收到商品照片")
    if len(data) > MAX_UPLOAD_BYTES:
        raise WorkflowError(f"照片过大，请控制在 {MAX_UPLOAD_BYTES // 1024 // 1024}MB 以内")
    try:
        image = Image.open(io.BytesIO(data))
        if image.format not in {"JPEG", "PNG", "WEBP", "BMP"}:
            raise WorkflowError("请上传真实JPG、PNG或WEBP图片")
        if image.width * image.height > 40_000_000:
            raise WorkflowError("图片像素超过4000万，请缩小原图后上传")
        image.load()
        if image.width < 160 or image.height < 160:
            raise WorkflowError("照片尺寸太小，至少需要 160×160 像素")
        return ImageOps.exif_transpose(image).convert("RGB")
    except (UnidentifiedImageError, OSError) as exc:
        raise WorkflowError("无法读取照片，请上传 JPG、PNG 或 WEBP 图片") from exc


def _combine_reference_images(images: list[Image.Image]) -> Image.Image:
    """把多张用户照片合成一张参考板，供只接受单图的生图接口使用。

    这是本地预处理，不会增加 API 调用次数；每张原图仍会单独保存，方便用户选作主图。
    """
    if not images:
        raise WorkflowError("至少需要一张商品照片")
    if len(images) == 1:
        return images[0]
    count = min(len(images), 12)
    cols = 3 if count > 4 else 2
    rows = (count + cols - 1) // cols
    cell = 900
    board = Image.new("RGB", (cols * cell, rows * cell), (247, 245, 240))
    for i, image in enumerate(images[:count]):
        fitted = _fit_image(image, (cell - 48, cell - 48), bg=(247, 245, 240))
        x = (i % cols) * cell + 24
        y = (i // cols) * cell + 24
        board.paste(fitted, (x, y))
    return board


def _fit_image(image: Image.Image, size: tuple[int, int], bg=(247, 245, 240)) -> Image.Image:
    image = ImageOps.contain(image, size, method=Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", size, bg)
    canvas.paste(image, ((size[0] - image.width) // 2, (size[1] - image.height) // 2))
    return canvas


def _save_jpeg(
    image: Image.Image,
    path: Path,
    max_edge: int = MAX_LONG_EDGE,
    max_bytes: int | None = None,
) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = image.convert("RGB")
    scale = min(1.0, max_edge / max(image.width, image.height))
    if scale < 1:
        image = image.resize((max(1, int(image.width * scale)), max(1, int(image.height * scale))), Image.Resampling.LANCZOS)
    # 逐步降低压缩质量/尺寸，确保单张图片不会超过常见 PDD 上传限制；
    # 图片仍尽量保持 480px 以上，避免用“合规大小”换来不可用的缩略图。
    byte_budget = max_bytes if max_bytes is not None else MAX_OUTPUT_BYTES
    encoded = b""
    for quality in (92, 88, 84, 80, 76, 72, 68):
        buf = io.BytesIO()
        image.save(buf, "JPEG", quality=quality, optimize=True, progressive=True)
        encoded = buf.getvalue()
        if len(encoded) <= byte_budget:
            break
    while len(encoded) > byte_budget and min(image.width, image.height) > PDD_MIN_IMAGE_EDGE:
        image = image.resize((max(480, int(image.width * 0.85)), max(480, int(image.height * 0.85))), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        image.save(buf, "JPEG", quality=76, optimize=True, progressive=True)
        encoded = buf.getvalue()
    path.write_bytes(encoded)
    return str(path)


def normalize_input(image: Image.Image, out_dir: Path) -> str:
    """生成不超过 PDD 常见限制的用户原图，作为首张主图候选。"""
    side = min(MAX_LONG_EDGE, max(image.width, image.height))
    return _save_jpeg(_fit_image(image, (side, side)), out_dir / "main_01_source.jpg")


def crop_grid(
    sheet: Image.Image,
    out_dir: Path,
    rows: int,
    cols: int,
    upscale: float = 1.5,
    max_bytes: int | None = None,
    square: bool = False,
) -> list[str]:
    """按等分网格裁剪并放大，所有裁剪边界留出安全内边距。

    Hero candidates use ``square=True`` so a provider that returns a 2×3
    vertical-cell sheet still produces stable 1:1 carousel files.  The
    product is contained on a white canvas rather than center-cropped away.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    width, height = sheet.size
    margin = max(2, min(width // (cols * 20), height // (rows * 20), 16))
    results = []
    for row in range(rows):
        for col in range(cols):
            left = round(col * width / cols) + margin
            top = round(row * height / rows) + margin
            right = round((col + 1) * width / cols) - margin
            bottom = round((row + 1) * height / rows) - margin
            if right <= left or bottom <= top:
                continue
            crop = sheet.crop((left, top, right, bottom))
            crop = ImageOps.exif_transpose(crop)
            if square:
                side = max(crop.width, crop.height)
                canvas = Image.new("RGB", (side, side), (255, 255, 255))
                contained = ImageOps.contain(crop.convert("RGB"), (side, side), method=Image.Resampling.LANCZOS)
                canvas.paste(contained, ((side - contained.width) // 2, (side - contained.height) // 2))
                crop = canvas
            target_w = max(PDD_MIN_IMAGE_EDGE, int(crop.width * max(1.0, upscale)))
            target_h = max(PDD_MIN_IMAGE_EDGE, int(crop.height * max(1.0, upscale)))
            crop = crop.resize((target_w, target_h), Image.Resampling.LANCZOS)
            # 轻量锐化只补偿缩放造成的边缘软化，不伪造纹理；真正需要 2x/4x 超分时
            # 应降低网格数量或接入专用超分服务，不能把小格子硬拉大。
            crop = crop.filter(ImageFilter.UnsharpMask(radius=1.1, percent=115, threshold=3))
            results.append(_save_jpeg(crop, out_dir / f"detail_{len(results) + 1:02d}.jpg", max_bytes=max_bytes))
    return results


def _render_verified_size_chart(rows: list[dict[str, Any]], out_path: Path, style: str = "minimal", columns: list[str] | None = None, page_label: str = "") -> str:
    """Render every supplied value exactly; the execution plan owns pagination."""
    from size_chart import render
    return render(rows, out_path, style, columns, page_label)


def _audit_output_images(
    paths: list[str],
    max_bytes: int = MAX_OUTPUT_BYTES,
    min_width: int = PDD_MIN_IMAGE_EDGE,
    aspect_min: float | None = None,
    soft_max_height: int | None = None,
) -> dict[str, Any]:
    """Run cheap deterministic checks on the files that will enter the PDD pack."""
    dimensions: list[tuple[int, int]] = []
    sizes: list[int] = []
    hashes: list[str] = []
    visual_hashes = []
    unreadable = 0
    for raw_path in paths:
        path = Path(raw_path)
        try:
            raw = path.read_bytes()
            sizes.append(len(raw))
            hashes.append(hashlib.sha256(raw).hexdigest())
            with Image.open(io.BytesIO(raw)) as image:
                dimensions.append((int(image.width), int(image.height)))
                thumb=image.convert('RGB').resize((9,8))
                grey=list(getattr(thumb.convert('L'),'get_flattened_data',thumb.convert('L').getdata)())
                visual=sum(int(grey[row*9+col]>grey[row*9+col+1])<<(row*8+col) for row in range(8) for col in range(8))
                pixels=list(getattr(thumb,'get_flattened_data',thumb.getdata)())
                mean=tuple(sum(pixel[channel] for pixel in pixels)/len(pixels) for channel in range(3))
                visual_hashes.append((visual,mean))
        except (OSError, UnidentifiedImageError):
            unreadable += 1
    min_edge = min((min(width, height) for width, height in dimensions), default=0)
    duplicate_count = len(hashes) - len(set(hashes))
    oversized_count = sum(1 for size in sizes if size > max_bytes)
    undersized_count = sum(1 for width, height in dimensions if width < min_width or height < PDD_MIN_IMAGE_EDGE)
    aspect_violation_count = sum(
        1 for width, height in dimensions
        if aspect_min is not None and (height <= 0 or (width / height) < aspect_min)
    )
    soft_tall_count = sum(
        1 for width, height in dimensions
        if soft_max_height is not None and height > soft_max_height
    )
    near_pairs=[{'first':first+1,'second':second+1} for first,(first_hash,first_mean) in enumerate(visual_hashes)
                for second,(second_hash,second_mean) in enumerate(visual_hashes[:first])
                if (first_hash^second_hash).bit_count()<=4 and sum(abs(a-b) for a,b in zip(first_mean,second_mean))/3<25]
    return {
        "count": len(paths),
        "readable_count": len(dimensions),
        "unreadable_count": unreadable,
        "min_edge": min_edge,
        "max_edge": max((max(width, height) for width, height in dimensions), default=0),
        "max_bytes": max(sizes, default=0),
        "max_allowed_bytes": max_bytes,
        "duplicate_count": duplicate_count,
        'near_duplicate_pairs': near_pairs,
        'near_duplicate_count': len(near_pairs),
        'near_duplicate_note': '本地相似度提示，可能包含相同商品的合理角度；请人工判断是否重复',
        "undersized_count": undersized_count,
        "oversized_count": oversized_count,
        "aspect_violation_count": aspect_violation_count,
        "soft_tall_count": soft_tall_count,
        "status": "pass" if not (unreadable or duplicate_count or undersized_count or oversized_count or aspect_violation_count) else "review",
    }


def _build_quality_audit(
    main_paths: list[str],
    detail_paths: list[str],
    main_max_bytes: int = PDD_CAROUSEL_MAX_BYTES,
    detail_max_bytes: int = PDD_DETAIL_MAX_BYTES,
    spec_paths: list[str] | None = None,
) -> dict[str, Any]:
    """Summarize output checks without storing image content or credentials."""
    main_audit = _audit_output_images(
        main_paths,
        max_bytes=main_max_bytes,
        min_width=PDD_MIN_IMAGE_EDGE,
    )
    detail_audit = _audit_output_images(
        detail_paths,
        max_bytes=detail_max_bytes,
        min_width=PDD_MIN_IMAGE_EDGE,
        aspect_min=1 / 3,
        soft_max_height=PDD_DETAIL_SOFT_MAX_HEIGHT,
    )
    spec_audit = _audit_output_images(spec_paths or [], max_bytes=main_max_bytes, min_width=PDD_MIN_IMAGE_EDGE)
    for paths, audit in ((main_paths, main_audit), (spec_paths or [], spec_audit)):
        audit["square_violation_count"] = 0
        for raw_path in paths:
            try:
                with Image.open(raw_path) as im:
                    audit["square_violation_count"] += int(im.width != im.height)
            except (OSError, UnidentifiedImageError):
                continue
        if audit["square_violation_count"]:
            audit["status"] = "review"
    main_hashes = set()
    detail_hashes = set()
    for paths, target in ((main_paths, main_hashes), (detail_paths, detail_hashes)):
        for raw_path in paths:
            try:
                target.add(hashlib.sha256(Path(raw_path).read_bytes()).hexdigest())
            except OSError:
                continue
    overlap = len(main_hashes & detail_hashes)
    return {
        "status": "pass" if main_audit["status"] == "pass" and detail_audit["status"] == "pass" and spec_audit["status"] == "pass" and overlap == 0 else "review",
        "file_check_status": "pass" if main_audit["status"] == detail_audit["status"] == spec_audit["status"] == "pass" else "review",
        "content_review_status": "pending_manual",
        "spec": spec_audit,
        "main": main_audit,
        "detail": detail_audit,
        "main_detail_overlap": overlap,
        "notes": "主图/详情图分别质检；详情图按宽≥480、宽高比不低于1:3和1MB预算检查；请人工复核模型内容、中文排版和商品身份。",
    }


def _mock_sheet(image: Image.Image, rows: int, cols: int) -> Image.Image:
    """离线验收用的确定性网格：不伪造商品内容，只改变裁切、背景和亮度。"""
    cell_w = max(640, min(1200, image.width))
    cell_h = cell_w
    sheet = Image.new("RGB", (cell_w * cols, cell_h * rows), (235, 232, 225))
    palette = [(247, 245, 240), (232, 239, 244), (242, 235, 225), (236, 243, 232), (242, 232, 238), (231, 237, 241)]
    for i in range(rows * cols):
        bg = Image.new("RGB", (cell_w, cell_h), palette[i % len(palette)])
        factor = 0.96 + (i % 3) * 0.04
        variant = ImageEnhance.Contrast(ImageEnhance.Brightness(image).enhance(factor)).enhance(1.02)
        variant = _fit_image(variant, (cell_w - 80, cell_h - 80), bg=palette[i % len(palette)])
        bg.paste(variant, ((cell_w - variant.width) // 2, (cell_h - variant.height) // 2))
        sheet.paste(bg, ((i % cols) * cell_w, (i // cols) * cell_h))
    return sheet


def _http_json(url: str, payload: dict[str, Any], key: str, timeout: int) -> dict[str, Any]:
    from safe_http import request_json, private_api_enabled, OutboundError
    try:
        return request_json("POST", url, key=key, json_payload=payload, timeout=timeout, max_bytes=48 * 1024 * 1024, allow_private=private_api_enabled())
    except OutboundError as exc:
        raise WorkflowError(str(exc)) from exc


def _download(url: str, timeout: int) -> bytes:
    from safe_http import download_bytes, private_api_enabled, OutboundError
    try:
        return download_bytes(url, timeout=timeout, max_bytes=MAX_UPLOAD_BYTES, allow_private=private_api_enabled())
    except OutboundError as exc:
        raise WorkflowError(str(exc)) from exc


def _effective_reference_mode(config: WorkflowConfig) -> str:
    """Resolve the image endpoint mode without exposing credentials or prompt data.

    ``auto`` is deliberately treated as edit when a generations URL is supplied:
    otherwise a product photo mentioned in the prompt is silently ignored.  The
    resolved value is returned in the task trace so the UI can prove whether a
    reference was actually sent.
    """
    configured = (config.image_reference_mode or os.getenv("PDD_IMAGE_REFERENCE_MODE", "auto")).strip().lower()
    legacy_mode = os.getenv("PDD_IMAGE_API_MODE", "").strip().lower()
    if configured == "auto" and legacy_mode in {"edit", "generation"}:
        configured = legacy_mode
    if configured not in {"auto", "edit", "generation"}:
        configured = "auto"
    if configured == "auto":
        configured = "edit" if "/images/edits" in (config.image_api_url or "").lower() else (
            "edit" if "/images/generations" in (config.image_api_url or "").lower() else "generation"
        )
    return configured


# A surprisingly common gateway failure mode is to return the first multipart
# input unchanged while still replying with a successful ``data[0]`` payload.
# Treating that response as an AI result is worse than failing the task: it can
# make the seller believe that a transformed image was produced and can also
# silently re-publish a source photo.  The checks below are intentionally
# conservative.  They only reject an output when it is effectively the same
# raster as a reference, or when its resolution cannot support the requested
# panel grid.  They do not try to judge whether a model preserved a product's
# identity or whether the creative direction is good.
_DUPLICATE_MAE_THRESHOLD = 0.035
_DUPLICATE_HASH_DISTANCE = 10
_MIN_GRID_CELL_EDGE = 256


def _image_average_hash(image: Image.Image, size: int = 8) -> int:
    """Return a tiny perceptual average hash without an optional dependency."""
    gray = ImageOps.fit(image.convert("L"), (size, size), method=Image.Resampling.LANCZOS)
    flattened = getattr(gray, "get_flattened_data", None)
    values = list(flattened()) if callable(flattened) else list(gray.getdata())
    mean = sum(values) / max(1, len(values))
    result = 0
    for value in values:
        result = (result << 1) | int(value >= mean)
    return result


def _image_difference(left: Image.Image, right: Image.Image, size: int = 64) -> tuple[float, int]:
    """Compare two images after normalizing scale/aspect for duplicate detection.

    ``mae`` is in the 0..1 range.  A low MAE *and* a low average-hash distance
    is required before we classify an image as unchanged; requiring both keeps
    a legitimate generated image with similar colour blocks from being rejected.
    """
    left_fit = ImageOps.fit(left.convert("RGB"), (size, size), method=Image.Resampling.LANCZOS)
    right_fit = ImageOps.fit(right.convert("RGB"), (size, size), method=Image.Resampling.LANCZOS)
    diff = ImageChops.difference(left_fit, right_fit)
    # ImageStat.mean is one value per channel.  Normalise by the largest
    # possible RGB channel difference to make the threshold resolution agnostic.
    mae = sum(ImageStat.Stat(diff).mean) / (3.0 * 255.0)
    hash_left = _image_average_hash(left_fit)
    hash_right = _image_average_hash(right_fit)
    hash_distance = (hash_left ^ hash_right).bit_count()
    return float(mae), int(hash_distance)


def _is_effectively_duplicate(left: Image.Image, right: Image.Image) -> tuple[bool, float, int]:
    mae, hash_distance = _image_difference(left, right)
    return mae <= _DUPLICATE_MAE_THRESHOLD and hash_distance <= _DUPLICATE_HASH_DISTANCE, mae, hash_distance


def _validate_generated_sheet(
    sheet: Image.Image,
    *,
    input_image: Image.Image,
    reference_images: list[Image.Image] | None,
    rows: int,
    cols: int,
    trace: dict[str, Any] | None = None,
) -> None:
    """Reject unchanged/undersized API responses before they reach crop_grid.

    The whole sheet is compared with the combined reference and every
    individual reference.  The per-cell comparison catches a provider that
    returns a repeated source photo as a nominal contact sheet.  We require a
    majority of cells to be duplicates before rejecting, so a generated hero
    panel that intentionally resembles a source close-up remains usable.
    """
    width, height = sheet.size
    min_cell_edge = min(width / max(1, cols), height / max(1, rows))
    refs = [ref for ref in (reference_images or [input_image]) if ref is not None][:12]
    diagnostics: dict[str, Any] = {
        "width": int(width),
        "height": int(height),
        "rows": int(rows),
        "cols": int(cols),
        "min_cell_edge": round(float(min_cell_edge), 2),
        "duplicate_reference_count": 0,
        "duplicate_panel_count": 0,
        "status": "passed",
    }
    if trace is not None:
        trace["output_validation"] = diagnostics
    if min_cell_edge < _MIN_GRID_CELL_EDGE:
        diagnostics["status"] = "rejected_grid_resolution"
        raise WorkflowError(
            f"生图接口返回分辨率不足以切出 {rows}×{cols} 网格（每格约 {min_cell_edge:.0f}px）；"
            "请提高输出尺寸或减少网格数量，避免放大后模糊。"
        )
    if min_cell_edge < 480:
        diagnostics["quality_warning"] = (
            f"当前网格每格约 {min_cell_edge:.0f}px，裁剪后的放大图可能偏软；"
            "建议提高生图尺寸或减少网格数量。"
        )

    # If the provider returned an image with exactly the same dimensions as a
    # source, prefer the single-image echo diagnostic.  This avoids classifying
    # a solid-colour test/photo as a repeated grid merely because every crop
    # happens to resemble the source.
    same_dimension = any(sheet.size == ref.size for ref in refs)
    if same_dimension:
        whole_matches: list[tuple[float, int]] = []
        for ref in refs:
            duplicate, mae, distance = _is_effectively_duplicate(sheet, ref)
            whole_matches.append((mae, distance))
            if duplicate:
                diagnostics["duplicate_reference_count"] += 1
        if diagnostics["duplicate_reference_count"]:
            diagnostics["status"] = "rejected_duplicate"
            best = min(whole_matches, key=lambda value: (value[0], value[1]))
            diagnostics["best_mae"] = round(best[0], 6)
            diagnostics["best_hash_distance"] = best[1]
            raise WorkflowError(
                "生图接口返回内容与上传照片基本相同，已拦截，未将原图当作生图成品。"
                "请确认服务商使用 /images/edits（或选择 edit 模式）并检查模型/API 网关配置。"
            )

    # If the provider wraps the source in the requested grid, compare each
    # panel.  More than half unchanged panels is treated as a failed edit.
    panel_count = max(1, int(rows) * int(cols))
    duplicate_panels = 0
    for row in range(rows):
        for col in range(cols):
            left = round(col * width / cols)
            top = round(row * height / rows)
            right = round((col + 1) * width / cols)
            bottom = round((row + 1) * height / rows)
            if right <= left or bottom <= top:
                continue
            panel = sheet.crop((left, top, right, bottom))
            if any(_is_effectively_duplicate(panel, ref)[0] for ref in refs):
                duplicate_panels += 1
    diagnostics["duplicate_panel_count"] = duplicate_panels
    diagnostics["duplicate_panel_threshold"] = max(1, (panel_count + 1) // 2)
    if duplicate_panels >= diagnostics["duplicate_panel_threshold"]:
        diagnostics["status"] = "rejected_duplicate_grid"
        raise WorkflowError(
            "生图接口返回的网格大部分仍是上传原图，已拦截，避免原图被误当作详情图。"
            "请检查 edit 接口、参考图字段和服务商返回内容。"
        )

    # Compare the complete response after the panel check.  A repeated source
    # grid is visually dominated by unchanged panels, so it should get the
    # more actionable grid-specific error above instead of being classified as
    # a single-image echo merely because the colour blocks are similar.
    if not same_dimension:
        whole_matches = []
        for ref in refs:
            duplicate, mae, distance = _is_effectively_duplicate(sheet, ref)
            whole_matches.append((mae, distance))
            if duplicate:
                diagnostics["duplicate_reference_count"] += 1
        if diagnostics["duplicate_reference_count"]:
            diagnostics["status"] = "rejected_duplicate"
            best = min(whole_matches, key=lambda value: (value[0], value[1]))
            diagnostics["best_mae"] = round(best[0], 6)
            diagnostics["best_hash_distance"] = best[1]
            raise WorkflowError(
                "生图接口返回内容与上传照片基本相同，已拦截，未将原图当作生图成品。"
                "请确认服务商使用 /images/edits（或选择 edit 模式）并检查模型/API 网关配置。"
            )


def generate_sheet(
    config: WorkflowConfig,
    prompt: str,
    input_image: Image.Image,
    out_path: Path,
    rows: int = DEFAULT_ROWS,
    cols: int = DEFAULT_COLS,
    progress: Callable[[str], None] | None = None,
    trace: dict[str, Any] | None = None,
    reference_images: list[Image.Image] | None = None,
) -> Image.Image:
    """真实通道支持 OpenAI-compatible images API；无 API 时走 mock。"""
    progress = progress or (lambda _msg: None)
    if config.provider_mode in {"mock", "local"} or not config.image_api_url:
        if trace is not None:
            trace.update({"mode": "offline", "endpoint_kind": "none", "reference_sent": False})
        progress("使用本地离线 mock 生成网格，验证裁剪与上架链路")
        sheet = _mock_sheet(input_image, rows, cols)
        _save_jpeg(sheet, out_path, max_edge=MAX_LONG_EDGE * 2)
        return sheet

    progress("调用生图 API（预计一次计费请求）")
    default_size = "1536x1536" if rows == cols else ("2048x2048" if rows >= 3 else "1536x1024")
    try:
        output_compression = max(0, min(int(os.getenv("PDD_IMAGE_OUTPUT_COMPRESSION", "90")), 100))
    except ValueError:
        output_compression = 90
    payload = {
        "model": config.image_model,
        "prompt": prompt,
        "size": getattr(config, "request_output_size", "") or os.getenv("PDD_IMAGE_SIZE", default_size),
        "quality": os.getenv("PDD_IMAGE_QUALITY", "medium"),
        "n": 1,
        # GPT Image 2/2.5 always returns base64; response_format is deprecated for these models.
        # output_format keeps the request compatible with the current official API while still
        # allowing older OpenAI-compatible gateways via the explicit legacy switch below.
        "output_format": os.getenv("PDD_IMAGE_OUTPUT_FORMAT", "jpeg"),
        "output_compression": output_compression,
    }
    if os.getenv("PDD_IMAGE_LEGACY_RESPONSE_FORMAT", "0").strip().lower() in {"1", "true", "yes"}:
        payload["response_format"] = "b64_json"
    try:
        # OpenAI-compatible edits endpoint可以把用户照片作为参考图传入。用户若填的是
        # 官方 /images/generations 地址，auto 模式自动切换到同级 /images/edits，避免
        # “提示词写了参考图但请求根本没有图片”这种静默错误。
        reference_mode = _effective_reference_mode(config)
        edit_url = config.image_api_url
        if reference_mode != "generation" and "/images/generations" in edit_url.lower():
            edit_url = edit_url.replace("/images/generations", "/images/edits")
        use_edit = reference_mode == "edit" or "/images/edits" in edit_url.lower()
        if trace is not None:
            trace.update({
                "mode": "api",
                "endpoint_kind": "edits" if use_edit else "generations",
                "reference_mode": "edit" if use_edit else "generation",
                "reference_sent": bool(use_edit),
                "endpoint_path": "/images/edits" if use_edit else "/images/generations",
            })
        refs = [img for img in (reference_images or [input_image]) if img is not None][:12]
        if use_edit:
            import requests
            headers = {"Authorization": f"Bearer {config.image_api_key}"} if config.image_api_key else {}
            send_all = os.getenv("PDD_IMAGE_SEND_ALL_REFERENCES", "1").strip().lower() not in {"0", "false", "no"}
            if not send_all or len(refs) <= 1:
                refs = [input_image]
            if trace is not None:
                trace["reference_count"] = len(refs)
                trace["reference_input_mode"] = "separate" if len(refs) > 1 else "single"
        # Reusing an identical request avoids duplicate image charges after a
        # browser retry.  The key includes the source pixels, prompt, model,
        # output settings and a digest of the provider key, but never stores a
        # raw credential or image in a filename.
        cache_key = _image_cache_key(
            config,
            prompt,
            input_image,
            rows,
            cols,
            refs if use_edit else None,
            "edit" if use_edit else "generation",
            edit_url if use_edit else config.image_api_url,
        ) if _image_cache_enabled() else ""
        cache_path, cache_meta_path = _image_cache_paths(config, cache_key) if cache_key else (None, None)
        if cache_path and cache_path.is_file():
            try:
                cached = _open_image(cache_path.read_bytes())
                cached_meta = {}
                if cache_meta_path and cache_meta_path.is_file():
                    cached_meta = json.loads(cache_meta_path.read_text(encoding="utf-8"))
                if trace is not None:
                    previous = cached_meta.get("trace") if isinstance(cached_meta, dict) else {}
                    if isinstance(previous, dict):
                        for field in ("endpoint_kind", "reference_mode", "reference_sent", "endpoint_path", "reference_count", "reference_input_mode", "output_validation"):
                            if field in previous:
                                trace[field] = previous[field]
                    trace.update({"cache_hit": True, "cache_key": cache_key[:16]})
                # Cache files may predate output validation, so verify every
                # cache hit before publishing it.  An echoed source or an
                # undersized stale raster is deleted and the provider is
                # called again below.
                _validate_generated_sheet(
                    cached,
                    input_image=input_image,
                    reference_images=reference_images,
                    rows=rows,
                    cols=cols,
                    trace=trace,
                )
                _save_jpeg(cached, out_path, max_edge=MAX_LONG_EDGE * 2)
                progress("复用相同输入的本地图片缓存，未重复调用生图 API")
                return cached
            except WorkflowError:
                try:
                    cache_path.unlink(missing_ok=True)
                    if cache_meta_path:
                        cache_meta_path.unlink(missing_ok=True)
                except OSError:
                    pass
                progress("本地图片缓存未通过成品校验，已丢弃并重新请求生图 API")
            except (OSError, ValueError, UnidentifiedImageError, json.JSONDecodeError):
                # A partial cache is disposable; fall through and regenerate.
                try:
                    cache_path.unlink(missing_ok=True)
                    if cache_meta_path:
                        cache_meta_path.unlink(missing_ok=True)
                except OSError:
                    pass
        request_hook = getattr(config, "request_hook", None)
        if callable(request_hook):
            request_hook("request_sent")
        if use_edit:
            # OpenAI-compatible edits endpoints accept repeated `image` parts for
            # multi-reference input. Sending originals separately is more reliable
            # than asking the model to reverse-engineer a tiled board. Gateways that
            # only accept one file can set PDD_IMAGE_SEND_ALL_REFERENCES=0.
            files = []
            for index, ref in enumerate(refs, start=1):
                buf = io.BytesIO()
                ref.save(buf, "PNG")
                files.append(("image", (f"product-reference-{index:02d}.png", buf.getvalue(), "image/png")))
            from safe_http import request_json, private_api_enabled, OutboundError
            try:
                data = request_json("POST", edit_url, key=config.image_api_key, data=payload, files=files, timeout=config.timeout_seconds, max_bytes=48 * 1024 * 1024, allow_private=private_api_enabled())
            except OutboundError as exc:
                raise WorkflowError(str(exc)) from exc
        else:
            data = _http_json(config.image_api_url, payload, config.image_api_key, config.timeout_seconds)
        if trace is not None:
            trace["response_received"] = True
            trace["request_sent"] = True
        if callable(request_hook):
            request_hook("response_received")
    except WorkflowError:
        raise
    except Exception as exc:
        raise WorkflowError(f"AI 图片接口请求失败：{type(exc).__name__}") from exc
    first = (data.get("data") or [{}])[0]
    if not first.get("b64_json") and not first.get("url"):
        raise WorkflowError("AI 图片接口没有返回图片")
    raw = base64.b64decode(first["b64_json"]) if first.get("b64_json") else _download(first.get("url", ""), config.timeout_seconds)
    sheet = _open_image(raw)
    # Validate the provider response before persisting it.  A successful HTTP
    # response alone is not proof that an edit occurred; some gateways echo the
    # first multipart image or return a too-small raster that would become a
    # blurry grid after cropping.
    demo_asset_adapter = bool(getattr(config, "demo_asset_mode", False))
    if demo_asset_adapter:
        # The local demo intentionally serves fixed Skill samples.  They may
        # coincide pixel-for-pixel with a reference used in a UI smoke test,
        # but this channel is already labelled demo_asset and never represents
        # a billable, photo-specific generation.
        if trace is not None:
            trace["output_validation"] = {"status": "demo_asset_skipped"}
    else:
        _validate_generated_sheet(
            sheet,
            input_image=input_image,
            reference_images=reference_images,
            rows=rows,
            cols=cols,
            trace=trace,
        )
    _save_jpeg(sheet, out_path, max_edge=MAX_LONG_EDGE * 2)
    if cache_path:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = cache_path.with_suffix(".tmp")
            shutil.copyfile(out_path, temporary)
            temporary.replace(cache_path)
            if cache_meta_path:
                meta_temp = cache_meta_path.with_suffix(".tmp")
                meta_temp.write_text(
                    json.dumps({"trace": trace or {}, "created_at": time.time()}, ensure_ascii=False),
                    encoding="utf-8",
                )
                meta_temp.replace(cache_meta_path)
            if trace is not None:
                trace.update({"cache_hit": False, "cache_key": cache_key[:16]})
        except OSError:
            # Caching is an optimisation; a read-only disk must not fail a
            # successful paid generation.
            if trace is not None:
                trace["cache_write_error"] = True
    return sheet


def generate_metadata(
    config: WorkflowConfig,
    product_hint: str,
    prompt_pack: dict[str, Any],
    reference_image: Image.Image | None = None,
    progress: Callable[[str], None] | None = None,
    reference_images: list[Image.Image] | None = None,
    creative_density: str = "rich",
) -> dict[str, Any]:
    """优先调用兼容 Chat Completions 的提示词/文本 API，失败时安全降级。"""
    progress = progress or (lambda _msg: None)
    if not config.prompt_api_url:
        progress("未配置提示词 API，使用本地保守字段（未知信息不编造）")
        return {**_validate_metadata({}, product_hint), 'prompt_ai_status':'not_configured', 'prompt_ai_note':'提示词AI未配置，当前为规则方案；商品资料需人工核对'}
    progress("调用提示词 API 生成标题、规格和属性")
    brief_limit = 6 if str(creative_density or "rich").strip().lower() in {"rich", "丰富", "full"} else 3
    system = (
        "你是电商商品资料助手。只根据用户提供的商品照片描述和输入提示生成 JSON；"
        "不确定的已确认字段留空，不要编造品牌、认证、销量、检测、尺寸或功效。标题不超过 60 个中文字符。"
        "除了 specs（照片或卖家输入明确支持的规格）之外，还要返回 spec_suggestions："
        "按品类整理的可勾选规格方向，例如服装可以建议 XS/S/M/L/XL/XXL，但不能把建议当成实际库存。"
        "还要返回 care_instructions（最多8条短句）和 care_status/care_note。护理建议只能依据照片里看见的材质、织法、洗标或包装说明；"
        "看不到洗标时只能返回 candidate，不得编造成分、温度、熨烫或烘干要求。还要返回 identity_evidence（最多12条照片可见证据）、"
        "identity_constraints（最多10条不可凭空添加的结构）、uncertain_features（遮挡/看不清的部位）和 identity_confidence（0到1）。"
        "identity_evidence 只能描述照片中直接看见的内容，不能从商品提示词推断开衫、纽扣、口袋、尺码或材质；"
        f"如果照片没有证明某个结构，请把它放入 uncertain_features 或 identity_constraints。还要返回 selling_points（最多{brief_limit}条、只写照片或卖家资料支持的短卖点）、"
        f"scene_suggestions（最多{brief_limit}条合理的电商使用场景候选，可以提出通勤、咖啡店、卧室、桌面、厨房或周末等创意布景，但不得把虚构环境当成产品事实，也不得添加未经支持的配件或功效）和 ad_headlines（最多{min(5, brief_limit + 1)}条、每条不超过20字的短标题候选）。"
        "另外返回 scene_plan：最多9个自动分镜对象，每个对象严格使用 {role,setting,time_of_day,camera,lighting,mood,action,copy_direction}，用于让生图模型自动选择合适的场景、镜头、光线和动作；"
        "再返回 creative_blocks：最多9个PDP模块，按 hook/pain_point/mechanism/benefit/usage/scene/comparison/proof/faq_cta 的顺序尽量覆盖；"
        "每个模块严格使用 JSON 对象 {role,headline,subheadline,labels,scene,evidence,cta}，headline 不超过20字，subheadline不超过48字，labels最多4条且每条不超过24字，"
        "scene 是可审核的创意布景方向，可以合理扩展电商使用环境；evidence 才只能写照片或卖家资料直接支持的事实。cta不超过8字。文案和场景都是候选，不能当成已确认事实；没有证据的证据字段留空。"
        "另外返回 module_recommendations 数组，根据本商品选择合适的详情模块、数量和理由。每项为 {type,count,reason}，合计不超过20张；"
        "type 仅可为 hook/pain_point/mechanism/benefit/usage/scene/comparison/proof/packaging/closing_cta/carousel_sell/angle/flat_lay/size_chart/sku_preview。"
        "默认不推荐护理和尺码表，只有商家明确选择后才生成这些信息。返回 module_recommendation_reason 总体推荐理由。"
        "返回严格 JSON，字段还包括 module_recommendations/module_recommendation_reason，其他字段为 title/category/brand/attrs/specs/spec_suggestions/spec_direction_note/description/care_instructions/care_status/care_note/identity_evidence/identity_constraints/uncertain_features/identity_confidence/identity_note/selling_points/scene_suggestions/scene_plan/ad_headlines/creative_blocks。"
    )
    user_parts: list[dict[str, Any]] = [{
        "type": "text",
        "text": json.dumps({
            "product_hint": product_hint,
            "visual_prompt": prompt_pack["prompt"],
            "identity_rule": "照片证据优先于商品名称；只报告可见事实，未见结构保持未知。",
        }, ensure_ascii=False),
    }]
    if reference_image is not None and os.getenv("PDD_PROMPT_INCLUDE_IMAGE", "1").strip().lower() not in {"0", "false", "no"}:
        # 视觉模型应逐张看到原始照片；仅发送拼接板会让近景/远景被误读成
        # 同一张裁剪图。保留旧的 reference_image 参数作为单图兼容回退。
        refs = [img for img in (reference_images or [reference_image]) if img is not None][:12]
        user_parts[0]["text"] = f"{user_parts[0]['text']}\n参考照片数量：{len(refs)}；请逐张比对同一商品的可见结构。"
        for index, ref in enumerate(refs, start=1):
            image_buf = io.BytesIO()
            reference_copy = ref.copy()
            reference_copy.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
            reference_copy.save(image_buf, "JPEG", quality=82, optimize=True)
            encoded = base64.b64encode(image_buf.getvalue()).decode("ascii")
            user_parts.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}", "detail": "high"}})
            user_parts.append({"type": "text", "text": f"参考照片 {index}（仅作为身份与可见结构证据）"})
    payload = {
        "model": config.prompt_model,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_parts},
        ],
        "response_format": {"type": "json_object"},
    }
    try:
        data = _http_json(config.prompt_api_url, payload, config.prompt_api_key, config.timeout_seconds)
        content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "")
        if isinstance(content, list):
            content = "".join(
                str(part.get("text", "")) if isinstance(part, dict) else str(part)
                for part in content
            )
        return {**_validate_metadata(_json_from_text(content), product_hint), 'prompt_ai_status':'completed', 'prompt_ai_note':'已调用提示词AI；识别结果和卖点仍需人工核对'}
    except WorkflowError:
        progress("提示词 API 暂不可用，已安全降级到人工确认字段")
        return {**_validate_metadata({}, product_hint), 'prompt_ai_status':'failed_fallback', 'prompt_ai_note':'提示词AI请求失败，已使用保守规则；请核对商品资料与场景再采用'}


def _inject_care_panel_prompt(prompt_pack: dict[str, Any], metadata: dict[str, Any]) -> None:
    """把视觉模型识别出的护理候选传给详情信息图请求。"""
    care_items = _clean_text_items(metadata.get("care_instructions"), max_items=6, max_len=120)
    if not care_items:
        return
    status = metadata.get("care_status") or "candidate"
    label = "护理说明" if status == "confirmed" else "护理建议（候选，请以洗标为准）"
    care_text = "；".join(care_items)
    note = (
        "Add one dedicated care-information infographic panel. Use the exact heading "
        f"「{label}」 and render only these seller-reviewable lines: 「{care_text}」. "
        "Do not add washing temperatures, fabric composition, drying methods or claims that are not in these lines."
    )
    if isinstance(prompt_pack.get("detail"), dict):
        prompt_pack["detail"]["prompt"] = f"{prompt_pack['detail']['prompt']}\n{note}"
    if prompt_pack.get("mode") == "quality":
        hero_prompt = (prompt_pack.get("hero") or {}).get("prompt", "")
        detail_prompt = (prompt_pack.get("detail") or {}).get("prompt", "")
        prompt_pack["prompt"] = f"主图组：{hero_prompt}\n\n详情信息图组：{detail_prompt}"
    else:
        prompt_pack["prompt"] = f"{prompt_pack.get('prompt', '')}\n{note}"


def run_workflow(
    *, image_bytes: bytes | list[bytes], filename: str | list[str], product_hint: str,
    style: str, rows: int, cols: int, project_dir: Path, strategy: str = "",
    model_scene: bool = False, ad_copy: str = "", creative_density: str = "rich",
    creative_brief_override: dict[str, Any] | None = None, detail_modules: Any = None,
    generation_rounds: int = 0, round_strategy: str = "auto", size_chart_enabled: bool = False,
    size_chart_specs: Any = None, provider_overrides: dict[str, str] | None = None,
    progress: Callable[[str], None] | None = None, auto_module_plan: bool = True,
    selected_specs: Any = None, variant_references: Any = None, size_chart_only_selected: bool = True,
    hero_count: int = 6, max_image_requests: int = 0, output_size: str = "",
    resume_job_id: str = "", should_cancel: Callable[[], bool] | None = None,
    checkpoint: Callable[[dict[str, Any]], None] | None = None,
    deadline_seconds: int = 1200,
    approved_proposal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute the versioned image plan and persist all successful batches for recovery."""
    from workflow_engine import execute_workflow
    return execute_workflow(**locals())
