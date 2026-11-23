"""Spreadsheet tools for the in-process agent runtime.

The MVP exposes five tools:

``SheetInspect``   workbook metadata and a small sample, without loading the file.
``SheetCreate``    create a new workbook from tabular data that has no source file.
``SheetQuery``     read-only filter / aggregate / sort / project over one sheet.
``SheetTransform`` clean a sheet (dedupe, fill, cast, rename, ...) into a **new** file.
``SheetPivot``     build a pivot table from one sheet into a **new** file.

Writes are never in place: ``output_path`` is required and must differ from the
input. Paths are confined to ``AGENT_WORKSPACE_ROOT`` through
``dev_tools._resolve``.

Only ``.xlsx``, ``.xlsm`` and ``.csv`` are read; ``.xlsx`` and ``.csv`` are
written. ``.xls`` and ``.ods`` need extra engines that the server image does not
ship. Formulas are read as their cached values: this module never evaluates
them, and an ``.xlsx`` rewrite does not preserve charts, pivot caches or VBA.
"""

from __future__ import annotations

import csv
import json
import logging
import math
import os
import re
import tempfile
import zipfile
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.agent.dev_tools import DevToolConfig, _relative, _resolve

logger = logging.getLogger(__name__)

try:  # pandas and openpyxl are server requirements; tests may run without them.
    import numpy as np
    import pandas as pd
    from openpyxl import Workbook, load_workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
except ImportError:  # pragma: no cover - exercised only when a package is absent
    np = None
    pd = None
    Workbook = None
    load_workbook = None

SHEET_TOOLS_AVAILABLE = pd is not None and np is not None and load_workbook is not None

READABLE_SUFFIXES = {".xlsx", ".xlsm", ".csv"}
WRITABLE_SUFFIXES = {".xlsx", ".csv"}
_CSV_ENCODINGS = ("utf-8-sig", "gb18030")
_AGGREGATIONS = {"count", "size", "sum", "mean", "median", "min", "max", "std", "nunique"}
_FILTER_OPS = {
    "eq",
    "ne",
    "gt",
    "ge",
    "lt",
    "le",
    "in",
    "not_in",
    "contains",
    "startswith",
    "endswith",
    "between",
    "is_null",
    "not_null",
}
_CAST_TYPES = {"string", "int", "float", "bool", "datetime"}
_MAX_LIMIT = 500
_MAX_FILTERS = 50
_MAX_OPERATIONS = 50
_MAX_WARNINGS = 32
_DEFAULT_SHEET = "Sheet1"
_INVALID_SHEET = re.compile(r"[\\/*?:\[\]]")
_ARCHIVE_EXPANSION_FACTOR = 8
_FORMULA_PREFIXES = ("=", "+", "-", "@")
_MIN_COLUMN_WIDTH = 10
_MAX_COLUMN_WIDTH = 60


def _warn(warnings: List[str], message: str) -> None:
    if len(warnings) < _MAX_WARNINGS:
        warnings.append(message)
    elif len(warnings) == _MAX_WARNINGS:
        warnings.append("further warnings omitted")


# ---- values and paths --------------------------------------------------------


