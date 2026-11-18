"""Tests for the spreadsheet tools in `server/app/agent/sheet_tools.py`."""

import os
import sys
from pathlib import Path

import pytest

_SERVER_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "server")
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

pd = pytest.importorskip("pandas")
pytest.importorskip("openpyxl")

from app.agent.dev_tools import DevToolConfig, build_dev_tools  # noqa: E402
from app.agent.sheet_tools import build_sheet_tools  # noqa: E402


class FakeTool:
    def __init__(self, fn, name):
        self.fn = fn
        self.name = name


class FakeSDK:
    def tool(self, fn, *, name=None, effect=None, concurrency_safe=None, retry=None):
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
        "office_tools": False,
        "sheet_tools": True,
        "sheet_max_bytes": 50_000_000,
        "sheet_max_rows": 200_000,
    }
    values.update(overrides)
    return DevToolConfig(**values)


def _tools(workspace: Path, **overrides):
    return {tool.name: tool.fn for tool in build_sheet_tools(FakeSDK(), _config(workspace, **overrides))}


def _write_workbook(path: Path, sheets: dict) -> None:
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        for name, frame in sheets.items():
            frame.to_excel(writer, sheet_name=name, index=False)


def _write_csv(path: Path, frame) -> None:
    frame.to_csv(path, index=False, encoding="utf-8-sig")


SALES = pd.DataFrame(
    {
        "region": ["north", "north", "south", "south"],
        "quarter": ["Q1", "Q2", "Q1", "Q2"],
        "amount": [10, 20, 5, 7],
        "qty": [1, 2, 1, 2],
    }
)


# ---- registration -------------------------------------------------------------


def test_sheet_tools_register_with_the_core_tools(tmp_path):
    tools = {tool.name: tool.fn for tool in build_dev_tools(FakeSDK(), _config(tmp_path))}

    assert {"SheetInspect", "SheetQuery", "SheetTransform", "SheetPivot"} <= set(tools)


def test_sheet_tools_are_optional(tmp_path):
    assert build_sheet_tools(FakeSDK(), _config(tmp_path, sheet_tools=False)) == []


# ---- SheetInspect -------------------------------------------------------------


def test_inspect_lists_sheets_headers_and_a_sample(tmp_path):
    _write_workbook(tmp_path / "book.xlsx", {"Sales": SALES, "Other": pd.DataFrame({"x": [1]})})

    result = _tools(tmp_path)["SheetInspect"]("book.xlsx")

    assert result["format"] == "xlsx"
    assert result["path"] == "book.xlsx"
    sales = result["sheets"][0]
    assert sales["name"] == "Sales"
    assert sales["rows"] == 5
    assert sales["column_names"] == ["region", "quarter", "amount", "qty"]
    assert sales["sample"][0] == ["north", "Q1", 10, 1]
    assert any("Formulas" in note for note in result["notes"])


def test_inspect_can_select_one_sheet(tmp_path):
    _write_workbook(tmp_path / "book.xlsx", {"Sales": SALES, "Other": pd.DataFrame({"x": [1]})})

    result = _tools(tmp_path)["SheetInspect"]("book.xlsx", sheet="Other")

    assert [item["name"] for item in result["sheets"]] == ["Other"]
    assert result["sheets"][0]["column_names"] == ["x"]


def test_inspect_rejects_an_unknown_sheet(tmp_path):
    _write_workbook(tmp_path / "book.xlsx", {"Sales": SALES})

    with pytest.raises(ValueError, match="available: Sales"):
        _tools(tmp_path)["SheetInspect"]("book.xlsx", sheet="Missing")


def test_inspect_reads_csv(tmp_path):
    _write_csv(tmp_path / "sales.csv", SALES.head(2))

    result = _tools(tmp_path)["SheetInspect"]("sales.csv")

    assert result["format"] == "csv"
    assert result["sheets"][0]["name"] == "sales"
    assert result["sheets"][0]["rows"] == 2
    assert result["sheets"][0]["sample"][0] == ["north", "Q1", 10, 1]


# ---- SheetQuery ---------------------------------------------------------------


def test_query_filters_projects_and_sorts(tmp_path):
    _write_workbook(tmp_path / "book.xlsx", {"Sales": SALES})

    result = _tools(tmp_path)["SheetQuery"](
        "book.xlsx",
        filters=[{"column": "amount", "op": "gt", "value": 6}],
        select=["region", "amount"],
        sort_by=["amount"],
        ascending=False,
    )

    assert result["rows_matched"] == 3
    assert result["truncated"] is False
    assert result["columns"] == ["region", "amount"]
    assert result["rows"] == [
        {"region": "north", "amount": 20},
        {"region": "north", "amount": 10},
        {"region": "south", "amount": 7},
    ]


def test_query_coerces_a_text_column_for_a_numeric_filter(tmp_path):
    frame = pd.DataFrame({"name": ["a", "b", "c"], "amount": ["10", "20", "5"]})
    _write_workbook(tmp_path / "book.xlsx", {"Sales": frame})

    result = _tools(tmp_path)["SheetQuery"](
        "book.xlsx",
        filters=[{"column": "amount", "op": "ge", "value": 10}],
        select=["name"],
    )

    assert result["rows"] == [{"name": "a"}, {"name": "b"}]


