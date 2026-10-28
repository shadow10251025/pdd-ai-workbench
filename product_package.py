"""Versioned product drafts and shared listing preflight rules.

This module contains no browser automation or provider calls.  A product's
structured specification combination, rather than its display name or row
number, is its identity.  Drafts remain editable even when not ready to list.
"""
from __future__ import annotations

import copy
import hashlib
import json
from decimal import Decimal, InvalidOperation
from itertools import product
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2
MAX_SKUS = 200
_SECRET_KEYS = {"api_key", "image_api_key", "prompt_api_key", "authorization", "provider_overrides", "traceback"}


def strip_package_secrets(value: Any) -> Any:
    from task_store import public_data
    return public_data(value)


def clean_specs(raw: Any) -> dict[str, list[str]]:
    if not isinstance(raw, dict):
        return {}
    specs = {}
    for name, values in raw.items():
        name = str(name).strip()
        if not name or len(name) > 60:
            continue
        if not isinstance(values, (list, tuple)):
            continue
        unique = list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))
        if unique:
            specs[name] = unique
    return specs


def sku_id_for_specs(specs: dict[str, Any]) -> str:
    canonical = json.dumps({str(key): str(value) for key, value in sorted(specs.items())}, ensure_ascii=False, separators=(",", ":"))
    return "sku-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20]