def _jsonable(value: Any) -> Any:
    """Convert a pandas cell into a JSON-safe scalar."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        number = float(value)
        return None if math.isnan(number) or math.isinf(number) else number
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _rows(frame: Any, limit: int) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for row in frame.head(limit).to_dict(orient="records"):
        records.append({str(key): _jsonable(value) for key, value in row.items()})
    return records


def _max_bytes(config: DevToolConfig) -> int:
    return max(1, int(getattr(config, "sheet_max_bytes", 50_000_000)))


def _max_rows(config: DevToolConfig) -> int:
    return max(1, int(getattr(config, "sheet_max_rows", 200_000)))


def _check_archive(path: Path, limit: int) -> None:
    """Reject an xlsx that expands far beyond its on-disk size."""
    try:
        with zipfile.ZipFile(path) as archive:
            total = sum(info.file_size for info in archive.infolist())
    except zipfile.BadZipFile:
        raise ValueError(f"{path.name!r} is not a valid .xlsx/.xlsm archive") from None
    if total > limit * _ARCHIVE_EXPANSION_FACTOR:
        raise ValueError(
            f"{path.name!r} expands to {total} bytes; the limit is "
            f"{limit * _ARCHIVE_EXPANSION_FACTOR} (raise AGENT_SHEET_MAX_BYTES to allow it)"
        )


def _check_input(config: DevToolConfig, file_path: str) -> Tuple[Path, str]:
    path = _resolve(config, file_path, must_exist=True)
    if not path.is_file():
        raise ValueError(f"{file_path!r} is not a file")
    suffix = path.suffix.lower()
    if suffix not in READABLE_SUFFIXES:
        raise ValueError(
            f"{file_path!r} is not a supported spreadsheet; use one of "
            + ", ".join(sorted(READABLE_SUFFIXES))
        )
    size = path.stat().st_size
    if size > _max_bytes(config):
        raise ValueError(
            f"{file_path!r} is {size} bytes; the limit is {_max_bytes(config)} "
            "(raise AGENT_SHEET_MAX_BYTES to allow it)"
        )
    if suffix in {".xlsx", ".xlsm"}:
        _check_archive(path, _max_bytes(config))
    return path, suffix


def _check_output(
    config: DevToolConfig,
    input_path: Path,
    output_path: str,
    overwrite: bool,
) -> Path:
    path = _resolve(config, output_path, must_exist=False, for_write=True)
    if path.suffix.lower() not in WRITABLE_SUFFIXES:
        raise ValueError(
            f"output_path {output_path!r} must end with one of " + ", ".join(sorted(WRITABLE_SUFFIXES))
        )
    if path.resolve() == input_path.resolve():
        raise ValueError("output_path must differ from input_path; in-place editing is not supported")
    if path.exists() and not overwrite:
        raise ValueError(f"{output_path!r} already exists; pass overwrite=true to replace it")
    if path.exists() and not path.is_file():
        raise ValueError(f"{output_path!r} exists and is not a file")
    return path


def _sheet_name(value: str) -> str:
    name = (value or "").strip() or _DEFAULT_SHEET
    if len(name) > 31:
        raise ValueError("sheet names cannot exceed 31 characters")
    if _INVALID_SHEET.search(name):
        raise ValueError("sheet names cannot contain any of \\ / * ? : [ ]")
    return name


# ---- reading -----------------------------------------------------------------


def _normalize_columns(columns: Sequence[Any], warnings: List[str]) -> List[str]:
    seen: Dict[str, int] = {}
    names: List[str] = []
    for index, raw in enumerate(columns):
        name = str(raw).strip() if raw is not None else ""
        if not name or name.lower().startswith("unnamed:"):
            name = f"Column {index + 1}"
        base = name
        if base in seen:
            seen[base] += 1
            name = f"{base}_{seen[base]}"
            _warn(warnings, f"duplicate column {base!r} renamed to {name!r}")
        else:
            seen[base] = 1
        names.append(name)
    return names


def _is_unnamed(column: Any) -> bool:
    text = str(column).strip().lower()
    return not text or text.startswith("unnamed:")


def _clean_frame(frame: Any, config: DevToolConfig, warnings: List[str]) -> Any:
    if frame is None:
        return pd.DataFrame()
    empty = [column for column in frame.columns if _is_unnamed(column) and frame[column].isna().all()]
    if empty:
        frame = frame.drop(columns=empty)
        _warn(warnings, f"dropped {len(empty)} empty unnamed column(s)")
    before = len(frame)
    frame = frame.dropna(how="all")
    if len(frame) < before:
        _warn(warnings, f"dropped {before - len(frame)} empty row(s)")
    frame = frame.reset_index(drop=True)
    frame.columns = _normalize_columns(frame.columns, warnings)
    if len(frame) > _max_rows(config):
        raise ValueError(
            f"sheet has {len(frame)} rows; the limit is {_max_rows(config)} "
            "(raise AGENT_SHEET_MAX_ROWS to allow it)"
        )
    return frame


def _read_csv(path: Path) -> Any:
    last_error: Optional[Exception] = None
    for encoding in _CSV_ENCODINGS:
        try:
            return pd.read_csv(path, encoding=encoding, low_memory=False)
        except UnicodeDecodeError as exc:
            last_error = exc
        except pd.errors.EmptyDataError:
            return pd.DataFrame()
    raise ValueError(f"could not decode {path.name!r}; tried {', '.join(_CSV_ENCODINGS)}") from last_error


def _sheet_names(path: Path) -> List[str]:
    with pd.ExcelFile(path, engine="openpyxl") as book:
        return list(book.sheet_names)


def _select_sheet(names: Sequence[str], sheet: str) -> str:
    if not names:
        raise ValueError("workbook contains no worksheets")
    requested = (sheet or "").strip()
    if not requested:
        return names[0]
    if requested not in names:
        raise ValueError(f"sheet {requested!r} not found; available: {', '.join(names)}")
    return requested


def _read_frame(config: DevToolConfig, path: Path, suffix: str, sheet: str, warnings: List[str]) -> Any:
    try:
        if suffix == ".csv":
            pseudo = path.stem or _DEFAULT_SHEET
            if sheet and sheet != pseudo:
                raise ValueError(f"sheet {sheet!r} not found; CSV files have one sheet named {pseudo!r}")
            frame = _read_csv(path)
            actual = pseudo
        else:
            with pd.ExcelFile(path, engine="openpyxl") as book:
                names = list(book.sheet_names)
                actual = _select_sheet(names, sheet)
                frame = book.parse(actual)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"could not read {path.name!r}: {exc}") from exc
    return _clean_frame(frame, config, warnings), actual


def _write_frame(
    config: DevToolConfig,
    frame: Any,
    path: Path,
    *,
    sheet_name: str = _DEFAULT_SHEET,
    index: bool = False,
) -> None:
    suffix = path.suffix.lower()
    if suffix not in WRITABLE_SUFFIXES:
        raise ValueError(f"cannot write {suffix!r}; use one of " + ", ".join(sorted(WRITABLE_SUFFIXES)))
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".bokaclaw-sheet-", suffix=suffix, dir=str(path.parent))
    os.close(handle)
    temp_path = Path(temporary)
    try:
        if suffix == ".csv":
            frame.to_csv(temp_path, index=index, encoding="utf-8-sig")
        else:
            frame.to_excel(temp_path, index=index, sheet_name=_sheet_name(sheet_name), engine="openpyxl")
        # mkstemp creates 0600. Mirror _atomic_write_text: keep the target's
        # mode on overwrite, otherwise make the file readable like Write does.
        mode = (path.stat().st_mode & 0o7777) if path.exists() else 0o644
        os.chmod(temp_path, mode)
        os.replace(temp_path, path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


# ---- creation ----------------------------------------------------------------


def _check_create_output(config: DevToolConfig, output_path: str, overwrite: bool) -> Path:
    path = _resolve(config, output_path, must_exist=False, for_write=True)
    if path.suffix.lower() not in WRITABLE_SUFFIXES:
        raise ValueError(
            f"output_path {output_path!r} must end with one of " + ", ".join(sorted(WRITABLE_SUFFIXES))
        )
    if path.exists() and not overwrite:
        raise ValueError(f"{output_path!r} already exists; pass overwrite=true to replace it")
    if path.exists() and not path.is_file():
        raise ValueError(f"{output_path!r} exists and is not a file")
    return path


def _cell_value(value: Any) -> Any:
    """Convert one cell to a JSON-compatible scalar that openpyxl can write."""
    if isinstance(value, (dict, list, tuple, set)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return _jsonable(value)


def _looks_like_formula(value: Any) -> bool:
    return isinstance(value, str) and value[:1] in _FORMULA_PREFIXES


def _create_rows(
    columns: Optional[Sequence[str]],
    rows: Any,
    warnings: List[str],
) -> Tuple[List[str], List[List[Any]]]:
    """Normalize user-supplied columns and rows for a new workbook."""
    if rows is None:
        raw_rows: List[Any] = []
    elif isinstance(rows, dict):
        raw_rows = [rows]
    elif isinstance(rows, (list, tuple)):
        raw_rows = list(rows)
    else:
        raise ValueError("rows must be a list of lists or objects")

    if columns:
        names = [str(column).strip() for column in columns]
    else:
        inferred: List[str] = []
        for index, row in enumerate(raw_rows, start=1):
            if not isinstance(row, dict):
                raise ValueError(
                    f"row {index} must be an object when columns is omitted; pass columns for positional rows"
                )
            for key in row:
                name = str(key)
                if name not in inferred:
                    inferred.append(name)
        names = inferred
    if not names or not any(name for name in names):
        raise ValueError("columns must contain at least one column name")
    names = _normalize_columns(names, warnings)

    normalized: List[List[Any]] = []
    for index, row in enumerate(raw_rows, start=1):
        if isinstance(row, dict):
            keyed = {str(key): value for key, value in row.items()}
            values = [keyed.get(name) for name in names]
        elif isinstance(row, (list, tuple)):
            values = list(row)
            if len(values) != len(names):
                raise ValueError(f"row {index} has {len(values)} values but columns has {len(names)}")
        else:
            raise ValueError(f"row {index} must be a list or an object")
        normalized.append([_cell_value(value) for value in values])
    return names, normalized


def _atomic_save_workbook(workbook: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".bokaclaw-sheet-", suffix=".xlsx", dir=str(path.parent))
    os.close(handle)
    temp_path = Path(temporary)
    try:
        workbook.save(temp_path)
        mode = (path.stat().st_mode & 0o7777) if path.exists() else 0o644
        os.chmod(temp_path, mode)
        os.replace(temp_path, path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    finally:
        workbook.close()


def _write_created_xlsx(
    path: Path,
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    sheet_name: str,
    *,
    allow_formulas: bool,
    number_formats: Optional[Dict[str, str]],
    freeze_header: bool,
    warnings: List[str],
) -> None:
    formats = {str(key): str(value) for key, value in (number_formats or {}).items()}
    unknown = [key for key in formats if key not in columns]
    if unknown:
        raise ValueError(f"number_formats has unknown column(s) {unknown}; available: {', '.join(columns)}")

    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = _sheet_name(sheet_name)
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="1F4E79")
    header_alignment = Alignment(horizontal="center", vertical="center")
    worksheet.append(list(columns))
    for cell in worksheet[1]:
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_alignment

    widths = [max(_MIN_COLUMN_WIDTH, min(_MAX_COLUMN_WIDTH, len(str(column)) + 2)) for column in columns]
    formula_cells = 0
    for row in rows:
        worksheet.append(list(row))
        row_index = worksheet.max_row
        for index, value in enumerate(row, start=1):
            cell = worksheet.cell(row=row_index, column=index)
            if _looks_like_formula(value) and not allow_formulas:
                # openpyxl turns a leading "=" into a formula; force the cell
                # back to text so untrusted web data cannot execute on open.
                cell.data_type = "s"
                formula_cells += 1
            width = min(_MAX_COLUMN_WIDTH, max(_MIN_COLUMN_WIDTH, len(str(value)) + 2))
            widths[index - 1] = max(widths[index - 1], width)
        for index, column in enumerate(columns, start=1):
            if column in formats:
                worksheet.cell(row=row_index, column=index).number_format = formats[column]

    if freeze_header:
        worksheet.freeze_panes = "A2"
    for index, width in enumerate(widths, start=1):
        worksheet.column_dimensions[get_column_letter(index)].width = width
    if formula_cells:
        _warn(
            warnings,
            f"wrote {formula_cells} formula-like cell(s) as text; pass allow_formulas=true to keep formulas",
        )
    _atomic_save_workbook(workbook, path)


def _write_created_csv(
    path: Path,
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    *,
    allow_formulas: bool,
    warnings: List[str],
) -> None:
    if not allow_formulas:
        formula_cells = sum(1 for row in rows for value in row if _looks_like_formula(value))
        if formula_cells:
            _warn(
                warnings,
                f"{formula_cells} cell(s) start with a formula character; spreadsheet apps may "
                "interpret them as formulas when the CSV is opened",
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".bokaclaw-sheet-", suffix=".csv", dir=str(path.parent))
    temp_path = Path(temporary)
    try:
        with os.fdopen(handle, "w", newline="", encoding="utf-8-sig") as stream:
            writer = csv.writer(stream)
            writer.writerow(list(columns))
            for row in rows:
                writer.writerow(["" if value is None else value for value in row])
        mode = (path.stat().st_mode & 0o7777) if path.exists() else 0o644
        os.chmod(temp_path, mode)
        os.replace(temp_path, path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


# ---- validation helpers ------------------------------------------------------


def _require_columns(frame: Any, columns: Sequence[str], *, detail: str = "") -> List[str]:
    available = [str(column) for column in frame.columns]
    missing = [column for column in columns if column not in available]
    if missing:
        suffix = f" ({detail})" if detail else ""
        raise ValueError(
            f"unknown column(s) {missing}{suffix}; available: {', '.join(available)}"
        )
    return list(columns)


def _as_list(value: Any, *, field: str) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return [value]


def _clamp_limit(value: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = 50
    return max(1, min(_MAX_LIMIT, number))


# ---- filters -----------------------------------------------------------------


def _coerced_value(series: Any, value: Any) -> Any:
    if value is None:
        return None
    if pd.api.types.is_numeric_dtype(series) and isinstance(value, str):
        text = value.strip()
        try:
            return int(text) if text.lstrip("-").isdigit() else float(text)
        except ValueError:
            return value
    if pd.api.types.is_datetime64_any_dtype(series) and isinstance(value, str):
        parsed = pd.to_datetime(value, errors="coerce")
        return value if pd.isna(parsed) else parsed
    if pd.api.types.is_bool_dtype(series) and isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "1"}:
            return True
        if lowered in {"false", "no", "0"}:
            return False
    return value


def _comparison_series(series: Any, value: Any) -> Any:
    """Use a numeric view when comparing a text column against a number."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not pd.api.types.is_numeric_dtype(series):
            numeric = pd.to_numeric(series, errors="coerce")
            if numeric.notna().any():
                return numeric
    return series


