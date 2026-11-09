"""Tests for the presentation tools in `server/app/agent/office_tools.py`."""

import os
import sys
from pathlib import Path

import pytest

_SERVER_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "server")
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

pytest.importorskip("pptx")

from pptx import Presentation  # noqa: E402

from app.agent.dev_tools import DevToolConfig, build_dev_tools  # noqa: E402

PAGE_SHAPE = "bc-page-number"


class FakeTool:
    def __init__(self, fn, name):
        self.fn = fn
        self.name = name


class FakeSDK:
    def tool(self, fn, *, name=None, effect=None, concurrency_safe=None):
        return FakeTool(fn, name or fn.__name__)


def _config(workspace: Path, **overrides) -> DevToolConfig:
    values = {
        "workspace": workspace.resolve(),
        "allow_shell": False,
        "command_timeout": 5.0,
        "max_output_bytes": 100_000,
        "max_file_bytes": 2_000_000,
        "web_timeout": 5.0,
        "web_max_bytes": 100_000,
        "web_allow_private": False,
        "office_tools": True,
    }
    values.update(overrides)
    return DevToolConfig(**values)


def _tools(workspace: Path, **overrides):
    return {tool.name: tool.fn for tool in build_dev_tools(FakeSDK(), _config(workspace, **overrides))}


def _deck() -> list:
    return [
        {"layout": "title", "title": "Agent 运行 Span", "subtitle": "采集与评测"},
        {"layout": "section", "title": "背景与目标"},
        {"layout": "bullets", "title": "三个采集点", "bullets": ["入口", "工具", {"text": "子项", "level": 1}]},
        {
            "layout": "two_column",
            "title": "执行器差异",
            "columns": [{"heading": "本地", "bullets": ["同进程"]}, {"heading": "远程", "bullets": ["隔离"]}],
        },
        {"layout": "table", "title": "字段", "headers": ["字段", "类型"], "rows": [["id", "uuid"], ["run_id", "uuid"]]},
        {"layout": "code", "title": "写入", "code": "session.add(span)\nsession.commit()"},
        {"layout": "quote", "quote": "Span 是轨迹的最小单位。", "attribution": "设计结论"},
        {"layout": "end", "title": "谢谢"},
    ]


def _texts(slide) -> str:
    chunks = []
    for shape in slide.shapes:
        if shape.has_text_frame:
            chunks.append("\n".join(paragraph.text for paragraph in shape.text_frame.paragraphs))
        if shape.has_table:
            for row in shape.table.rows:
                chunks.append("\n".join(cell.text for cell in row.cells))
    return "\n".join(chunks)


def _titles(prs) -> list:
    return [prs.slides[index] for index in range(len(prs.slides))]


def _page_numbers(prs) -> list:
    numbers = []
    for slide in prs.slides:
        for shape in slide.shapes:
            if shape.name == PAGE_SHAPE and shape.has_text_frame:
                numbers.append(shape.text_frame.text)
    return numbers


# ---- registration -------------------------------------------------------------


def test_office_tools_register_with_the_core_tools(tmp_path):
    names = set(_tools(tmp_path))

    assert {"PptxBuild", "PptxUpdateSlide", "PptxAddSlide", "PptxDeleteSlide"} <= names


def test_office_tools_are_optional(tmp_path):
    names = set(_tools(tmp_path, office_tools=False))

    assert not {name for name in names if name.startswith("Pptx")}


# ---- PptxBuild ----------------------------------------------------------------


def test_build_renders_every_layout(tmp_path):
    tools = _tools(tmp_path)
    slides = _deck()

    result = tools["PptxBuild"]("deck.pptx", slides, title="BokaClaw")

    assert result["path"] == "deck.pptx"
    assert result["slides"] == len(slides)
    assert result["bytes"] > 0
    assert result["warnings"] == []

    prs = Presentation(str(tmp_path / "deck.pptx"))
    assert len(prs.slides) == len(slides)
    assert prs.slide_width.pt == 960
    assert prs.slide_height.pt == 540
    assert "Agent 运行 Span" in _texts(prs.slides[0])
    assert "背景与目标" in _texts(prs.slides[1])
    assert "三个采集点" in _texts(prs.slides[2])
    assert "本地" in _texts(prs.slides[3])
    assert "run_id" in _texts(prs.slides[4])
    assert "session.commit()" in _texts(prs.slides[5])
    assert "Span 是轨迹的最小单位。" in _texts(prs.slides[6])
    assert "谢谢" in _texts(prs.slides[7])


