"""Tabular file reader — CSV and XLSX rows, standard library only.

``import_prospects`` used to accept prospect data only as a ``csv_data``
string, which meant a 527-row spreadsheet had to be squeezed through a single
LLM tool argument.  Rows were lost in transit and nobody noticed.  This module
reads the rows straight off disk instead.

XLSX is parsed with the stdlib (a .xlsx file is a zip of XML parts) —
``openpyxl`` is *not* a declared dependency of this project, so relying on it
would make the feature work on some installs and fail on others.

Every reader returns ``(row_number, cells)`` pairs with the **source row
number preserved**, so a caller can report per-row dispositions that point
back at the spreadsheet the user is looking at.
"""

from __future__ import annotations

import csv
import io
import logging
import os
import zipfile
from xml.etree import ElementTree as ET

logger = logging.getLogger(__name__)

# Refuse absurd inputs loudly rather than chewing through a zip bomb.
MAX_ROWS = 200_000
# Sheet XML is streamed row by row, so MAX_ROWS bounds it. sharedStrings.xml
# has to be read whole, so it needs a bound of its own.
MAX_SHARED_STRING_BYTES = 256 * 1024 * 1024


class TabularReadError(Exception):
    """Raised when a prospect file cannot be read."""


# ── XML helpers ──

def _local(tag: str) -> str:
    """Strip the XML namespace from a tag name."""
    return tag.rsplit("}", 1)[-1]


def _col_index(ref: str) -> int:
    """Column index from a cell reference: 'A1' -> 0, 'AB12' -> 27."""
    letters = ""
    for ch in ref or "":
        if ch.isalpha():
            letters += ch
        else:
            break
    if not letters:
        return -1
    idx = 0
    for ch in letters.upper():
        idx = idx * 26 + (ord(ch) - 64)
    return idx - 1


def _si_text(node) -> str:
    """Text of a shared-string / inline-string node, including rich-text runs.

    Phonetic hints (``rPh``) are deliberately skipped — they are furigana, not
    part of the value.
    """
    if node is None:
        return ""
    parts: list[str] = []
    for child in node:
        tag = _local(child.tag)
        if tag == "t":
            parts.append(child.text or "")
        elif tag == "r":
            for sub in child:
                if _local(sub.tag) == "t":
                    parts.append(sub.text or "")
    return "".join(parts)


def _number_text(value: str) -> str:
    """Render a numeric cell as the user would expect to see it."""
    value = (value or "").strip()
    if not value:
        return ""
    try:
        num = float(value)
    except ValueError:
        return value
    if num.is_integer() and abs(num) < 1e15:
        return str(int(num))
    return value


def _cell_value(cell, shared: list[str]) -> str:
    kind = cell.get("t") or "n"
    v_text: str | None = None
    inline = None
    for sub in cell:
        tag = _local(sub.tag)
        if tag == "v":
            v_text = sub.text or ""
        elif tag == "is":
            inline = sub

    if kind == "s":
        try:
            idx = int((v_text or "").strip())
        except ValueError:
            return ""
        return shared[idx] if 0 <= idx < len(shared) else ""
    if kind == "inlineStr":
        return _si_text(inline)
    if kind == "str":
        return v_text or ""
    if kind == "b":
        return "FALSE" if (v_text or "").strip() in ("", "0", "false", "FALSE") else "TRUE"
    if kind == "e":
        return ""
    return _number_text(v_text or "")


# ── XLSX ──

def _read_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    try:
        info = zf.getinfo("xl/sharedStrings.xml")
    except KeyError:
        return []
    # Check the declared uncompressed size before decompressing: a 40 KB zip
    # member can expand to gigabytes.
    if info.file_size > MAX_SHARED_STRING_BYTES:
        raise TabularReadError(
            f"Refusing to read a {info.file_size}-byte shared string table "
            f"(limit {MAX_SHARED_STRING_BYTES} bytes)."
        )
    root = ET.fromstring(zf.read(info))
    return [_si_text(si) for si in root if _local(si.tag) == "si"]


def _norm_part(target: str) -> str:
    """Normalise a workbook relationship target to a zip member path."""
    path = (target or "").lstrip("/")
    if path.startswith("xl/"):
        return path
    return "xl/" + path


def list_sheets(zf: zipfile.ZipFile) -> list[tuple[str, str]]:
    """Return [(sheet_name, zip_member_path)] in workbook order."""
    try:
        workbook = ET.fromstring(zf.read("xl/workbook.xml"))
    except KeyError as exc:
        raise TabularReadError(
            "Not a readable .xlsx workbook (xl/workbook.xml is missing)."
        ) from exc

    rels: dict[str, str] = {}
    try:
        rel_root = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    except KeyError:
        rel_root = []  # type: ignore[assignment]
    for rel in rel_root:
        rid = rel.get("Id")
        if rid:
            rels[rid] = rel.get("Target") or ""

    members = set(zf.namelist())
    sheets: list[tuple[str, str]] = []
    for el in workbook.iter():
        if _local(el.tag) != "sheet":
            continue
        name = el.get("name") or ""
        rid = ""
        for key, val in el.attrib.items():
            if _local(key) == "id":
                rid = val
        path = _norm_part(rels.get(rid, "")) if rels.get(rid) else ""
        if path not in members:
            path = f"xl/worksheets/sheet{len(sheets) + 1}.xml"
        sheets.append((name, path))
    return sheets