def _apply_filters(frame: Any, filters: Any, warnings: List[str]) -> Any:
    specs = _as_list(filters, field="filters")
    if len(specs) > _MAX_FILTERS:
        raise ValueError(f"at most {_MAX_FILTERS} filters are allowed")
    result = frame
    for index, raw in enumerate(specs, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"filter {index} must be an object")
        column = str(raw.get("column") or "").strip()
        if not column:
            raise ValueError(f"filter {index} needs a column")
        _require_columns(result, [column], detail=f"filter {index}")
        op = str(raw.get("op") or "eq").strip().lower()
        if op not in _FILTER_OPS:
            raise ValueError(f"filter {index}: unknown op {op!r}; use one of {', '.join(sorted(_FILTER_OPS))}")
        series = result[column]
        value = raw.get("value")
        case_sensitive = bool(raw.get("case_sensitive", False))
        text = series.astype("string")

        if op == "is_null":
            mask = series.isna()
        elif op == "not_null":
            mask = series.notna()
        elif op in {"eq", "ne"}:
            if value is None:
                mask = series.isna() if op == "eq" else series.notna()
            else:
                coerced = _coerced_value(series, value)
                mask = series == coerced
                if op == "ne":
                    mask = ~mask
        elif op in {"gt", "ge", "lt", "le"}:
            if value is None:
                raise ValueError(f"filter {index}: op {op!r} needs a value")
            comparable = _comparison_series(series, value)
            coerced = _coerced_value(comparable, value)
            try:
                operations = {
                    "gt": comparable > coerced,
                    "ge": comparable >= coerced,
                    "lt": comparable < coerced,
                    "le": comparable <= coerced,
                }
            except TypeError as exc:
                raise ValueError(
                    f"filter {index}: column {column!r} cannot be compared with {value!r}"
                ) from exc
            mask = operations[op].fillna(False)
        elif op in {"in", "not_in"}:
            values = _as_list(value, field=f"filter {index} value")
            coerced = [_coerced_value(series, item) for item in values]
            mask = series.isin(coerced)
            if op == "not_in":
                mask = ~mask
        elif op in {"contains", "startswith", "endswith"}:
            needle = "" if value is None else str(value)
            if not needle:
                raise ValueError(f"filter {index}: op {op!r} needs a non-empty value")
            if op == "contains":
                mask = text.str.contains(needle, case=case_sensitive, regex=False, na=False)
            elif op == "startswith":
                haystack = text if case_sensitive else text.str.lower()
                prefix = needle if case_sensitive else needle.lower()
                mask = haystack.str.startswith(prefix, na=False)
            else:
                haystack = text if case_sensitive else text.str.lower()
                suffix = needle if case_sensitive else needle.lower()
                mask = haystack.str.endswith(suffix, na=False)
        else:  # between
            bounds = _as_list(value, field=f"filter {index} value")
            if len(bounds) != 2:
                raise ValueError(f"filter {index}: between needs a two-item value")
            comparable = _comparison_series(series, bounds[0])
            low = _coerced_value(comparable, bounds[0])
            high = _coerced_value(comparable, bounds[1])
            inclusive = bool(raw.get("inclusive", True))
            try:
                if inclusive:
                    mask = comparable.between(low, high, inclusive="both")
                else:
                    mask = (comparable > low) & (comparable < high)
            except TypeError as exc:
                raise ValueError(
                    f"filter {index}: column {column!r} cannot be compared with {bounds!r}"
                ) from exc
            mask = mask.fillna(False)
        result = result[mask.fillna(False)]
    if len(result) < 1:
        _warn(warnings, "no rows matched the filters")
    return result