def merge_skus_for_specs(specs: Any, existing: Any = None, max_rows: int = MAX_SKUS) -> list[dict[str, Any]]:
    """Keep prices, stock (including zero), codes, and disabled state by identity.

    A legacy row is adopted by its full name only when that name identifies a
    unique combination; ambiguous legacy names are never guessed.
    """
    specs = clean_specs(specs)
    dimensions = list(specs)
    rows = existing if isinstance(existing, list) else []
    count = 1
    for values in specs.values():
        count *= len(values)
    if count > max_rows:
        raise ValueError(f"规格组合共{count}项，超过{max_rows}项，请减少规格值")
    combos = [dict(zip(dimensions, values)) for values in product(*(specs[name] for name in dimensions))] if dimensions else [{}]
    names = [" ".join(combo[name] for name in dimensions) or "默认" for combo in combos]
    name_counts = {name: names.count(name) for name in set(names)}
    by_id, by_name = {}, {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        structured = row.get("spec_values") if isinstance(row.get("spec_values"), dict) else row.get("specs")
        identity = sku_id_for_specs(structured) if isinstance(structured, dict) else row.get("id") or row.get("sku_id")
        if identity:
            by_id[str(identity)] = row
        by_name.setdefault(str(row.get("name") or "").strip(), []).append(row)
    used_codes = {str(row.get("spec_code") or "") for row in rows if isinstance(row, dict)}
    next_code = max((int(value) for value in used_codes if value.isdigit()), default=0) + 1
    result = []
    for combo, name in zip(combos, names):
        identity = sku_id_for_specs(combo)
        old = by_id.get(identity)
        if old is None and name_counts[name] == 1 and len(by_name.get(name, [])) == 1:
            old = by_name[name][0]
        old = copy.deepcopy(old or {})
        code = str(old.get("spec_code") or "").strip()
        if not code:
            while str(next_code) in used_codes:
                next_code += 1
            code = str(next_code)
            used_codes.add(code)
            next_code += 1
        old.update({
            "id": identity, "sku_id": identity, "name": name, "spec_values": combo,
            "price": old.get("price", 0), "single_price": old.get("single_price", 0),
            "stock": old.get("stock", 0), "enabled": bool(old.get("enabled", True)), "spec_code": code,
        })
        result.append(strip_package_secrets(old))
    return result


def normalize_product_package(raw: Any, existing: Any = None) -> dict[str, Any]:
    """Migrate a legacy saved product or an AI result to a complete draft."""
    raw = strip_package_secrets(raw if isinstance(raw, dict) else {})
    previous = strip_package_secrets(existing if isinstance(existing, dict) else {})
    result = {**previous, **raw}
    metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
    for field, default in (("id", ""), ("job_id", ""), ("title", ""), ("source_url", ""), ("brand", ""), ("description", ""), ("category", "")):
        result[field] = str(result.get(field, metadata.get(field, default)) or "").strip()
    attrs = result.get("attrs", metadata.get("attrs", {}))
    result["attrs"] = {str(key): str(value) for key, value in attrs.items()} if isinstance(attrs, dict) else {}
    result["schema_version"] = SCHEMA_VERSION
    result["specs"] = clean_specs(result.get("specs", result.get("specs_confirmed", {})))
    incoming_rows = raw.get("sku", raw.get("skus", previous.get("sku", previous.get("skus", []))))
    # Legacy custom rows without a declared specification dimension must not
    # collapse into a fabricated default SKU on save/load.
    if result["specs"]:
        result["sku"] = merge_skus_for_specs(result["specs"], incoming_rows)
    else:
        result["sku"] = [dict(row) for row in incoming_rows if isinstance(row, dict)] if isinstance(incoming_rows, list) else []
        for index, row in enumerate(result["sku"]):
            row.setdefault("id", "legacy-" + hashlib.sha256(str(row.get("name") or index).encode("utf-8")).hexdigest()[:20])
    result["skus"] = copy.deepcopy(result["sku"])
    for field in ("main_images", "detail_images", "source_images", "module_outputs", "review_removed_images", "version_history"):
        result[field] = list(result.get(field) or []) if isinstance(result.get(field), list) else []
    result["spec_images"] = dict(result.get("spec_images") or {}) if isinstance(result.get("spec_images"), dict) else {}
    result["review"] = dict(result.get("review") or {}) if isinstance(result.get("review"), dict) else {}
    try:
        result["revision"] = max(0, int(result.get("revision") or 0))
    except (ValueError, TypeError, OverflowError):
        result["revision"] = 0
    return strip_package_secrets(result)


def _number(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return number if number.is_finite() else None


def validate_listing_payload(raw: Any, require_shops: bool = True, project_dir: Path | None = None) -> list[dict[str, str]]:
    """Validate before a browser can be touched.  No network or file writes."""
    if not isinstance(raw, dict):
        return [{"field": "product", "code": "invalid_product", "message": "商品资料格式不正确"}]
    errors: list[dict[str, str]] = []
    def add(field, code, message):
        errors.append({"field": field, "code": code, "message": message})
    title = str(raw.get("title") or "").strip()
    if not title:
        add("title", "required", "请填写商品标题")
    elif len(title) > 60:
        add("title", "too_long", "商品标题最多60个字符")
    main = raw.get("main_images") if isinstance(raw.get("main_images"), list) else []
    details = raw.get("detail_images") if isinstance(raw.get("detail_images"), list) else []
    if not main:
        add("main_images", "required", "至少选择一张主图")
    if len(main) > 10:
        add("main_images", "too_many", "上架主图最多10张")
    if len(details) > 20:
        add("detail_images", "too_many", "上架详情图最多20张")
    spec_images = raw.get('spec_images') if isinstance(raw.get('spec_images'), dict) else {}
    for field, values in (("main_images", main), ("detail_images", details), ('spec_images', list(spec_images.values()))):
        for index, value in enumerate(values):
            if not isinstance(value, str) or not value.strip():
                add(f"{field}.{index}", "invalid_image", "存在无效图片，请重新选择")
                continue
            if project_dir is not None and not value.startswith(("https://", "http://")):
                normalized = value.replace("\\", "/")
                if normalized.startswith("/data/"):
                    path = (Path(project_dir) / normalized.lstrip("/")).resolve()
                else:
                    path = (Path(project_dir) / value).resolve() if not Path(value).is_absolute() else Path(value).resolve()
                allowed = (Path(project_dir) / "data").resolve()
                if allowed not in path.parents or not path.is_file() or path.suffix.lower() not in {'.jpg','.jpeg','.png','.webp','.bmp'}:
                    add(f"{field}.{index}", "missing_image", "图片文件不存在或不属于当前商品目录，请重新生成或加载")
            elif value.startswith(('http://','https://')):
                from safe_http import validate_public_url, OutboundError
                try:
                    validate_public_url(value, resolve_dns=False)
                except OutboundError:
                    add(f'{field}.{index}', 'unsafe_url', '图片地址不允许访问本机或内网')
    rows = raw.get("skus", raw.get("sku", []))
    if not isinstance(rows, list):
        rows = []
    enabled = [(index, row) for index, row in enumerate(rows) if isinstance(row, dict) and row.get("enabled", True)]
    if not enabled:
        add("skus", "required", "至少启用一个SKU")
    if len(rows) > MAX_SKUS:
        add("skus", "too_many", f"SKU最多{MAX_SKUS}项")
    seen_ids, seen_names, seen_codes = set(), set(), set()
    for index, row in enabled:
        prefix = f"skus.{index}"
        label = str(row.get("name") or "").strip()
        if not label:
            add(prefix + ".name", "required", "SKU名称不能为空")
        elif label in seen_names:
            add(prefix + ".name", "duplicate", f"SKU名称重复：{label}")
        seen_names.add(label)
        identity = row.get("id") or row.get("sku_id")
        if identity is not None and not isinstance(identity, str):
            add(prefix + ".id", "invalid_id", "SKU编号格式不正确")
            identity = None
        if identity and identity in seen_ids:
            add(prefix + ".id", "duplicate", "SKU编号重复，请重新整理规格")
        seen_ids.add(identity)
        structured = row.get('spec_values')
        specs = clean_specs(raw.get('specs'))
        if isinstance(structured, dict) and specs and (set(structured) != set(specs) or any(str(structured.get(name)) not in values for name, values in specs.items())):
            add(prefix+'.spec_values', 'invalid_spec', 'SKU组合与当前选择的规格不一致')
        price, single, stock = _number(row.get("price")), _number(row.get("single_price")), _number(row.get("stock"))
        for field, value in (("price", price), ("single_price", single)):
            if value is None or value <= 0:
                add(prefix + "." + field, "invalid_price", f"{label or 'SKU'}的{'拼单价' if field == 'price' else '单买价'}必须是大于0的有效金额")
            elif value.as_tuple().exponent < -2:
                add(prefix + "." + field, "price_precision", "金额最多保留两位小数")
        if price is not None and single is not None and single < price:
            add(prefix + ".single_price", "price_order", f"{label or 'SKU'}的单买价不能低于拼单价")
        if stock is None or stock < 0 or stock != stock.to_integral_value():
            add(prefix + ".stock", "invalid_stock", f"{label or 'SKU'}的库存必须是大于或等于0的整数")
        code = str(row.get("spec_code") or "").strip()
        if code and code in seen_codes:
            add(prefix + ".spec_code", "duplicate", "规格编码重复，请为每个SKU填写不同编码")
        if code:
            seen_codes.add(code)
    if require_shops:
        shops = raw.get("shops")
        if not isinstance(shops, list) or not shops:
            add("shops", "required", "请明确选择目标店铺；检测失败时不会自动挑选店铺")
        elif any(isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535 for port in shops):
            add("shops", "invalid_shop", "目标店铺连接无效，请重新检测店铺")
    return errors


def preflight_listing(raw: Any, require_shops: bool = True, project_dir: Path | None = None) -> dict[str, Any]:
    errors = validate_listing_payload(raw, require_shops=require_shops, project_dir=project_dir)
    rows = raw.get("skus", raw.get("sku", [])) if isinstance(raw, dict) else []
    return {"ok": not errors, "errors": errors, "sku_count": sum(1 for row in rows if isinstance(row, dict) and row.get("enabled", True)) if isinstance(rows, list) else 0}
