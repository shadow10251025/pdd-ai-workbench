"""Exact merchant-data typesetting. Pagination is owned by core_planner."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont


def render(rows, out_path, style="minimal", columns=None, page_label=""):
    import ai_workflow as w
    clean = [row for row in rows or [] if isinstance(row, dict) and row.get("size")]
    keys = columns or list(dict.fromkeys(str(key) for row in clean for key in row.get("measurements", {}))) or ["实测数据"]
    if len(clean) > 8 or len(keys) > 4:
        raise w.WorkflowError("尺码表需先分页：每页最多8行、4个测量字段，避免数据截断")
    width, margin, cell_width = 750, 38, (750 - 76) / (len(keys) + 1)
    palette = {"luxury": ("#1E1E1E", "#F5F1E8", "#D4AF37", "#313131"), "tech": ("#0B1220", "#F8FAFC", "#4778FF", "#182333"),
               "fresh": ("#FAFFFC", "#29443A", "#8FC7A5", "#EAF7F0")}
    bg, fg, accent, panel = palette.get(style, ("#FFFFFF", "#2D2D2D", "#7A8490", "#F7F5F0"))
    font_path = next((p for p in (Path(r"C:\Windows\Fonts\msyh.ttc"), Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')) if p.is_file()), None)
    def font(size):
        return ImageFont.truetype(str(font_path), size) if font_path else ImageFont.load_default()
    body, title, small = font(22), font(38), font(20)
    probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    def wrap(value, max_width, f):
        lines, line = [], ""
        for char in str(value):
            if char == '\n' or (line and probe.textlength(line + char, font=f) > max_width):
                lines.append(line); line = "" if char == '\n' else char
            else:
                line += char
        return lines + [line]
    data = [["尺码"] + keys]
    for row in clean or [{"size": "待补充", "measurements": {}}]:
        measurements = row.get("measurements") or {}
        data.append([str(row["size"])] + [str(measurements[key]) if key in measurements and measurements[key] not in (None, "") else "待补充" for key in keys])
    laid_out = [[[line for line in wrap(cell, cell_width - 18, body)] for cell in cells] for cells in data]
    heights = [max(68, max(len(cell) for cell in row) * 30 + 26) for row in laid_out]
    footer_lines = wrap("单位按商家实测数据填写；空白项请补充后上架，未自动推测尺寸。", width - margin * 2, small)
    height = max(650, 195 + sum(heights) + 65 + len(footer_lines) * 30)
    if height > 2250:
        raise w.WorkflowError("尺码表文字过长，请缩短字段名称或按更多页面拆分，数据不会被截断")
    image = Image.new("RGB", (width, height), bg); draw = ImageDraw.Draw(image)
    draw.text((margin, 45), "尺码参考 " + page_label, font=title, fill=fg)
    draw.text((margin, 108), "真实测量 · 不推测未填写数值", font=small, fill=accent)
    y = 175
    for row_index, (cells, row_height) in enumerate(zip(laid_out, heights)):
        draw.rectangle((margin, y, width - margin, y + row_height), fill=panel if row_index == 0 else bg, outline=accent, width=1)
        for col, lines in enumerate(cells):
            x = margin + col * cell_width
            if col:
                draw.line((x, y, x, y + row_height), fill=accent, width=1)
            for line_index, line in enumerate(lines):
                draw.text((x + 9, y + 13 + line_index * 30), line, font=body, fill=fg)
        y += row_height
    for index, line in enumerate(footer_lines):
        draw.text((margin, y + 30 + index * 30), line, font=small, fill=fg)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    w._save_jpeg(image, Path(out_path), max_edge=2250, max_bytes=w.PDD_DETAIL_MAX_BYTES)
    return str(out_path)