# ---- aggregation -------------------------------------------------------------


def _numeric(series: Any, column: str) -> Any:
    if pd.api.types.is_numeric_dtype(series):
        return series
    numeric = pd.to_numeric(series, errors="coerce")
    if numeric.notna().any():
        return numeric
    raise ValueError(f"aggregation needs numeric data, but column {column!r} has none")


def _aggregate(frame: Any, group_by: Any, aggregations: Any, warnings: List[str]) -> Any:
    specs = _as_list(aggregations, field="aggregations")
    groups = [str(item) for item in _as_list(group_by, field="group_by")]
    if groups:
        _require_columns(frame, groups, detail="group_by")
    if not specs:
        raise ValueError("aggregations must not be empty when aggregating")
    if len(specs) > _MAX_FILTERS:
        raise ValueError(f"at most {_MAX_FILTERS} aggregations are allowed")

    normalized: List[Tuple[str, str, str]] = []
    aliases = set()
    for index, raw in enumerate(specs, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"aggregation {index} must be an object")
        op = str(raw.get("op") or "").strip().lower()
        if op not in _AGGREGATIONS:
            raise ValueError(
                f"aggregation {index}: unknown op {op!r}; use one of {', '.join(sorted(_AGGREGATIONS))}"
            )
        column = str(raw.get("column") or "").strip()
        if op != "size" and not column:
            raise ValueError(f"aggregation {index}: op {op!r} needs a column")
        if column:
            _require_columns(frame, [column], detail=f"aggregation {index}")
        alias = str(raw.get("alias") or "").strip() or (f"{op}_{column}" if column else "row_count")
        if alias in aliases:
            raise ValueError(f"duplicate aggregation alias {alias!r}")
        aliases.add(alias)
        normalized.append((column, op, alias))

    if groups:
        numeric_columns = {
            column for column, op, _ in normalized if op in {"sum", "mean", "median", "std"} and column
        }
        working = frame
        if numeric_columns:
            working = frame.copy()
            for column in numeric_columns:
                working[column] = _numeric(working[column], column)
        grouped = working.groupby(groups, dropna=False, sort=True)
        output = grouped.size().reset_index(name="_size")[groups].copy()
        for column, op, alias in normalized:
            if op == "size":
                output[alias] = grouped.size().to_numpy()
            elif op == "count":
                output[alias] = grouped[column].count().to_numpy()
            elif op == "nunique":
                output[alias] = grouped[column].nunique().to_numpy()
            else:
                output[alias] = grouped[column].agg(op).to_numpy()
        return output

    row: Dict[str, Any] = {}
    for column, op, alias in normalized:
        if op == "size":
            row[alias] = len(frame)
        elif op == "count":
            row[alias] = frame[column].count()
        elif op == "nunique":
            row[alias] = frame[column].nunique()
        elif op in {"sum", "mean", "median", "std"}:
            row[alias] = _numeric(frame[column], column).agg(op)
        else:
            row[alias] = frame[column].agg(op)
    return pd.DataFrame([row])


