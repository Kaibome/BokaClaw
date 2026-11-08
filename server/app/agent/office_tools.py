"""Presentation tools for the in-process agent runtime.

``PptxBuild`` turns a structured slide spec into a ``.pptx`` with python-pptx,
so the model emits slide *content* instead of hundreds of lines of OOXML or
python-pptx calls. Layout, wrapping, font sizing and page numbers are computed
here, and the result reports every page that had to shrink text.

``PptxUpdateSlide`` / ``PptxAddSlide`` / ``PptxDeleteSlide`` patch an existing
deck one slide at a time, so a correction never regenerates the whole file.

Paths are confined to ``AGENT_WORKSPACE_ROOT`` through ``dev_tools._resolve``.
The deck title and resolved theme are stored in a small ``<deck>.theme.json``
sidecar next to the file: python-pptx caps core properties at 255 characters,
and the sidecar keeps later edits rendering with the same look. Editing a deck
whose sidecar is missing falls back to the default theme.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.agent.dev_tools import DevToolConfig, _relative, _resolve

logger = logging.getLogger(__name__)

try:  # python-pptx is a server requirement; tests and stripped images may lack it.
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
    from pptx.oxml.ns import qn
    from pptx.util import Pt
except ImportError:  # pragma: no cover - exercised only when the package is absent
    Presentation = None
    RGBColor = None
    MSO_SHAPE = None
    MSO_ANCHOR = None
    PP_ALIGN = None
    qn = None
    Pt = None

PPTX_AVAILABLE = Presentation is not None

# 16:9 canvas in points (13.333in x 7.5in), the same grid the reference decks use.
SLIDE_WIDTH = 960
SLIDE_HEIGHT = 540
MARGIN_X = 64
CONTENT_WIDTH = SLIDE_WIDTH - 2 * MARGIN_X

LAYOUTS = {"title", "section", "bullets", "two_column", "table", "code", "quote", "end", "blank"}
COLOR_KEYS = ("background", "surface", "accent", "accent2", "text", "muted", "rule", "code_background", "code_text")
FONT_KEYS = ("font", "code_font")

DEFAULT_THEME: Dict[str, str] = {
    "background": "FFFFFF",
    "surface": "F3F4F6",
    "accent": "1F4E79",
    "accent2": "C55A11",
    "text": "1A1A1A",
    "muted": "6B7280",
    "rule": "D1D5DB",
    "code_background": "1E293B",
    "code_text": "E2E8F0",
    "font": "Microsoft YaHei",
    "code_font": "Consolas",
}

_THEME_SUFFIX = ".theme.json"
_HEX_COLOR = re.compile(r"^#?([0-9A-Fa-f]{6})$")
_TOKEN = re.compile(r"[A-Za-z0-9_@#%&+=/.:\-]+|[ \t]+|.", re.S)
_PAGE_SHAPE_NAME = "bc-page-number"
_SECTION_SHAPE_NAME = "bc-section-number"
_MAX_WARNINGS = 24
_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
    "C:/Windows/Fonts/arial.ttf",
)


# ---- theme ------------------------------------------------------------------


def _normalize_hex(value: Any, fallback: str, key: str, warnings: List[str]) -> str:
    match = _HEX_COLOR.match(str(value or ""))
    if match:
        return match.group(1).upper()
    if value and value != fallback:
        warnings.append(f"theme.{key} {value!r} is not RRGGBB; using {fallback}")
    return fallback.upper()


def _clean_theme(raw: Any, warnings: List[str]) -> Dict[str, str]:
    theme = dict(DEFAULT_THEME)
    if raw is None:
        return theme
    if not isinstance(raw, dict):
        warnings.append("theme must be an object; using the default theme")
        return theme

    for key, value in raw.items():
        if key in theme:
            theme[key] = value
        else:
            warnings.append(f"unknown theme key {key!r} ignored")

    for key in COLOR_KEYS:
        theme[key] = _normalize_hex(theme[key], DEFAULT_THEME[key], key, warnings)
    for key in FONT_KEYS:
        theme[key] = str(theme[key] or DEFAULT_THEME[key])
    return theme


def _color(theme: Dict[str, str], key: str) -> Any:
    return RGBColor.from_string(theme[key])


def _mix(color: Any, ratio: float) -> Any:
    """Blend ``color`` towards white by ``ratio`` (0..1)."""
    return RGBColor(
        round(color[0] + (255 - color[0]) * ratio),
        round(color[1] + (255 - color[1]) * ratio),
        round(color[2] + (255 - color[2]) * ratio),
    )


# ---- text measurement -------------------------------------------------------


def _font_file() -> str:
    override = os.environ.get("AGENT_OFFICE_FONT_PATH", "").strip()
    if override and Path(override).is_file():
        return override
    for candidate in _FONT_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return ""


_FONT_CACHE: Dict[tuple, Any] = {}


def _load_font(size: float) -> Any:
    path = _font_file()
    if not path:
        return None
    key = (path, round(size, 2))
    if key not in _FONT_CACHE:
        try:
            from PIL import ImageFont

            _FONT_CACHE[key] = ImageFont.truetype(path, int(round(size)))
        except (ImportError, OSError):
            _FONT_CACHE[key] = None
    return _FONT_CACHE[key]


def _char_em(char: str) -> float:
    if char in " \t":
        return 0.30
    if ord(char) > 0x2E7F:
        return 1.0
    if char.isupper():
        return 0.62
    return 0.55


def text_width(text: str, size: float, bold: bool = False) -> float:
    """Approximate the rendered width of ``text`` in points.

    CJK glyphs are full-width by definition; Latin glyphs use real font metrics
    when a TTF is available and a character-class estimate otherwise.
    """
    font = _load_font(size)
    if font is None:
        total = sum(_char_em(char) for char in text) * size
    else:
        total = 0.0
        for char in text:
            total += size if ord(char) > 0x2E7F else font.getlength(char)
    return total * (1.04 if bold else 1.0)


def line_count(text: str, width: float, size: float, bold: bool = False) -> int:
    if width <= 0 or not text:
        return 1
    lines = 1
    current = 0.0
    for token in _TOKEN.findall(text):
        token_width = text_width(token, size, bold)
        if current > 0 and current + token_width > width:
            lines += 1
            current = 0.0
            if token.isspace():
                continue
        current += token_width
    return lines


def fit_size(
    items: List[tuple],
    width: float,
    height: float,
    sizes: List[float],
    *,
    bold: bool = False,
    line_factor: float = 1.30,
    spacing: float = 0.40,
) -> tuple:
    """Pick the largest size from ``sizes`` whose paragraphs fit ``width`` x ``height``.

    ``items`` holds ``(text, size_ratio, indent)`` tuples. Returns the chosen
    size and the estimated height it needs.
    """
    needed = 0.0
    for size in sizes:
        needed = 0.0
        for text, ratio, indent in items:
            scaled = size * ratio
            needed += line_count(text, width - indent, scaled, bold) * scaled * line_factor + scaled * spacing
        if needed <= height:
            return size, needed
    return sizes[-1], needed


# ---- rendering primitives ---------------------------------------------------


@dataclass
class _Context:
    deck_title: str
    font: str
    code_font: str
    background: Any
    surface: Any
    accent: Any
    accent2: Any
    text: Any
    muted: Any
    rule: Any
    code_background: Any
    code_text: Any
    on_accent: Any
    warnings: List[str]
    page_count: int = 0


def _build_context(theme: Dict[str, str], deck_title: str, warnings: List[str]) -> _Context:
    accent = _color(theme, "accent")
    accent2 = _color(theme, "accent2")
    return _Context(
        deck_title=deck_title,
        font=theme["font"],
        code_font=theme["code_font"],
        background=_color(theme, "background"),
        surface=_color(theme, "surface"),
        accent=accent,
        accent2=accent2,
        text=_color(theme, "text"),
        muted=_color(theme, "muted"),
        rule=_color(theme, "rule"),
        code_background=_color(theme, "code_background"),
        code_text=_color(theme, "code_text"),
        on_accent=_mix(accent, 0.92),
        warnings=warnings,
    )


def _warn(ctx: _Context, message: str) -> None:
    if len(ctx.warnings) < _MAX_WARNINGS:
        ctx.warnings.append(message)


def _set_background(slide: Any, color: Any) -> None:
    fill = slide.background.fill
    fill.solid()
    fill.fore_color.rgb = color


def _add_rect(slide: Any, x: float, y: float, width: float, height: float, color: Any) -> Any:
    shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Pt(x), Pt(y), Pt(width), Pt(height))
    shape.fill.solid()
    shape.fill.fore_color.rgb = color
    shape.line.fill.background()
    shape.shadow.inherit = False
    return shape


def _add_textbox(
    slide: Any,
    x: float,
    y: float,
    width: float,
    height: float,
    *,
    align: Any = None,
    anchor: Any = None,
    wrap: bool = True,
) -> Any:
    box = slide.shapes.add_textbox(Pt(x), Pt(y), Pt(width), Pt(height))
    frame = box.text_frame
    frame.word_wrap = wrap
    frame.vertical_anchor = anchor if anchor is not None else MSO_ANCHOR.TOP
    frame.margin_left = 0
    frame.margin_right = 0
    frame.margin_top = 0
    frame.margin_bottom = 0
    frame.paragraphs[0].alignment = align if align is not None else PP_ALIGN.LEFT
    return box


def _set_bullet(paragraph: Any, indent: float) -> None:
    p_pr = paragraph._p.get_or_add_pPr()
    p_pr.set("marL", str(int(Pt(indent))))
    p_pr.set("indent", str(int(-Pt(indent))))
    p_pr.append(p_pr.makeelement(qn("a:buFont"), {"typeface": "Arial"}))
    p_pr.append(p_pr.makeelement(qn("a:buChar"), {"char": "•"}))


def _write(
    paragraph: Any, text: str, *, size: float, color: Any, ctx: _Context, bold: bool = False, code: bool = False
) -> None:
    paragraph.alignment = PP_ALIGN.LEFT
    paragraph.space_after = Pt(size * 0.40)
    run = paragraph.add_run()
    run.text = text
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.name = ctx.code_font if code else ctx.font
    run.font.color.rgb = color


def _header(ctx: _Context, slide: Any, spec: Dict[str, Any]) -> None:
    eyebrow = str(spec.get("eyebrow") or ctx.deck_title or "")
    if eyebrow:
        box = _add_textbox(slide, MARGIN_X, 28, CONTENT_WIDTH, 18)
        _write(box.text_frame.paragraphs[0], eyebrow, size=10, color=ctx.muted, ctx=ctx)
    _add_rect(slide, MARGIN_X, 52, 36, 3, ctx.accent2)


def _title(ctx: _Context, slide: Any, spec: Dict[str, Any], y: float = 70) -> None:
    text = str(spec.get("title") or "")
    size, _ = fit_size([(text, 1.0, 0)], CONTENT_WIDTH, 92, [32, 29, 26, 23, 20], bold=True)
    if size <= 20 and text:
        _warn(ctx, f"slide {spec['_page']}: title shrank to {size:g}pt to fit two lines")
    box = _add_textbox(slide, MARGIN_X, y, CONTENT_WIDTH, 92)
    _write(box.text_frame.paragraphs[0], text, size=size, color=ctx.text, ctx=ctx, bold=True)


def _footer(ctx: _Context, slide: Any, page: int) -> None:
    _add_rect(slide, MARGIN_X, 498, CONTENT_WIDTH, 0.75, ctx.rule)
    box = _add_textbox(slide, MARGIN_X, 506, 200, 18)
    _write(box.text_frame.paragraphs[0], f"{page} / {ctx.page_count}", size=9, color=ctx.muted, ctx=ctx)
    box.name = _PAGE_SHAPE_NAME


# ---- slide layouts ----------------------------------------------------------


def _render_title(ctx: _Context, slide: Any, spec: Dict[str, Any]) -> None:
    _set_background(slide, ctx.accent)
    _add_rect(slide, MARGIN_X, 168, 72, 5, ctx.accent2)

    title = str(spec.get("title") or "")
    size, _ = fit_size([(title, 1.0, 0)], CONTENT_WIDTH, 130, [44, 40, 36, 32, 28], bold=True)
    box = _add_textbox(slide, MARGIN_X, 196, CONTENT_WIDTH, 150)
    _write(box.text_frame.paragraphs[0], title, size=size, color=RGBColor(0xFF, 0xFF, 0xFF), ctx=ctx, bold=True)

    subtitle = str(spec.get("subtitle") or "")
    if subtitle:
        box = _add_textbox(slide, MARGIN_X, 352, CONTENT_WIDTH, 80)
        _write(box.text_frame.paragraphs[0], subtitle, size=18, color=ctx.on_accent, ctx=ctx)

    footer = str(spec.get("footer") or "")
    if footer:
        box = _add_textbox(slide, MARGIN_X, 468, CONTENT_WIDTH, 20)
        _write(box.text_frame.paragraphs[0], footer, size=11, color=ctx.on_accent, ctx=ctx)


def _render_section(ctx: _Context, slide: Any, spec: Dict[str, Any], page: int) -> None:
    _set_background(slide, ctx.background)
    _add_rect(slide, 0, 0, 14, SLIDE_HEIGHT, ctx.accent)

    box = _add_textbox(slide, MARGIN_X, 150, CONTENT_WIDTH, 76)
    _write(box.text_frame.paragraphs[0], f"{page:02d}", size=60, color=ctx.accent2, ctx=ctx, bold=True)
    box.name = _SECTION_SHAPE_NAME

    title = str(spec.get("title") or "")
    size, _ = fit_size([(title, 1.0, 0)], CONTENT_WIDTH, 90, [36, 32, 28, 24], bold=True)
    box = _add_textbox(slide, MARGIN_X, 246, CONTENT_WIDTH, 96)
    _write(box.text_frame.paragraphs[0], title, size=size, color=ctx.text, ctx=ctx, bold=True)

    subtitle = str(spec.get("subtitle") or "")
    if subtitle:
        box = _add_textbox(slide, MARGIN_X, 348, CONTENT_WIDTH, 60)
        _write(box.text_frame.paragraphs[0], subtitle, size=16, color=ctx.muted, ctx=ctx)


def _bullet_items(bullets: List[Any]) -> List[tuple]:
    items: List[tuple] = []
    for raw in bullets:
        if isinstance(raw, dict):
            text = str(raw.get("text") or "")
            level = int(raw.get("level") or 0)
        else:
            text, level = str(raw), 0
        level = min(max(level, 0), 1)
        items.append((text, 1.0 if level == 0 else 0.88, 0.0 if level == 0 else 26.0))
    return items


def _render_bullets(ctx: _Context, slide: Any, spec: Dict[str, Any], page: int) -> None:
    _set_background(slide, ctx.background)
    _header(ctx, slide, spec)
    _title(ctx, slide, spec)

    items = _bullet_items(spec.get("bullets") or [])
    if not items:
        _warn(ctx, f"slide {page}: bullets layout has no bullets")
        _footer(ctx, slide, page)
        return

    size, _ = fit_size(items, CONTENT_WIDTH, 300, [19, 18, 17, 16, 15, 14, 13, 12, 11])
    if size <= 12:
        _warn(ctx, f"slide {page}: body text shrank to {size:g}pt; consider splitting the page")
    if len(items) > 8:
        _warn(ctx, f"slide {page}: {len(items)} bullets; consider splitting the page")

    box = _add_textbox(slide, MARGIN_X, 176, CONTENT_WIDTH, 300)
    frame = box.text_frame
    first = True
    for text, ratio, _indent in items:
        paragraph = frame.paragraphs[0] if first else frame.add_paragraph()
        first = False
        _write(paragraph, text, size=size * ratio, color=ctx.text, ctx=ctx)
        if ratio >= 1.0:
            _set_bullet(paragraph, 18)
        else:
            paragraph.level = 1
            _set_bullet(paragraph, 22)
    _footer(ctx, slide, page)


def _render_two_column(ctx: _Context, slide: Any, spec: Dict[str, Any], page: int) -> None:
    _set_background(slide, ctx.background)
    _header(ctx, slide, spec)
    _title(ctx, slide, spec)

    columns = spec.get("columns") or []
    if not isinstance(columns, list):
        columns = []
    if len(columns) != 2:
        _warn(ctx, f"slide {page}: two_column expects exactly two columns, got {len(columns)}")
    columns = (columns + [{}, {}])[:2]

    gap = 28
    column_width = (CONTENT_WIDTH - gap) / 2
    for index, column in enumerate(columns):
        if not isinstance(column, dict):
            column = {"heading": str(column)}
        x = MARGIN_X + index * (column_width + gap)
        heading = str(column.get("heading") or "")
        if heading:
            box = _add_textbox(slide, x, 176, column_width, 26)
            _write(box.text_frame.paragraphs[0], heading, size=15, color=ctx.accent, ctx=ctx, bold=True)

        items = _bullet_items(column.get("bullets") or [])
        if not items:
            continue
        size, _ = fit_size(items, column_width, 268, [15, 14, 13, 12, 11, 10])
        if size <= 11:
            _warn(ctx, f"slide {page}: column {index + 1} text shrank to {size:g}pt")
        box = _add_textbox(slide, x, 210, column_width, 268)
        frame = box.text_frame
        first = True
        for text, ratio, _indent in items:
            paragraph = frame.paragraphs[0] if first else frame.add_paragraph()
            first = False
            _write(paragraph, text, size=size * ratio, color=ctx.text, ctx=ctx)
            _set_bullet(paragraph, 16)
    _footer(ctx, slide, page)


def _cell_text(cell: Any, text: str, *, size: float, color: Any, bold: bool, ctx: _Context) -> None:
    frame = cell.text_frame
    frame.word_wrap = True
    paragraph = frame.paragraphs[0]
    paragraph.alignment = PP_ALIGN.LEFT
    run = paragraph.add_run()
    run.text = text
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.name = ctx.font
    run.font.color.rgb = color


def _render_table(ctx: _Context, slide: Any, spec: Dict[str, Any], page: int) -> None:
    _set_background(slide, ctx.background)
    _header(ctx, slide, spec)
    _title(ctx, slide, spec)

    headers = [str(value) for value in (spec.get("headers") or [])]
    rows = [
        [str(value) for value in row] if isinstance(row, (list, tuple)) else [str(row)]
        for row in (spec.get("rows") or [])
    ]
    if not headers and not rows:
        _warn(ctx, f"slide {page}: table layout has no headers or rows")
        _footer(ctx, slide, page)
        return

    column_count = max([len(headers)] + [len(row) for row in rows])
    headers = headers + [""] * (column_count - len(headers))
    rows = [row + [""] * (column_count - len(row)) for row in rows]

    ratios = spec.get("col_widths") or []
    if isinstance(ratios, list) and len(ratios) == column_count:
        try:
            ratios = [max(0.05, float(value)) for value in ratios]
            total = sum(ratios)
            ratios = [value / total for value in ratios]
        except (TypeError, ValueError):
            ratios = []
    if not ratios:
        ratios = [1.0 / column_count] * column_count

    widths = [CONTENT_WIDTH * value for value in ratios]
    available_height = 300
    row_count = len(rows) + 1
    row_height = min(34.0, available_height / max(row_count, 1))
    if row_count > 11:
        _warn(ctx, f"slide {page}: {row_count} table rows; text may overflow")

    size = 11.0
    while size > 7:
        tallest = max(
            line_count(cell, width - 16, size) for width, cell in zip(widths, headers, strict=False)
        )
        body_lines = max(
            [
                line_count(cell, width - 16, size)
                for row in rows
                for width, cell in zip(widths, row, strict=False)
            ]
            or [1]
        )
        if tallest * size * 1.35 <= row_height - 8 and body_lines * size * 1.35 <= row_height - 8:
            break
        size -= 0.5
    if size <= 7.5:
        _warn(ctx, f"slide {page}: table text shrank to {size:g}pt; shorten cells or split the table")

    table_height = row_height * row_count
    if table_height > available_height:
        _warn(ctx, f"slide {page}: table needs more height than the page allows")

    frame = slide.shapes.add_table(row_count, column_count, Pt(MARGIN_X), Pt(176), Pt(CONTENT_WIDTH), Pt(table_height))
    table = frame.table
    table.first_row = True
    table.horz_banding = False
    for index, width in enumerate(widths):
        table.columns[index].width = Pt(width)
    for index in range(row_count):
        table.rows[index].height = Pt(row_height)

    for column, text in enumerate(headers):
        cell = table.cell(0, column)
        cell.fill.solid()
        cell.fill.fore_color.rgb = ctx.accent
        cell.margin_left = Pt(8)
        cell.margin_right = Pt(8)
        cell.margin_top = Pt(2)
        cell.margin_bottom = Pt(2)
        cell.vertical_anchor = MSO_ANCHOR.MIDDLE
        _cell_text(cell, text, size=size, color=RGBColor(0xFF, 0xFF, 0xFF), bold=True, ctx=ctx)

    for row_index, row in enumerate(rows, start=1):
        for column, text in enumerate(row):
            cell = table.cell(row_index, column)
            cell.fill.solid()
            cell.fill.fore_color.rgb = ctx.surface if row_index % 2 == 0 else RGBColor(0xFF, 0xFF, 0xFF)
            cell.margin_left = Pt(8)
            cell.margin_right = Pt(8)
            cell.margin_top = Pt(2)
            cell.margin_bottom = Pt(2)
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE
            _cell_text(cell, text, size=size, color=ctx.text, bold=False, ctx=ctx)
    _footer(ctx, slide, page)


def _render_code(ctx: _Context, slide: Any, spec: Dict[str, Any], page: int) -> None:
    _set_background(slide, ctx.background)
    _header(ctx, slide, spec)
    _title(ctx, slide, spec)

    code = str(spec.get("code") or "")
    lines = code.splitlines() or [""]
    inner_width = CONTENT_WIDTH - 40
    inner_height = 290 - 32
    size = 11.0
    while size > 7:
        widest = max(text_width(line, size) for line in lines)
        if widest <= inner_width and len(lines) * size * 1.35 <= inner_height:
            break
        size -= 0.5
    if size <= 7.5:
        _warn(ctx, f"slide {page}: code shrank to {size:g}pt; shorten the snippet or split it")

    _add_rect(slide, MARGIN_X, 176, CONTENT_WIDTH, 290, ctx.code_background)
    box = _add_textbox(slide, MARGIN_X + 20, 192, inner_width, inner_height, wrap=False)
    frame = box.text_frame
    first = True
    for line in lines:
        paragraph = frame.paragraphs[0] if first else frame.add_paragraph()
        first = False
        _write(paragraph, line, size=size, color=ctx.code_text, ctx=ctx, code=True)
    _footer(ctx, slide, page)


def _render_quote(ctx: _Context, slide: Any, spec: Dict[str, Any], page: int) -> None:
    _set_background(slide, ctx.background)
    _header(ctx, slide, spec)

    quote = str(spec.get("quote") or spec.get("title") or "")
    size, _ = fit_size([(quote, 1.0, 0)], CONTENT_WIDTH - 40, 190, [26, 24, 22, 20, 18])
    if size <= 18:
        _warn(ctx, f"slide {page}: quote shrank to {size:g}pt")
    _add_rect(slide, MARGIN_X, 190, 6, 130, ctx.accent2)
    box = _add_textbox(slide, MARGIN_X + 32, 186, CONTENT_WIDTH - 32, 190)
    _write(box.text_frame.paragraphs[0], quote, size=size, color=ctx.text, ctx=ctx)

    attribution = str(spec.get("attribution") or "")
    if attribution:
        box = _add_textbox(slide, MARGIN_X + 32, 372, CONTENT_WIDTH - 32, 24)
        _write(box.text_frame.paragraphs[0], f"— {attribution}", size=12, color=ctx.muted, ctx=ctx)
    _footer(ctx, slide, page)


def _render_end(ctx: _Context, slide: Any, spec: Dict[str, Any]) -> None:
    _set_background(slide, ctx.accent2)

    title = str(spec.get("title") or "")
    size, _ = fit_size([(title, 1.0, 0)], CONTENT_WIDTH, 130, [44, 40, 36, 30], bold=True)
    box = _add_textbox(slide, MARGIN_X, 210, CONTENT_WIDTH, 140, align=PP_ALIGN.CENTER)
    _write(box.text_frame.paragraphs[0], title, size=size, color=RGBColor(0xFF, 0xFF, 0xFF), ctx=ctx, bold=True)
    box.text_frame.paragraphs[0].alignment = PP_ALIGN.CENTER

    subtitle = str(spec.get("subtitle") or "")
    if subtitle:
        box = _add_textbox(slide, MARGIN_X, 360, CONTENT_WIDTH, 60, align=PP_ALIGN.CENTER)
        _write(box.text_frame.paragraphs[0], subtitle, size=16, color=_mix(ctx.accent2, 0.85), ctx=ctx)
        box.text_frame.paragraphs[0].alignment = PP_ALIGN.CENTER


def _render_blank(ctx: _Context, slide: Any, spec: Dict[str, Any], page: int) -> None:
    _set_background(slide, ctx.background)
    if spec.get("title"):
        _header(ctx, slide, spec)
        _title(ctx, slide, spec)
    _footer(ctx, slide, page)


def _render_slide(ctx: _Context, slide: Any, spec: Dict[str, Any], page: int) -> None:
    layout = spec["layout"]
    if layout == "title":
        _render_title(ctx, slide, spec)
    elif layout == "section":
        _render_section(ctx, slide, spec, page)
    elif layout == "two_column":
        _render_two_column(ctx, slide, spec, page)
    elif layout == "table":
        _render_table(ctx, slide, spec, page)
    elif layout == "code":
        _render_code(ctx, slide, spec, page)
    elif layout == "quote":
        _render_quote(ctx, slide, spec, page)
    elif layout == "end":
        _render_end(ctx, slide, spec)
    elif layout == "blank":
        _render_blank(ctx, slide, spec, page)
    else:
        _render_bullets(ctx, slide, spec, page)

    notes = str(spec.get("notes") or "")
    if notes:
        slide.notes_slide.notes_text_frame.text = notes


# ---- spec normalization -----------------------------------------------------


def _normalize_slide(raw: Any, page: int) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"slide {page} must be an object, got {type(raw).__name__}")
    spec = dict(raw)
    spec["_page"] = page

    layout = str(spec.get("layout") or "bullets").strip().lower()
    spec["layout"] = layout if layout in LAYOUTS else "bullets"
    spec["_unknown_layout"] = layout if layout not in LAYOUTS else ""

    for key in ("bullets", "rows", "columns"):
        if key in spec and not isinstance(spec[key], list):
            raise ValueError(f"slide {page}: {key} must be a list")
    if "headers" in spec and not isinstance(spec["headers"], list):
        raise ValueError(f"slide {page}: headers must be a list")
    if "theme" in spec:
        spec.pop("theme")
    return spec


def _normalize_slides(slides: Any) -> List[Dict[str, Any]]:
    if not isinstance(slides, list) or not slides:
        raise ValueError("slides must be a non-empty list of slide objects")
    return [_normalize_slide(raw, index) for index, raw in enumerate(slides, start=1)]


# ---- presentation helpers ---------------------------------------------------


def _new_presentation() -> Any:
    presentation = Presentation()
    presentation.slide_width = Pt(SLIDE_WIDTH)
    presentation.slide_height = Pt(SLIDE_HEIGHT)
    return presentation


def _blank_layout(presentation: Any) -> Any:
    for layout in presentation.slide_layouts:
        if layout.name.strip().lower() == "blank":
            return layout
    return presentation.slide_layouts[-1]


def _clear_slide(slide: Any) -> None:
    for shape in list(slide.shapes):
        shape._element.getparent().remove(shape._element)
    if slide.has_notes_slide:
        slide.notes_slide.notes_text_frame.text = ""


def _renumber(presentation: Any) -> None:
    total = len(presentation.slides)
    for index, slide in enumerate(presentation.slides, start=1):
        for shape in slide.shapes:
            if not shape.has_text_frame:
                continue
            if shape.name == _PAGE_SHAPE_NAME:
                text = f"{index} / {total}"
            elif shape.name == _SECTION_SHAPE_NAME:
                text = f"{index:02d}"
            else:
                continue
            frame = shape.text_frame
            if frame.paragraphs and frame.paragraphs[0].runs:
                frame.paragraphs[0].runs[0].text = text
            else:
                frame.text = text


def _theme_path(path: Path) -> Path:
    return path.with_name(path.name + _THEME_SUFFIX)


def _store_meta(path: Path, title: str, theme: Dict[str, str]) -> Path:
    meta_path = _theme_path(path)
    meta_path.write_text(
        json.dumps({"version": 1, "title": title, "theme": theme}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return meta_path


def _load_meta(path: Path, warnings: List[str]) -> Dict[str, Any]:
    meta_path = _theme_path(path)
    if not meta_path.is_file():
        warnings.append(f"{meta_path.name} is missing; new pages use the default theme")
        return {"title": "", "theme": {}}
    try:
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        warnings.append(f"{meta_path.name} is unreadable; using the default theme")
        return {"title": "", "theme": {}}
    return payload if isinstance(payload, dict) else {"title": "", "theme": {}}


def _deck_context(path: Path, warnings: List[str]) -> _Context:
    meta = _load_meta(path, warnings)
    theme = _clean_theme(meta.get("theme"), warnings)
    ctx = _build_context(theme, str(meta.get("title") or ""), warnings)
    return ctx


def _open_presentation(path: Path) -> Any:
    try:
        return Presentation(str(path))
    except Exception as exc:  # noqa: BLE001 - python-pptx raises several unrelated types
        raise ValueError(f"{path.name!r} is not a readable .pptx file: {exc}") from exc


def _check_page(index: Any, total: int) -> int:
    try:
        page = int(index)
    except (TypeError, ValueError):
        raise ValueError("index must be a 1-based slide number") from None
    if page < 1 or page > total:
        raise ValueError(f"index {page} is out of range; this deck has {total} slides")
    return page


def _result(
    path: Path,
    config: DevToolConfig,
    slides: int,
    warnings: List[str],
    theme_path: Optional[Path] = None,
) -> Dict[str, Any]:
    payload = {
        "path": _relative(config, path),
        "bytes": path.stat().st_size,
        "slides": slides,
        "warnings": warnings,
    }
    if theme_path is not None:
        payload["theme_file"] = _relative(config, theme_path)
    return payload


# ---- tool registration ------------------------------------------------------


def build_office_tools(sdk: Any, config: DevToolConfig) -> List[Any]:
    """Return the presentation tools, or ``[]`` when disabled or python-pptx is absent."""
    if not getattr(config, "office_tools", False):
        return []
    if not PPTX_AVAILABLE:
        logger.warning("AGENT_OFFICE_TOOLS is enabled but python-pptx is not installed; Pptx tools are off")
        return []

    def PptxBuild(
        file_path: str,
        slides: List[Dict[str, Any]],
        title: str = "",
        theme: Optional[Dict[str, Any]] = None,
        overwrite: bool = False,
    ) -> Dict[str, Any]:
        """Build a PowerPoint deck from a structured slide spec.

        Use this whenever the user asks for a .pptx. Send slide content, not
        OOXML and not python-pptx code: this tool owns layout, text wrapping,
        font sizing and page numbers, and reports which pages had to shrink.
        Fix a page with PptxUpdateSlide instead of rebuilding the whole deck.

        Args:
            file_path: Output path relative to the workspace; must end with .pptx.
            slides: List of slide objects. Each has a "layout" plus its own fields:
                title {title, subtitle, footer}; section {title, subtitle};
                bullets {title, bullets: list of strings or {"text","level"}};
                two_column {title, columns: two {"heading","bullets"}};
                table {title, headers, rows, optional col_widths};
                code {title, code}; quote {quote, attribution};
                end {title, subtitle}; blank {title}. Every slide accepts "notes".
                Unknown layouts fall back to bullets and add a warning.
            title: Deck title shown at the top of content slides.
            theme: Optional overrides: background, surface, accent, accent2, text,
                muted, rule, code_background, code_text, font, code_font. Colors
                are RRGGBB with or without a leading '#'.
            overwrite: Replace an existing file instead of refusing the write.
        """
        path = _resolve(config, file_path, must_exist=False)
        if path.suffix.lower() != ".pptx":
            raise ValueError("file_path must end with .pptx")
        if path.exists() and not overwrite:
            raise ValueError(f"{file_path!r} already exists; pass overwrite=true to replace it")

        specs = _normalize_slides(slides)
        warnings: List[str] = []
        theme_clean = _clean_theme(theme, warnings)
        ctx = _build_context(theme_clean, title, warnings)
        ctx.page_count = len(specs)

        presentation = _new_presentation()
        for page, spec in enumerate(specs, start=1):
            if spec["_unknown_layout"]:
                _warn(ctx, f"slide {page}: unknown layout {spec['_unknown_layout']!r}; rendered as bullets")
            slide = presentation.slides.add_slide(_blank_layout(presentation))
            _render_slide(ctx, slide, spec, page)

        path.parent.mkdir(parents=True, exist_ok=True)
        presentation.save(str(path))
        theme_path = _store_meta(path, title, theme_clean)
        return _result(path, config, len(specs), warnings, theme_path)

    def PptxUpdateSlide(file_path: str, index: int, slide: Dict[str, Any]) -> Dict[str, Any]:
        """Replace one slide of an existing deck, keeping the rest untouched.

        Args:
            file_path: Existing .pptx path relative to the workspace.
            index: 1-based slide number, matching the page numbers in the deck.
            slide: A slide object with the same shape PptxBuild accepts.
        """
        path = _resolve(config, file_path, must_exist=True)
        if not path.is_file():
            raise ValueError(f"{file_path!r} is not a file")
        presentation = _open_presentation(path)
        page = _check_page(index, len(presentation.slides))

        warnings: List[str] = []
        ctx = _deck_context(path, warnings)
        ctx.page_count = len(presentation.slides)
        spec = _normalize_slide(slide, page)
        if spec["_unknown_layout"]:
            _warn(ctx, f"slide {page}: unknown layout {spec['_unknown_layout']!r}; rendered as bullets")

        target = presentation.slides[page - 1]
        _clear_slide(target)
        _render_slide(ctx, target, spec, page)
        _renumber(presentation)
        presentation.save(str(path))
        return {
            "path": _relative(config, path),
            "index": page,
            "slides": len(presentation.slides),
            "warnings": warnings,
        }

    def PptxAddSlide(file_path: str, slide: Dict[str, Any], index: int = 0) -> Dict[str, Any]:
        """Insert a slide into an existing deck.

        Args:
            file_path: Existing .pptx path relative to the workspace.
            slide: A slide object with the same shape PptxBuild accepts.
            index: 1-based position to insert at; 0 appends after the last slide.
        """
        path = _resolve(config, file_path, must_exist=True)
        if not path.is_file():
            raise ValueError(f"{file_path!r} is not a file")
        presentation = _open_presentation(path)

        total = len(presentation.slides)
        position = int(index or 0)
        if position < 0 or position > total + 1:
            raise ValueError(f"index {position} is out of range; insert between 1 and {total + 1}, or pass 0 to append")
        if position == 0:
            position = total + 1

        warnings: List[str] = []
        ctx = _deck_context(path, warnings)
        ctx.page_count = len(presentation.slides)
        spec = _normalize_slide(slide, position)
        if spec["_unknown_layout"]:
            _warn(ctx, f"slide {position}: unknown layout {spec['_unknown_layout']!r}; rendered as bullets")

        new_slide = presentation.slides.add_slide(_blank_layout(presentation))
        _render_slide(ctx, new_slide, spec, position)

        sld_ids = presentation.slides._sldIdLst
        sld_id = list(sld_ids)[-1]
        sld_ids.remove(sld_id)
        sld_ids.insert(position - 1, sld_id)

        _renumber(presentation)
        presentation.save(str(path))
        return {
            "path": _relative(config, path),
            "index": position,
            "slides": len(presentation.slides),
            "warnings": warnings,
        }

    def PptxDeleteSlide(file_path: str, index: int) -> Dict[str, Any]:
        """Delete one slide from an existing deck.

        Args:
            file_path: Existing .pptx path relative to the workspace.
            index: 1-based slide number to delete. The last remaining slide cannot be deleted.
        """
        path = _resolve(config, file_path, must_exist=True)
        if not path.is_file():
            raise ValueError(f"{file_path!r} is not a file")
        presentation = _open_presentation(path)
        page = _check_page(index, len(presentation.slides))
        if len(presentation.slides) == 1:
            raise ValueError("cannot delete the only slide in the deck")

        sld_ids = presentation.slides._sldIdLst
        sld_id = list(sld_ids)[page - 1]
        presentation.part.drop_rel(sld_id.rId)
        sld_ids.remove(sld_id)

        _renumber(presentation)
        presentation.save(str(path))
        return {
            "path": _relative(config, path),
            "deleted": page,
            "slides": len(presentation.slides),
        }

    return [
        sdk.tool(PptxBuild, name="PptxBuild"),
        sdk.tool(PptxUpdateSlide, name="PptxUpdateSlide"),
        sdk.tool(PptxAddSlide, name="PptxAddSlide"),
        sdk.tool(PptxDeleteSlide, name="PptxDeleteSlide"),
    ]
