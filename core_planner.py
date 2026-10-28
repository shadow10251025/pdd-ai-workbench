"""Pure execution planning: stable image slots, bounded native pixels and costs."""
from __future__ import annotations

import hashlib
import math
import os
import re


def build_plan(**options):
    import ai_workflow as w
    hint = options.get("product_hint") or ""
    style = options.get("style") or "minimal"
    rows = max(1, min(int(options.get("rows") or 3), 4))
    cols = max(1, min(int(options.get("cols") or 3), 4))
    hero_count = int(options.get("hero_count", 6))
    if not 1 <= hero_count <= 10:
        raise w.WorkflowError("主图数量应在1到10张之间")
    strategy = "grid" if str(options.get("strategy", "grid")).lower() in {"grid", "economy"} else "quality"
    selected = w._clean_spec_map(options.get("selected_specs"))
    bindings = w._clean_variant_references(options.get("variant_references"), options.get("reference_count", 12))
    chart_enabled = w._flag(options.get("size_chart_enabled"))
    requested = options.get("detail_modules")
    if requested is not None:
        modules, source = w._strict_module_request(requested), "user"
    elif w._flag(options.get("auto_module_plan"), True):
        suggested = options.get("module_recommendations")
        if suggested:
            try:
                modules, source = w._strict_module_request(suggested), "prompt_ai"
            except w.WorkflowError:
                modules, source = [], "category_rules"
        else:
            modules, source = [], "category_rules"
        if not modules:
            modules = w._resize_module_plan(w._default_detail_modules(hint, chart_enabled), min(20, rows * cols))
    else:
        modules, source = [], "user"
    modules = [item for item in modules if item["type"] != "size_chart" or chart_enabled]
    if chart_enabled and requested is None and not any(item["type"] == "size_chart" for item in modules):
        if sum(item["count"] for item in modules) >= 20:
            modules[-1]["count"] -= 1
        modules.append({"type": "size_chart", "label": w.DETAIL_MODULE_CATALOG["size_chart"], "count": 1, "enabled": True, "note": ""})
    reference_indices = list(range(1, min(12, options.get("reference_count", 12)) + 1))
    entries = []
    def entry(module_id, kind, module_type, label, **fields):
        value = {"id": module_id, "kind": kind, "module_type": module_type, "label": label, "path": "", "round": 0,
                 "spec_value": "", "needs_review": True, "render_source": "provider", "reference_indices": reference_indices, "style": style}
        value.update(fields)
        entries.append(value)
        return value
    hero_types = ["white_pack", "angle", "proof", "scene", "flat_lay", "angle", "scene", "proof", "angle", "flat_lay"]
    for index in range(hero_count):
        entry(f"hero-{index + 1:02d}", "hero", hero_types[index], "白底首图" if not index else f"主图候选 {index + 1}")
    chart_rows = w._clean_size_chart_specs(options.get("size_chart_specs"))
    if chart_enabled:
        for row in chart_rows:
            for key,value in row.get('measurements',{}).items():
                if re.search(r'\d',str(value)) and not re.search(r'(?:mm|cm|inch|inches|kg|克|公斤|厘米|毫米|英寸|米|码|%)',str(key)+' '+str(value),re.I):
                    raise w.WorkflowError('尺码实测值请明确单位，例如88 cm，或在列名中写胸围(cm)；本工具不会自动猜单位')
    sizes = w._selected_size_values(selected)
    if w._flag(options.get("size_chart_only_selected"), True) and sizes:
        by_size = {row["size"]: row for row in chart_rows}
        chart_rows = [by_size.get(size, {"size": size, "measurements": {}, "needs_measurement": True}) for size in sizes]
    keys = list(dict.fromkeys(key for row in chart_rows for key in row.get("measurements", {})))
    chart_pages = [{"rows": chart_rows[row:row + 8], "columns": keys[col:col + 4]}
                   for row in range(0, max(1, len(chart_rows)), 8) for col in range(0, max(1, len(keys)), 4)]
    values = w._selected_variant_values(selected)
    counts = {}
    for module in modules:
        kind = module["type"]
        for _ in range(module["count"]):
            counts[kind] = counts.get(kind, 0) + 1
            base_id = f"detail-{kind}-{counts[kind]:02d}"
            pages = chart_pages if kind == "size_chart" else [values[i:i + 8] for i in range(0, max(1, len(values)), 8)] if kind == "sku_preview" else [None]
            for page_index, page in enumerate(pages):
                suffix = f"-p{page_index + 1:02d}" if len(pages) > 1 else ""
                fields = {"render_source": "verified_table" if kind == "size_chart" else "variant_overview" if kind == "sku_preview" else "provider", "note": module.get("note", "")}
                if kind == "size_chart":
                    fields.update(table_page=page, page_label=f"第{page_index + 1}/{len(pages)}页" if len(pages) > 1 else "")
                elif kind == "sku_preview":
                    fields["variant_values"] = page
                entry(base_id + suffix, "detail", kind, module["label"], **fields)
    detail_count = sum(item["kind"] == "detail" for item in entries)
    if detail_count > 20:
        raise w.WorkflowError(f"尺码表或规格总览分页后共{detail_count}张详情图，超过20张，请减少模块或表格内容")
    for value in values:
        if value in bindings:
            entry("spec-" + hashlib.sha256(value.encode()).hexdigest()[:12], "spec", "sku_preview", value + "规格预览", spec_value=value, reference_indices=bindings[value])
    size = str(options.get("output_size") or os.getenv("PDD_IMAGE_SIZE") or "2048x2048").strip().lower()
    match = re.fullmatch(r"(\d{3,5})x(\d{3,5})", size)
    if not match:
        raise w.WorkflowError("请设置服务商支持的原生输出尺寸，例如2048x2048；自动尺寸无法预估裁剪清晰度")
    width, height = map(int, match.groups())
    # crop_grid uses up to 16px on either side. Reserve the full margin in
    # the plan so no output relies on enlargement to pass 480px validation.
    max_cols, max_rows = min(cols, width // 512), min(rows, height // 512)
    if not max_cols or not max_rows or max(width, height) > 8192 or width*height > 40_000_000:
        raise w.WorkflowError("输出尺寸无法提供至少480px的有效原生裁剪，请提高尺寸或减少每批图片")
    capacity = max_cols * max_rows
    rounds = int(options.get("generation_rounds") or (2 if strategy == "quality" else 1))
    if not 1 <= rounds <= 40:
        raise w.WorkflowError("生成轮次应在1到40之间")
    mode = options.get("round_strategy") or "auto"
    warnings, batches = [], []
    if max_cols < cols or max_rows < rows:
        warnings.append(f"按{size}原生尺寸，每批最多{capacity}张，已降低网格密度以保留至少480px有效边长")
    def add(items, wanted, role, limits=None):
        if not items:
            return
        cmax, rmax = limits or (max_cols, max_rows)
        cap = cmax * rmax
        count = min(len(items), max(math.ceil(len(items) / cap), wanted))
        if mode == "separate":
            groups = []
            for item in items:
                if not groups or groups[-1][-1]["module_type"] != item["module_type"] or len(groups[-1]) >= cap:
                    groups.append([])
                groups[-1].append(item)
        elif mode == "balanced":
            groups = [items[index::count] for index in range(count)]
        else:
            groups, offset = [], 0
            for index in range(count):
                take = math.ceil((len(items) - offset) / (count - index))
                groups.append(items[offset:offset + take]); offset += take
        for group in groups:
            n = len(group)
            choices = [(r * c - n, abs(r - c), -min(width // c, height // r), r, c)
                       for r in range(1, rmax + 1) for c in range(1, cmax + 1) if r * c >= n]
            _, _, _, r, c = min(choices)
            number = len(batches) + 1
            for item in group:
                item["round"] = number
            batches.append({"round": number, "role": role, "modules": [item["module_type"] for item in group],
                "module_ids": [item["id"] for item in group], "requested_panels": n, "rows": r, "cols": c, "api_request": True,
                "output_size": size, "native_effective_min_edge": min(width // c, height // r) - 32,
                "cost_note": "本批1次图片请求；费用以服务商账单为准"})
    hero = [item for item in entries if item["kind"] == "hero"]
    details = [item for item in entries if item["kind"] == "detail" and item["render_source"] == "provider"]
    if strategy == "quality" and rounds >= 2:
        add(hero, 1, "hero", (min(3, width // 512), min(2, height // 512)))
        add(details, max(1, rounds - len(batches)), "detail")
    else:
        add(hero + details, rounds, "combined")
    for item in entries:
        if item["kind"] == "spec":
            add([item], 1, "spec", (1, 1))
            batches[-1]["reference_indices"] = item["reference_indices"]
    limit = int(options.get("max_image_requests") or 0)
    if limit < 0:
        raise w.WorkflowError("图片请求预算不能为负数")
    if limit and len(batches) > limit:
        raise w.WorkflowError(f"当前方案需要{len(batches)}次图片请求，超过已确认上限{limit}次；请减少数量、规格专图或调整原生尺寸后重新确认方案")
    missing = [value for value in values if value not in bindings]
    if missing:
        warnings.append("这些规格待补充对应实物参考照片：" + "、".join(missing))
    if len(batches) > rounds:
        warnings.append(f"按清晰度和绑定规格要求，实际需要{len(batches)}批，以本计划请求数为准")
    return {"schema_version": 2, "strategy": strategy, "style": style, "main_count": hero_count, "hero_count": hero_count,
            "detail_count": detail_count, "spec_count": sum(item["kind"] == "spec" for item in entries), "planned_image_requests": len(batches),
            "provider_requests": len(batches), "rounds": len(batches), "generation_rounds": len(batches), "requested_rounds": rounds,
            "round_plan": batches, "module_plan": modules, "detail_module_plan": modules, "module_plan_source": source,
            "warnings": warnings, "module_outputs": entries, "spec_preview_missing": missing, "output_size": size, "max_image_requests": limit}