def test_query_groups_and_aggregates(tmp_path):
    _write_workbook(tmp_path / "book.xlsx", {"Sales": SALES})

    result = _tools(tmp_path)["SheetQuery"](
        "book.xlsx",
        group_by=["region"],
        aggregations=[
            {"column": "amount", "op": "sum", "alias": "total"},
            {"op": "size", "alias": "rows"},
        ],
        sort_by=["total"],
        ascending=False,
    )

    assert result["rows"] == [
        {"region": "north", "total": 30, "rows": 2},
        {"region": "south", "total": 12, "rows": 2},
    ]


def test_query_marks_a_truncated_result(tmp_path):
    _write_workbook(tmp_path / "book.xlsx", {"Sales": SALES})

    result = _tools(tmp_path)["SheetQuery"]("book.xlsx", limit=1)

    assert result["rows_matched"] == 4
    assert result["rows_returned"] == 1
    assert result["truncated"] is True


def test_query_reads_csv_and_rejects_a_foreign_sheet_name(tmp_path):
    _write_csv(tmp_path / "sales.csv", SALES)
    tools = _tools(tmp_path)

    result = tools["SheetQuery"](
        "sales.csv",
        group_by=["region"],
        aggregations=[{"column": "amount", "op": "sum", "alias": "total"}],
    )
    assert result["sheet"] == "sales"
    assert result["rows"] == [
        {"region": "north", "total": 30},
        {"region": "south", "total": 12},
    ]

    with pytest.raises(ValueError, match="CSV files have one sheet"):
        tools["SheetQuery"]("sales.csv", sheet="Data")


def test_query_never_modifies_the_input(tmp_path):
    path = tmp_path / "book.xlsx"
    _write_workbook(path, {"Sales": SALES})
    before = path.read_bytes()

    _tools(tmp_path)["SheetQuery"]("book.xlsx", filters=[{"column": "amount", "op": "gt", "value": 1}])

    assert path.read_bytes() == before


# ---- SheetTransform -----------------------------------------------------------


def test_transform_cleans_into_a_new_file_and_leaves_the_source_alone(tmp_path):
    source = pd.DataFrame({"name": [" a ", "a", "b"], "amount": ["1", None, "3"]})
    _write_workbook(tmp_path / "in.xlsx", {"Data": source})
    before = (tmp_path / "in.xlsx").read_bytes()

    result = _tools(tmp_path)["SheetTransform"](
        "in.xlsx",
        "out.xlsx",
        operations=[
            {"op": "strip", "columns": ["name"]},
            {"op": "drop_duplicates", "subset": ["name"]},
            {"op": "fillna", "columns": ["amount"], "value": 0},
            {"op": "astype", "column": "amount", "type": "int"},
            {"op": "rename", "columns": {"name": "customer"}},
            {"op": "sort", "by": ["amount"], "ascending": False},
        ],
    )

    assert result["input_path"] == "in.xlsx"
    assert result["input_sheet"] == "Data"
    assert result["output_path"] == "out.xlsx"
    assert result["rows_before"] == 3
    assert result["rows_after"] == 2
    assert result["columns"] == ["customer", "amount"]
    assert result["operations_applied"] == 6
    assert (tmp_path / "in.xlsx").read_bytes() == before

    written = pd.read_excel(tmp_path / "out.xlsx")
    assert written["customer"].tolist() == ["b", "a"]
    assert written["amount"].tolist() == [3, 1]


def test_transform_refuses_in_place_and_existing_outputs(tmp_path):
    _write_workbook(tmp_path / "in.xlsx", {"Data": pd.DataFrame({"a": [1]})})
    tools = _tools(tmp_path)

    with pytest.raises(ValueError, match="must differ from input_path"):
        tools["SheetTransform"]("in.xlsx", "in.xlsx")

    (tmp_path / "out.xlsx").write_bytes(b"existing")
    with pytest.raises(ValueError, match="already exists"):
        tools["SheetTransform"]("in.xlsx", "out.xlsx")

    result = tools["SheetTransform"]("in.xlsx", "out.xlsx", overwrite=True)
    assert result["output_path"] == "out.xlsx"


def test_transform_can_write_csv(tmp_path):
    _write_workbook(tmp_path / "in.xlsx", {"Data": SALES})

    result = _tools(tmp_path)["SheetTransform"](
        "in.xlsx",
        "out.csv",
        operations=[{"op": "select", "columns": ["region", "amount"]}],
    )

    assert result["output_path"] == "out.csv"
    written = pd.read_csv(tmp_path / "out.csv")
    assert written.columns.tolist() == ["region", "amount"]
    assert len(written) == 4


def test_transform_reports_coerced_casts(tmp_path):
    _write_workbook(tmp_path / "in.xlsx", {"Data": pd.DataFrame({"amount": ["1", "oops", "3"]})})

    result = _tools(tmp_path)["SheetTransform"](
        "in.xlsx",
        "out.xlsx",
        operations=[{"op": "astype", "column": "amount", "type": "int", "errors": "coerce"}],
    )

    assert any("became null" in warning for warning in result["warnings"])
    written = pd.read_excel(tmp_path / "out.xlsx")
    assert written["amount"].isna().sum() == 1