def _read_sheet(
    zf: zipfile.ZipFile, path: str, shared: list[str]
) -> list[tuple[int, list[str]]]:
    if path not in zf.namelist():
        raise TabularReadError(f"Worksheet part missing from workbook: {path}")

    out: list[tuple[int, list[str]]] = []
    last_row_no = 0
    with zf.open(path) as handle:
        for _event, el in ET.iterparse(handle, events=("end",)):
            if _local(el.tag) != "row":
                continue
            try:
                row_no = int(el.get("r") or 0)
            except ValueError:
                row_no = 0
            # `r` is optional: a row without one is the row after the previous
            # one. That synthesised number can collide with a later explicit
            # `r`, and two rows sharing a number used to dead-end the whole
            # import (the caller refuses to report an import that does not add
            # up). Row numbers stay strictly increasing so a self-inconsistent
            # sheet still imports, with a note about the rows we renumbered.
            if row_no <= last_row_no:
                if row_no > 0:
                    logger.warning(
                        "%s: row r=%d repeats an earlier row number; "
                        "reading it as row %d",
                        path, row_no, last_row_no + 1,
                    )
                row_no = last_row_no + 1
            last_row_no = row_no

            cells: dict[int, str] = {}
            for cell in el:
                if _local(cell.tag) != "c":
                    continue
                idx = _col_index(cell.get("r") or "")
                if idx < 0:
                    idx = (max(cells) + 1) if cells else 0
                cells[idx] = _cell_value(cell, shared)

            width = max(cells) + 1 if cells else 0
            out.append((row_no, [cells.get(i, "") for i in range(width)]))
            el.clear()
            if len(out) > MAX_ROWS:
                raise TabularReadError(
                    f"Refusing to read more than {MAX_ROWS} rows from {path}."
                )
    return out


def _read_xlsx(path: str, sheet: str) -> list[tuple[int, list[str]]]:
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise TabularReadError(f"Not a readable .xlsx file: {path}") from exc

    with zf:
        sheets = list_sheets(zf)
        if not sheets:
            raise TabularReadError("Workbook contains no sheets.")

        target = ""
        if sheet:
            wanted = sheet.strip().lower()
            for name, member in sheets:
                if name.strip().lower() == wanted:
                    target = member
                    break
            if not target:
                available = ", ".join(repr(n) for n, _ in sheets)
                raise TabularReadError(
                    f"Sheet {sheet!r} not found. Available sheets: {available}"
                )
        else:
            target = sheets[0][1]

        shared = _read_shared_strings(zf)
        return _read_sheet(zf, target, shared)


# ── CSV ──

def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    # latin-1 maps every byte, so this never raises — mangled text beats
    # silently dropping the whole file.
    logger.warning("Falling back to latin-1 decoding for prospect file")
    return raw.decode("latin-1")


def _read_csv_text(text: str) -> list[tuple[int, list[str]]]:
    text = (text or "").lstrip("\ufeff")
    out: list[tuple[int, list[str]]] = []
    for n, row in enumerate(csv.reader(io.StringIO(text)), start=1):
        out.append((n, list(row)))
        if len(out) > MAX_ROWS:
            raise TabularReadError(f"Refusing to read more than {MAX_ROWS} CSV rows.")
    return out


# ── Entry point ──

def read_table(
    csv_data: str = "",
    file_path: str = "",
    sheet: str = "",
) -> tuple[list[str], list[tuple[int, list[str]]]]:
    """Read a CSV/XLSX source into ``(header, [(row_number, cells), ...])``.

    The first row that has any non-empty cell is the header; everything after
    it is returned verbatim — blank rows included — so the caller can account
    for every single row in the file.
    """
    if file_path:
        path = os.path.expanduser(file_path.strip())
        if not os.path.isfile(path):
            raise TabularReadError(f"File not found: {path}")
        if os.path.splitext(path)[1].lower() == ".xls":
            raise TabularReadError(
                "Legacy .xls files are not supported — re-save as .xlsx or .csv."
            )
        if zipfile.is_zipfile(path):
            rows = _read_xlsx(path, sheet)
        else:
            if sheet:
                raise TabularReadError(
                    "The 'sheet' argument only applies to .xlsx files."
                )
            with open(path, "rb") as handle:
                rows = _read_csv_text(_decode(handle.read()))
    else:
        if sheet:
            raise TabularReadError("The 'sheet' argument only applies to .xlsx files.")
        rows = _read_csv_text(csv_data)

    header: list[str] = []
    data: list[tuple[int, list[str]]] = []
    for row_no, cells in rows:
        if not header:
            # A row can be *present* and still empty: ",,", a whitespace-only
            # line, or an Excel row of styled-but-valueless cells. None of them
            # is the header.
            if any(cell.strip() for cell in cells):
                header = [cell.strip() for cell in cells]
            continue
        data.append((row_no, cells))
    return header, data