# ---- transform operations ----------------------------------------------------


def _cast(series: Any, column: str, kind: str, errors: str, warnings: List[str]) -> Any:
    if kind == "string":
        return series.astype("string")
    if kind in {"int", "float"}:
        numeric = pd.to_numeric(series, errors=errors)
        if errors == "coerce":
            failed = int(numeric.isna().sum() - series.isna().sum())
            if failed > 0:
                _warn(warnings, f"column {column!r}: {failed} value(s) became null during {kind} cast")
        if kind == "float":
            return numeric
        fractional = (numeric.dropna() % 1 != 0)
        if fractional.any():
            if errors == "raise":
                raise ValueError(f"column {column!r} contains non-integer values")
            numeric = numeric.mask(fractional.reindex(numeric.index, fill_value=False), pd.NA)
            _warn(warnings, f"column {column!r}: non-integer value(s) became null during int cast")
        return numeric.astype("Int64")
    if kind == "bool":
        mapping = {
            "true": True,
            "yes": True,
            "1": True,
            "false": False,
            "no": False,
            "0": False,
        }

        def convert(value: Any) -> Any:
            if isinstance(value, bool):
                return value
            lowered = str(value).strip().lower()
            if lowered in mapping:
                return mapping[lowered]
            if errors == "raise":
                raise ValueError(f"column {column!r}: {value!r} is not a boolean")
            return pd.NA

        return series.map(convert).astype("boolean")
    if kind == "datetime":
        return pd.to_datetime(series, errors=errors)
    raise ValueError(f"unsupported astype {kind!r}; use one of {', '.join(sorted(_CAST_TYPES))}")


def _string_columns(frame: Any, columns: Any) -> List[str]:
    selected = [str(item) for item in _as_list(columns, field="columns")]
    if selected:
        return _require_columns(frame, selected)
    return [
        str(column)
        for column in frame.columns
        if pd.api.types.is_object_dtype(frame[column]) or pd.api.types.is_string_dtype(frame[column])
    ]


def _apply_operation(frame: Any, raw: Any, index: int, warnings: List[str]) -> Any:
    if not isinstance(raw, dict):
        raise ValueError(f"operation {index} must be an object")
    op = str(raw.get("op") or "").strip().lower()
    if not op:
        raise ValueError(f"operation {index} needs an op")

    if op == "select":
        columns = [str(item) for item in _as_list(raw.get("columns"), field="columns")]
        if not columns:
            raise ValueError(f"operation {index}: select needs columns")
        return frame[_require_columns(frame, columns, detail=f"operation {index}")]

    if op == "drop_columns":
        columns = [str(item) for item in _as_list(raw.get("columns"), field="columns")]
        if not columns:
            raise ValueError(f"operation {index}: drop_columns needs columns")
        _require_columns(frame, columns, detail=f"operation {index}")
        return frame.drop(columns=columns)

    if op == "rename":
        mapping = raw.get("columns")
        if not isinstance(mapping, dict) or not mapping:
            raise ValueError(f"operation {index}: rename needs a columns mapping")
        mapping = {str(old): str(new).strip() for old, new in mapping.items()}
        _require_columns(frame, list(mapping), detail=f"operation {index}")
        if any(not new for new in mapping.values()):
            raise ValueError(f"operation {index}: renamed columns must not be empty")
        collisions = set(mapping.values()) & (set(str(item) for item in frame.columns) - set(mapping))
        if collisions or len(set(mapping.values())) != len(mapping):
            raise ValueError(f"operation {index}: rename would create duplicate columns {sorted(collisions)}")
        return frame.rename(columns=mapping)

    if op == "dropna":
        subset = [str(item) for item in _as_list(raw.get("subset"), field="subset")]
        if subset:
            _require_columns(frame, subset, detail=f"operation {index}")
        how = str(raw.get("how") or "any").lower()
        if how not in {"any", "all"}:
            raise ValueError(f"operation {index}: how must be 'any' or 'all'")
        thresh = raw.get("thresh")
        return frame.dropna(subset=subset or None, how=how, thresh=None if thresh is None else int(thresh))

    if op == "fillna":
        subset = [str(item) for item in _as_list(raw.get("columns"), field="columns")]
        if subset:
            _require_columns(frame, subset, detail=f"operation {index}")
        has_value = "value" in raw
        method = raw.get("method")
        if has_value == (method is not None):
            raise ValueError(f"operation {index}: fillna needs exactly one of value or method")
        if method is not None:
            method = str(method).lower()
            if method not in {"ffill", "bfill"}:
                raise ValueError(f"operation {index}: method must be 'ffill' or 'bfill'")
            target = frame[subset] if subset else frame
            filled = target.ffill() if method == "ffill" else target.bfill()
            result = frame.copy()
            result[subset or list(frame.columns)] = filled
            return result
        result = frame.copy()
        if subset:
            result[subset] = result[subset].fillna(raw["value"])
        else:
            result = result.fillna(raw["value"])
        return result

    if op == "drop_duplicates":
        subset = [str(item) for item in _as_list(raw.get("subset"), field="subset")]
        if subset:
            _require_columns(frame, subset, detail=f"operation {index}")
        keep_raw = raw.get("keep", "first")
        if keep_raw is False or str(keep_raw).lower() == "false":
            keep: Any = False
        else:
            keep = str(keep_raw or "first").lower()
            if keep not in {"first", "last"}:
                raise ValueError(f"operation {index}: keep must be 'first', 'last' or false")
        return frame.drop_duplicates(subset=subset or None, keep=keep)

    if op == "astype":
        column = str(raw.get("column") or "").strip()
        if not column:
            raise ValueError(f"operation {index}: astype needs a column")
        _require_columns(frame, [column], detail=f"operation {index}")
        kind = str(raw.get("type") or "").strip().lower()
        errors = str(raw.get("errors") or "raise").strip().lower()
        if errors not in {"raise", "coerce"}:
            raise ValueError(f"operation {index}: errors must be 'raise' or 'coerce'")
        result = frame.copy()
        result[column] = _cast(result[column], column, kind, errors, warnings)
        return result

    if op in {"strip", "lower", "upper"}:
        columns = _string_columns(frame, raw.get("columns"))
        if not columns:
            _warn(warnings, f"operation {index}: no string columns to {op}")
            return frame
        result = frame.copy()
        for column in columns:
            text = result[column].astype("string")
            if op == "strip":
                text = text.str.strip()
                if bool(raw.get("empty_to_null", False)):
                    text = text.replace("", pd.NA)
            elif op == "lower":
                text = text.str.lower()
            else:
                text = text.str.upper()
            result[column] = text
        return result

    if op == "replace":
        column = str(raw.get("column") or "").strip()
        if not column:
            raise ValueError(f"operation {index}: replace needs a column")
        _require_columns(frame, [column], detail=f"operation {index}")
        if "old" not in raw or "new" not in raw:
            raise ValueError(f"operation {index}: replace needs old and new")
        result = frame.copy()
        result[column] = result[column].replace(raw["old"], raw["new"])
        return result

    if op == "filter":
        return _apply_filters(frame, raw.get("filters"), warnings)

    if op == "sort":
        columns = [str(item) for item in _as_list(raw.get("by"), field="by")]
        if not columns:
            raise ValueError(f"operation {index}: sort needs by")
        _require_columns(frame, columns, detail=f"operation {index}")
        ascending = bool(raw.get("ascending", True))
        return frame.sort_values(by=columns, ascending=ascending, kind="stable", na_position="last")

    if op == "limit":
        rows = int(raw.get("rows") or 0)
        if rows < 1:
            raise ValueError(f"operation {index}: limit needs rows >= 1")
        return frame.tail(rows) if bool(raw.get("from_end", False)) else frame.head(rows)

    if op == "reset_index":
        return frame.reset_index(drop=True)

    raise ValueError(
        f"operation {index}: unknown op {op!r}; use select, drop_columns, rename, dropna, fillna, "
        "drop_duplicates, astype, strip, lower, upper, replace, filter, sort, limit or reset_index"
    )