def test_build_writes_page_numbers_and_skips_them_on_cover_and_end(tmp_path):
    tools = _tools(tmp_path)

    tools["PptxBuild"]("deck.pptx", _deck())

    prs = Presentation(str(tmp_path / "deck.pptx"))
    # 8 slides: content pages are 3..7, cover/section/end carry no counter.
    assert _page_numbers(prs) == ["3 / 8", "4 / 8", "5 / 8", "6 / 8", "7 / 8"]


def test_build_stores_slide_notes(tmp_path):
    tools = _tools(tmp_path)

    tools["PptxBuild"]("deck.pptx", [{"layout": "bullets", "title": "要点", "bullets": ["一"], "notes": "讲稿"}])

    prs = Presentation(str(tmp_path / "deck.pptx"))
    assert prs.slides[0].notes_slide.notes_text_frame.text == "讲稿"


def test_build_refuses_to_overwrite_unless_asked(tmp_path):
    tools = _tools(tmp_path)
    (tmp_path / "deck.pptx").write_bytes(b"existing")

    with pytest.raises(ValueError, match="already exists"):
        tools["PptxBuild"]("deck.pptx", _deck())

    result = tools["PptxBuild"]("deck.pptx", _deck(), overwrite=True)
    assert result["slides"] == 8


def test_build_validates_input(tmp_path):
    tools = _tools(tmp_path)

    with pytest.raises(ValueError, match=r"\.pptx"):
        tools["PptxBuild"]("deck.txt", _deck())
    with pytest.raises(ValueError, match="non-empty list"):
        tools["PptxBuild"]("deck.pptx", [])
    with pytest.raises(ValueError, match="slide 1 must be an object"):
        tools["PptxBuild"]("deck.pptx", ["not-a-slide"])
    with pytest.raises(ValueError, match="bullets must be a list"):
        tools["PptxBuild"]("deck.pptx", [{"layout": "bullets", "bullets": "oops"}])