def test_transform_rejects_an_unknown_operation_and_column(tmp_path):
    _write_workbook(tmp_path / "in.xlsx", {"Data": SALES})
    tools = _tools(tmp_path)

    with pytest.raises(ValueError, match="unknown op"):
        tools["SheetTransform"]("in.xlsx", "out.xlsx", operations=[{"op": "explode"}])

    with pytest.raises(ValueError, match="unknown column"):
        tools["SheetTransform"](
            "in.xlsx",
            "out.xlsx",
            operations=[{"op": "select", "columns": ["missing"]}],
        )


# ---- SheetPivot ---------------------------------------------------------------


def test_pivot_writes_a_new_workbook_with_the_index_preserved(tmp_path):
    _write_workbook(tmp_path / "in.xlsx", {"Sales": SALES})

    result = _tools(tmp_path)["SheetPivot"](
        "in.xlsx",
        "pivot.xlsx",
        index=["region"],
        columns=["quarter"],
        values=["amount"],
        aggfunc="sum",
        output_sheet="Summary",
    )

    assert result["rows"] == 2
    assert result["columns"] == ["Q1", "Q2"]
    assert result["output_path"] == "pivot.xlsx"
    written = pd.read_excel(tmp_path / "pivot.xlsx", sheet_name="Summary", index_col=0)
    assert written.loc["north", "Q1"] == 10
    assert written.loc["north", "Q2"] == 20
    assert written.loc["south", "Q1"] == 5
    assert written.loc["south", "Q2"] == 7


def test_pivot_defaults_values_to_numeric_columns_and_warns_on_duplicate_keys(tmp_path):
    frame = pd.DataFrame(
        {
            "region": ["north", "north"],
            "quarter": ["Q1", "Q1"],
            "amount": [10, 20],
            "qty": [1, 2],
        }
    )
    _write_workbook(tmp_path / "in.xlsx", {"Sales": frame})

    result = _tools(tmp_path)["SheetPivot"](
        "in.xlsx",
        "pivot.xlsx",
        index=["region"],
        columns=["quarter"],
        aggfunc="sum",
    )

    assert any("duplicate" in warning for warning in result["warnings"])
    assert any("defaulted to numeric columns" in warning for warning in result["warnings"])
    written = pd.read_excel(tmp_path / "pivot.xlsx", index_col=0)
    assert written.loc["north", "amount / Q1"] == 30
    assert written.loc["north", "qty / Q1"] == 3


def test_pivot_can_write_csv(tmp_path):
    _write_workbook(tmp_path / "in.xlsx", {"Sales": SALES})

    result = _tools(tmp_path)["SheetPivot"](
        "in.xlsx",
        "pivot.csv",
        index=["region"],
        columns=["quarter"],
        values=["amount"],
    )

    assert result["output_path"] == "pivot.csv"
    written = pd.read_csv(tmp_path / "pivot.csv", index_col=0)
    assert written.loc["north", "Q2"] == 20


# ---- guards -------------------------------------------------------------------


def test_paths_outside_the_workspace_are_rejected(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_workbook(workspace / "book.xlsx", {"Data": SALES})
    outside = tmp_path / "outside.xlsx"
    _write_workbook(outside, {"Data": SALES})
    tools = _tools(workspace)

    with pytest.raises(ValueError, match="outside the workspace"):
        tools["SheetInspect"](str(outside))

    with pytest.raises(ValueError, match="outside the workspace"):
        tools["SheetTransform"]("book.xlsx", str(outside))


def test_unsupported_formats_are_rejected(tmp_path):
    (tmp_path / "legacy.xls").write_bytes(b"not really xls")

    with pytest.raises(ValueError, match="not a supported spreadsheet"):
        _tools(tmp_path)["SheetInspect"]("legacy.xls")


def test_a_missing_file_is_reported(tmp_path):
    with pytest.raises(FileNotFoundError):
        _tools(tmp_path)["SheetInspect"]("missing.xlsx")


def test_the_byte_limit_is_enforced(tmp_path):
    _write_workbook(tmp_path / "book.xlsx", {"Data": SALES})

    with pytest.raises(ValueError, match="the limit is"):
        _tools(tmp_path, sheet_max_bytes=100)["SheetInspect"]("book.xlsx")


def test_the_row_limit_is_enforced(tmp_path):
    _write_workbook(tmp_path / "book.xlsx", {"Data": SALES})

    with pytest.raises(ValueError, match="the limit is 2"):
        _tools(tmp_path, sheet_max_rows=2)["SheetQuery"]("book.xlsx")


def test_invalid_output_sheet_names_are_rejected(tmp_path):
    _write_workbook(tmp_path / "in.xlsx", {"Data": SALES})

    with pytest.raises(ValueError, match="cannot contain"):
        _tools(tmp_path)["SheetTransform"](
            "in.xlsx",
            "out.xlsx",
            output_sheet="bad/name",
        )