def _sort_and_select(
    frame: Any,
    sort_by: Any,
    ascending: bool,
    select: Any,
) -> Any:
    columns = [str(item) for item in _as_list(sort_by, field="sort_by")]
    if columns:
        _require_columns(frame, columns, detail="sort_by")
        frame = frame.sort_values(by=columns, ascending=ascending, kind="stable", na_position="last")
    projection = [str(item) for item in _as_list(select, field="select")]
    if projection:
        frame = frame[_require_columns(frame, projection, detail="select")]
    return frame


# ---- tool registration -------------------------------------------------------


def build_sheet_tools(sdk: Any, config: DevToolConfig) -> List[Any]:
    """Return the spreadsheet tools, or ``[]`` when disabled or pandas is absent."""
    if not getattr(config, "sheet_tools", False):
        return []
    if not SHEET_TOOLS_AVAILABLE:
        logger.warning("AGENT_SHEET_TOOLS is enabled but pandas/openpyxl is not installed; Sheet tools are off")
        return []

    def SheetInspect(
        file_path: str,
        sheet: str = "",
        max_rows: int = 5,
        max_columns: int = 30,
    ) -> Dict[str, Any]:
        """Inspect a workbook without loading every row.

        Use this first when you do not know the sheet names, headers or size.
        Supports .xlsx, .xlsm and .csv. Paths must stay inside the workspace.

        Args:
            file_path: Workbook or CSV path; absolute paths inside the workspace are accepted.
            sheet: Optional worksheet name; defaults to the first sheet. Ignored for CSV.
            max_rows: Sample rows per sheet, 1-50.
            max_columns: Sampled columns per sheet, 1-200.
        """
        path, suffix = _check_input(config, file_path)
        sample_rows = max(1, min(50, int(max_rows)))
        sample_columns = max(1, min(200, int(max_columns)))
        warnings: List[str] = []
        sheets: List[Dict[str, Any]] = []

        if suffix == ".csv":
            frame = _clean_frame(_read_csv(path), config, warnings)
            sample = [
                [_jsonable(value) for value in row]
                for row in frame.head(sample_rows).itertuples(index=False, name=None)
            ]
            names = list(frame.columns)
            sheets.append(
                {
                    "name": path.stem or _DEFAULT_SHEET,
                    "rows": len(frame),
                    "columns": len(names),
                    "column_names": names,
                    "sample": sample,
                }
            )
        else:
            try:
                workbook = load_workbook(path, read_only=True, data_only=True)
            except Exception as exc:
                raise ValueError(f"could not open {path.name!r}: {exc}") from exc
            try:
                names = list(workbook.sheetnames)
                target = _select_sheet(names, sheet)
                for name in [target]:
                    worksheet = workbook[name]
                    raw_rows = [
                        [_jsonable(value) for value in row]
                        for row in worksheet.iter_rows(
                            min_row=1, max_row=sample_rows + 1, max_col=sample_columns, values_only=True
                        )
                    ]
                    width = 0
                    for row in raw_rows:
                        for position, value in enumerate(row):
                            if value not in (None, ""):
                                width = max(width, position + 1)
                    header = _normalize_columns(raw_rows[0][:width], warnings) if raw_rows else []
                    rows = [row[:width] for row in raw_rows[1:]]
                    sheets.append(
                        {
                            "name": name,
                            "rows": int(worksheet.max_row or 0),
                            "columns": width,
                            "column_names": header,
                            "sample": rows,
                        }
                    )
            finally:
                workbook.close()

        return {
            "path": _relative(config, path),
            "format": suffix.lstrip("."),
            "sheets": sheets,
            "warnings": warnings,
            "notes": [
                "Formulas are read as cached values; this tool does not recalculate them.",
                "Only .xlsx, .xlsm and .csv are supported; .xls and .ods are not.",
            ],
        }

    def SheetCreate(
        output_path: str,
        columns: Optional[List[str]] = None,
        rows: Optional[List[Any]] = None,
        sheet: str = "",
        overwrite: bool = False,
        allow_formulas: bool = False,
        number_formats: Optional[Dict[str, str]] = None,
        freeze_header: bool = True,
    ) -> Dict[str, Any]:
        """Create a new workbook from tabular data; no source file is required.

        Use this when the data was gathered elsewhere (for example from web
        research) and no spreadsheet exists yet. For cleaning or reshaping an
        existing file, use SheetTransform instead; for a pivot, use SheetPivot.

        Args:
            output_path: New .xlsx or .csv path inside the workspace.
            columns: Ordered column names. Required for positional rows; inferred
                from the object keys when rows are objects.
            rows: Data rows. Each row is a list matching columns, or an object
                keyed by column name. Omit to create a header-only workbook.
            sheet: Worksheet name for .xlsx output; ignored for CSV.
            overwrite: Replace an existing output file instead of refusing.
            allow_formulas: Keep strings that start with =, +, - or @ as formulas.
                Off by default so untrusted data is written as text.
            number_formats: Optional {column: Excel number format} for .xlsx, for
                example {"Price": "#,##0.00", "Date": "yyyy-mm-dd"}.
            freeze_header: Freeze the header row in .xlsx output.
        """
        output = _check_create_output(config, output_path, overwrite)
        warnings: List[str] = []
        names, data = _create_rows(columns, rows, warnings)
        if len(data) > _max_rows(config):
            raise ValueError(
                f"rows has {len(data)} entries; the limit is {_max_rows(config)} "
                "(raise AGENT_SHEET_MAX_ROWS to allow it)"
            )

        suffix = output.suffix.lower()
        sheet_name = _sheet_name(sheet) if suffix == ".xlsx" else (output.stem or _DEFAULT_SHEET)
        if suffix == ".xlsx":
            _write_created_xlsx(
                output,
                names,
                data,
                sheet_name,
                allow_formulas=allow_formulas,
                number_formats=number_formats,
                freeze_header=freeze_header,
                warnings=warnings,
            )
        else:
            if number_formats:
                _warn(warnings, "number_formats is ignored for CSV output")
            if not freeze_header:
                _warn(warnings, "freeze_header is ignored for CSV output")
            _write_created_csv(output, names, data, allow_formulas=allow_formulas, warnings=warnings)

        return {
            "output_path": _relative(config, output),
            "format": suffix.lstrip("."),
            "sheet": sheet_name,
            "rows": len(data),
            "columns": list(names),
            "bytes": output.stat().st_size,
            "warnings": warnings,
            "notes": [
                "This tool creates a new file; it never edits an existing workbook.",
                "Strings that start with =, +, - or @ are written as text unless allow_formulas=true.",
            ],
        }

    def SheetQuery(
        file_path: str,
        sheet: str = "",
        filters: Optional[List[Dict[str, Any]]] = None,
        group_by: Optional[List[str]] = None,
        aggregations: Optional[List[Dict[str, Any]]] = None,
        select: Optional[List[str]] = None,
        sort_by: Optional[List[str]] = None,
        ascending: bool = True,
        limit: int = 50,
    ) -> Dict[str, Any]:
        """Filter, group and aggregate one sheet without modifying it.

        Use this for questions such as "how many rows per region" or "top 10
        orders over 100". It returns at most `limit` rows; ask for a summary
        instead of dumping a large table.

        Args:
            file_path: Workbook or CSV path inside the workspace.
            sheet: Optional worksheet name; defaults to the first sheet.
            filters: List of {column, op, value}. ops: eq, ne, gt, ge, lt, le,
                in, not_in, contains, startswith, endswith, between, is_null,
                not_null. contains/startswith/endswith are case-insensitive
                unless case_sensitive=true. between takes a two-item value.
            group_by: Columns to group by; omit for a single aggregate row.
            aggregations: List of {column, op, alias?}. ops: count, size, sum,
                mean, median, min, max, std, nunique. Use op "size" (or omit
                column) to count rows; "count" counts non-null values.
            select: Optional columns to return after aggregation.
            sort_by: Optional columns to sort the result by.
            ascending: Sort direction when sort_by is set.
            limit: Maximum rows to return, 1-500.
        """
        path, suffix = _check_input(config, file_path)
        warnings: List[str] = []
        frame, actual = _read_frame(config, path, suffix, sheet, warnings)
        filtered = _apply_filters(frame, filters, warnings)

        if _as_list(aggregations, field="aggregations"):
            result = _aggregate(filtered, group_by, aggregations, warnings)
        else:
            if _as_list(group_by, field="group_by"):
                raise ValueError("group_by needs aggregations; add at least one aggregation")
            result = filtered

        result = _sort_and_select(result, sort_by, ascending, select)
        matched = len(result)
        capped = _clamp_limit(limit)
        output = _rows(result, capped)
        return {
            "path": _relative(config, path),
            "sheet": actual,
            "rows_matched": int(matched),
            "rows_returned": len(output),
            "truncated": matched > capped,
            "columns": [str(column) for column in result.columns],
            "rows": output,
            "warnings": warnings,
        }

    def SheetTransform(
        input_path: str,
        output_path: str,
        sheet: str = "",
        operations: Optional[List[Dict[str, Any]]] = None,
        output_sheet: str = "",
        overwrite: bool = False,
    ) -> Dict[str, Any]:
        """Clean or reshape one sheet and write the result to a **new** file.

        Never edits the input. Only the selected sheet is written; other
        worksheets in an .xlsx are not copied. Supports .xlsx and .csv output.

        Args:
            input_path: Source workbook or CSV inside the workspace.
            output_path: New .xlsx or .csv path inside the workspace; must differ
                from input_path.
            sheet: Source worksheet name; defaults to the first sheet.
            operations: Ordered list of objects. Supported ops:
                select {columns}; drop_columns {columns};
                rename {columns: {old: new}};
                dropna {subset?, how?, thresh?};
                fillna {columns?, value} or {columns?, method: ffill|bfill};
                drop_duplicates {subset?, keep?: first|last|false};
                astype {column, type: string|int|float|bool|datetime, errors?};
                strip {columns?, empty_to_null?}; lower {columns?}; upper {columns?};
                replace {column, old, new}; filter {filters};
                sort {by, ascending?}; limit {rows, from_end?}; reset_index.
            output_sheet: Worksheet name for .xlsx output; ignored for CSV.
            overwrite: Replace an existing output file instead of refusing.
        """
        path, suffix = _check_input(config, input_path)
        output = _check_output(config, path, output_path, overwrite)
        warnings: List[str] = []
        frame, actual = _read_frame(config, path, suffix, sheet, warnings)
        before = len(frame)

        specs = _as_list(operations, field="operations")
        if len(specs) > _MAX_OPERATIONS:
            raise ValueError(f"at most {_MAX_OPERATIONS} operations are allowed")
        for index, raw in enumerate(specs, start=1):
            frame = _apply_operation(frame, raw, index, warnings)

        if len(frame) > _max_rows(config):
            raise ValueError(
                f"result has {len(frame)} rows; the limit is {_max_rows(config)} "
                "(raise AGENT_SHEET_MAX_ROWS to allow it)"
            )
        output_name = _sheet_name(output_sheet) if output.suffix.lower() == ".xlsx" else ""
        if output_name:
            _write_frame(config, frame, output, sheet_name=output_name)
        else:
            _write_frame(config, frame, output)
        if output.suffix.lower() == ".csv" and output_sheet:
            _warn(warnings, "output_sheet is ignored for CSV output")

        return {
            "input_path": _relative(config, path),
            "input_sheet": actual,
            "output_path": _relative(config, output),
            "rows_before": int(before),
            "rows_after": len(frame),
            "columns": [str(column) for column in frame.columns],
            "operations_applied": len(specs),
            "warnings": warnings,
        }

    def SheetPivot(
        input_path: str,
        output_path: str,
        index: List[str],
        columns: Optional[List[str]] = None,
        values: Optional[List[str]] = None,
        aggfunc: Any = "sum",
        sheet: str = "",
        fill_value: Optional[Any] = None,
        margins: bool = False,
        output_sheet: str = "",
        overwrite: bool = False,
    ) -> Dict[str, Any]:
        """Build a pivot table from one sheet and write it to a **new** file.

        Never edits the input. The index columns are written as leading
        columns, so the output opens cleanly in Excel and CSV readers.

        Args:
            input_path: Source workbook or CSV inside the workspace.
            output_path: New .xlsx or .csv path inside the workspace; must differ
                from input_path.
            index: Column(s) that identify a pivot row.
            columns: Optional column(s) whose values become output columns.
            values: Numeric column(s) to aggregate. Defaults to every numeric
                column except the index and columns.
            aggfunc: One of count, size, sum, mean, median, min, max, std,
                nunique; or a list of them.
            sheet: Source worksheet name; defaults to the first sheet.
            fill_value: Value used for missing pivot cells.
            margins: Add row and column totals.
            output_sheet: Worksheet name for .xlsx output; ignored for CSV.
            overwrite: Replace an existing output file instead of refusing.
        """
        path, suffix = _check_input(config, input_path)
        output = _check_output(config, path, output_path, overwrite)
        warnings: List[str] = []
        frame, actual = _read_frame(config, path, suffix, sheet, warnings)

        index_columns = [str(item) for item in _as_list(index, field="index")]
        if not index_columns:
            raise ValueError("index must contain at least one column")
        column_keys = [str(item) for item in _as_list(columns, field="columns")]
        value_keys = [str(item) for item in _as_list(values, field="values")]
        _require_columns(frame, index_columns, detail="index")
        if column_keys:
            _require_columns(frame, column_keys, detail="columns")
        if value_keys:
            _require_columns(frame, value_keys, detail="values")
        else:
            reserved = set(index_columns) | set(column_keys)
            value_keys = [
                str(column)
                for column in frame.columns
                if str(column) not in reserved and pd.api.types.is_numeric_dtype(frame[column])
            ]
            if not value_keys:
                raise ValueError("values is required when the sheet has no numeric columns")
            _warn(warnings, f"values defaulted to numeric columns: {', '.join(value_keys)}")

        functions = [str(item).strip().lower() for item in _as_list(aggfunc, field="aggfunc")]
        if not functions:
            functions = ["sum"]
        unknown = [item for item in functions if item not in _AGGREGATIONS]
        if unknown:
            raise ValueError(f"unknown aggfunc {unknown}; use one of {', '.join(sorted(_AGGREGATIONS))}")
        callable_functions: Any = functions[0] if len(functions) == 1 else functions

        key_columns = index_columns + column_keys
        if frame.duplicated(subset=key_columns).any():
            _warn(warnings, f"duplicate {' + '.join(key_columns)} keys were aggregated")

        pivot_values: Any = value_keys[0] if len(value_keys) == 1 else value_keys
        try:
            table = pd.pivot_table(
                frame,
                index=index_columns,
                columns=column_keys or None,
                values=pivot_values,
                aggfunc=callable_functions,
                fill_value=fill_value,
                margins=margins,
                dropna=True,
                observed=False,
            )
        except Exception as exc:
            raise ValueError(f"could not build the pivot: {exc}") from exc
        if table is None or (hasattr(table, "empty") and table.empty):
            raise ValueError("the pivot produced no rows; check index, columns and values")

        if isinstance(table.columns, pd.MultiIndex):
            table.columns = [
                " / ".join(str(part) for part in key if str(part) not in {"", "None"})
                for key in table.columns
            ]
        else:
            table.columns = [str(column) for column in table.columns]

        if len(table) > _max_rows(config):
            raise ValueError(
                f"pivot has {len(table)} rows; the limit is {_max_rows(config)} "
                "(raise AGENT_SHEET_MAX_ROWS to allow it)"
            )
        output_name = _sheet_name(output_sheet) if output.suffix.lower() == ".xlsx" else ""
        _write_frame(config, table, output, sheet_name=output_name or _DEFAULT_SHEET, index=True)
        if output.suffix.lower() == ".csv" and output_sheet:
            _warn(warnings, "output_sheet is ignored for CSV output")

        return {
            "input_path": _relative(config, path),
            "input_sheet": actual,
            "output_path": _relative(config, output),
            "rows": len(table),
            "columns": [str(column) for column in table.columns],
            "aggfunc": functions,
            "warnings": warnings,
        }

    return [
        sdk.tool(SheetInspect, name="SheetInspect", effect="read", concurrency_safe=True),
        sdk.tool(SheetCreate, name="SheetCreate"),
        sdk.tool(SheetQuery, name="SheetQuery", effect="read", concurrency_safe=True),
        sdk.tool(SheetTransform, name="SheetTransform"),
        sdk.tool(SheetPivot, name="SheetPivot"),
    ]
