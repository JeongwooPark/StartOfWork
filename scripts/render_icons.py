"""Bootstrap Icons SVG → PNG 변환 (개발용)."""

from __future__ import annotations

import io
import re
from pathlib import Path

from PIL import Image
from reportlab.graphics import renderPM
from svglib.svglib import svg2rlg

ICON_DIR = Path(__file__).resolve().parents[1] / "startofwork" / "assets" / "icons"

JOBS = [
    ("check-circle-fill", "#2E7D32", 44, "check-circle-fill"),
    ("x-circle-fill", "#C62828", 44, "x-circle-fill"),
    ("question-circle-fill", "#78909C", 44, "question-circle-fill"),
    ("calendar2-check", "#2E7D32", 18, "calendar2-check"),
    ("calendar-event", "#2E7D32", 18, "calendar-event"),
    ("briefcase-fill", "#2E7D32", 18, "briefcase-fill"),
    ("briefcase-fill", "#90A4AE", 18, "briefcase-fill-muted"),
    ("box-arrow-right", "#2E7D32", 18, "box-arrow-right"),
    ("box-arrow-right", "#90A4AE", 18, "box-arrow-right-muted"),
    ("clock", "#2E7D32", 18, "clock"),
    ("power", "#2E7D32", 18, "power"),
    ("info-circle-fill", "#607D8B", 14, "info-circle-fill"),
    ("shield-lock-fill", "#2E7D32", 78, "shield-lock-fill"),
    ("calendar-check", "#2E7D32", 18, "calendar-check"),
]


def tint_svg(src: Path, color: str) -> Path:
    text = src.read_text(encoding="utf-8")
    text = text.replace('fill="currentColor"', f'fill="{color}"')
    text = re.sub(r'fill="#[0-9A-Fa-f]{3,8}"', f'fill="{color}"', text)
    tmp = src.with_name(f"{src.stem}_{color.lstrip('#')}.svg")
    tmp.write_text(text, encoding="utf-8")
    return tmp


def render(src: Path, size: int, out: Path) -> None:
    drawing = svg2rlg(str(src))
    if drawing is None:
        raise RuntimeError(f"failed to parse {src}")
    scale = size / max(drawing.width, drawing.height)
    drawing.width *= scale
    drawing.height *= scale
    drawing.scale(scale, scale)
    png_bytes = renderPM.drawToString(drawing, fmt="PNG")
    img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    pixels = []
    for r, g, b, a in img.getdata():
        if r > 250 and g > 250 and b > 250:
            pixels.append((r, g, b, 0))
        else:
            pixels.append((r, g, b, a))
    img.putdata(pixels)
    img = img.resize((size, size), Image.Resampling.LANCZOS)
    img.save(out)
    print(f"wrote {out.name} {img.size}")


def main() -> None:
    for name, color, size, stem in JOBS:
        src = ICON_DIR / f"{name}.svg"
        tinted = tint_svg(src, color)
        try:
            render(tinted, size, ICON_DIR / f"{stem}.png")
        finally:
            tinted.unlink(missing_ok=True)

    wm_src = ICON_DIR / "shield-lock-fill.svg"
    wm_tint = tint_svg(wm_src, "#2E7D32")
    try:
        render(wm_tint, 78, ICON_DIR / "shield-lock-fill.png")
        img = Image.open(ICON_DIR / "shield-lock-fill.png").convert("RGBA")
        r, g, b, a = img.split()
        a = a.point(lambda p: int(p * 0.28))
        Image.merge("RGBA", (r, g, b, a)).save(
            ICON_DIR / "shield-lock-fill-watermark.png"
        )
        print("wrote shield-lock-fill-watermark.png")
    finally:
        wm_tint.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