def test_build_rejects_paths_outside_the_workspace(tmp_path):
    tools = _tools(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-outside.pptx"

    with pytest.raises(ValueError, match="outside the workspace"):
        tools["PptxBuild"](str(outside), _deck())


def test_build_warns_on_unknown_layout_and_bad_theme(tmp_path):
    tools = _tools(tmp_path)

    result = tools["PptxBuild"](
        "deck.pptx",
        [{"layout": "timeline", "title": "时间线", "bullets": ["一"]}],
        theme={"accent": "not-a-color", "mystery": "x"},
    )

    joined = "\n".join(result["warnings"])
    assert "unknown layout 'timeline'" in joined
    assert "accent" in joined
    assert "mystery" in joined


def test_build_warns_when_code_must_shrink(tmp_path):
    tools = _tools(tmp_path)

    result = tools["PptxBuild"](
        "deck.pptx",
        [{"layout": "code", "title": "长行", "code": "x = 1  # " + "y" * 400}],
    )

    assert any("code shrank" in warning for warning in result["warnings"])


def test_build_applies_theme_and_keeps_it_for_later_edits(tmp_path):
    tools = _tools(tmp_path)

    tools["PptxBuild"]("deck.pptx", _deck(), theme={"accent": "123456"})
    tools["PptxUpdateSlide"]("deck.pptx", 2, {"layout": "section", "title": "新章节"})

    prs = Presentation(str(tmp_path / "deck.pptx"))
    fills = set()
    for shape in prs.slides[1].shapes:
        try:
            fills.add(str(shape.fill.fore_color.rgb))
        except (AttributeError, TypeError):
            continue
    assert "123456" in fills
    assert (tmp_path / "deck.pptx.theme.json").is_file()


# ---- PptxUpdateSlide ----------------------------------------------------------


def test_update_replaces_only_the_target_slide(tmp_path):
    tools = _tools(tmp_path)
    tools["PptxBuild"]("deck.pptx", _deck())

    result = tools["PptxUpdateSlide"](
        "deck.pptx", 3, {"layout": "bullets", "title": "改写后的要点", "bullets": ["新内容"]}
    )

    assert result["index"] == 3
    assert result["slides"] == 8
    prs = Presentation(str(tmp_path / "deck.pptx"))
    assert "改写后的要点" in _texts(prs.slides[2])
    assert "三个采集点" not in _texts(prs.slides[2])
    assert "背景与目标" in _texts(prs.slides[1])
    assert "谢谢" in _texts(prs.slides[7])


def test_update_clears_notes_that_the_new_slide_does_not_set(tmp_path):
    tools = _tools(tmp_path)
    tools["PptxBuild"]("deck.pptx", [{"layout": "bullets", "title": "一", "bullets": ["a"], "notes": "旧讲稿"}])

    tools["PptxUpdateSlide"]("deck.pptx", 1, {"layout": "bullets", "title": "二", "bullets": ["b"]})

    prs = Presentation(str(tmp_path / "deck.pptx"))
    assert prs.slides[0].has_notes_slide is False or prs.slides[0].notes_slide.notes_text_frame.text == ""


def test_update_warns_when_the_theme_sidecar_is_missing(tmp_path):
    tools = _tools(tmp_path)
    tools["PptxBuild"]("deck.pptx", [{"layout": "bullets", "title": "一", "bullets": ["a"]}])
    (tmp_path / "deck.pptx.theme.json").unlink()

    result = tools["PptxUpdateSlide"]("deck.pptx", 1, {"layout": "bullets", "title": "二", "bullets": ["b"]})

    assert any("theme.json is missing" in warning for warning in result["warnings"])


def test_update_rejects_out_of_range_and_missing_file(tmp_path):
    tools = _tools(tmp_path)
    tools["PptxBuild"]("deck.pptx", _deck())

    with pytest.raises(ValueError, match="out of range"):
        tools["PptxUpdateSlide"]("deck.pptx", 9, {"layout": "bullets", "title": "x"})
    with pytest.raises(FileNotFoundError):
        tools["PptxUpdateSlide"]("missing.pptx", 1, {"layout": "bullets", "title": "x"})


# ---- PptxAddSlide / PptxDeleteSlide -------------------------------------------


def test_add_slide_offsets_page_numbers(tmp_path):
    tools = _tools(tmp_path)
    tools["PptxBuild"](
        "deck.pptx",
        [
            {"layout": "bullets", "title": "一", "bullets": ["a"]},
            {"layout": "bullets", "title": "二", "bullets": ["b"]},
        ],
    )

    result = tools["PptxAddSlide"]("deck.pptx", {"layout": "bullets", "title": "插入页", "bullets": ["c"]}, index=2)

    assert result["index"] == 2
    assert result["slides"] == 3
    prs = Presentation(str(tmp_path / "deck.pptx"))
    assert "一" in _texts(prs.slides[0])
    assert "插入页" in _texts(prs.slides[1])
    assert "二" in _texts(prs.slides[2])
    assert _page_numbers(prs) == ["1 / 3", "2 / 3", "3 / 3"]


def test_add_slide_updates_section_numbers(tmp_path):
    tools = _tools(tmp_path)
    tools["PptxBuild"](
        "deck.pptx",
        [
            {"layout": "bullets", "title": "一", "bullets": ["a"]},
            {"layout": "section", "title": "第二章"},
        ],
    )

    tools["PptxAddSlide"]("deck.pptx", {"layout": "bullets", "title": "插入", "bullets": ["x"]}, index=1)

    prs = Presentation(str(tmp_path / "deck.pptx"))
    numbers = [
        shape.text_frame.text
        for shape in prs.slides[2].shapes
        if shape.name == "bc-section-number" and shape.has_text_frame
    ]
    assert numbers == ["03"]


def test_add_slide_appends_when_index_is_zero(tmp_path):
    tools = _tools(tmp_path)
    tools["PptxBuild"]("deck.pptx", [{"layout": "bullets", "title": "一", "bullets": ["a"]}])

    result = tools["PptxAddSlide"]("deck.pptx", {"layout": "bullets", "title": "末尾", "bullets": ["b"]})

    assert result["index"] == 2
    prs = Presentation(str(tmp_path / "deck.pptx"))
    assert "末尾" in _texts(prs.slides[-1])


def test_delete_slide_renumbers_and_protects_the_last_slide(tmp_path):
    tools = _tools(tmp_path)
    tools["PptxBuild"](
        "deck.pptx",
        [
            {"layout": "bullets", "title": "一", "bullets": ["a"]},
            {"layout": "bullets", "title": "二", "bullets": ["b"]},
            {"layout": "bullets", "title": "三", "bullets": ["c"]},
        ],
    )

    result = tools["PptxDeleteSlide"]("deck.pptx", 2)

    assert result["deleted"] == 2
    assert result["slides"] == 2
    prs = Presentation(str(tmp_path / "deck.pptx"))
    assert ["一" in _texts(prs.slides[0]), "三" in _texts(prs.slides[1])] == [True, True]
    assert _page_numbers(prs) == ["1 / 2", "2 / 2"]

    tools["PptxDeleteSlide"]("deck.pptx", 1)
    with pytest.raises(ValueError, match="only slide"):
        tools["PptxDeleteSlide"]("deck.pptx", 1)


def test_pptx_tools_cannot_write_into_workspace_skills(tmp_path):
    skills = tmp_path / "skills"
    skills.mkdir()
    tools = _tools(tmp_path, workspace_skills_dir=skills.resolve())

    with pytest.raises(ValueError, match="read-only workspace skills"):
        tools["PptxBuild"]("skills/deck.pptx", _deck())

    assert not (skills / "deck.pptx").exists()
