"""CSV writing that cannot plant a formula in the reader's spreadsheet.

Every CSV HeyLead writes carries names, titles and companies that came from
an imported file or from a LinkedIn profile someone else wrote. A cell that
starts with ``=``, ``+``, ``-``, ``@``, a tab or a carriage return is read as
a formula by Excel, Sheets and LibreOffice, so ``=HYPERLINK(...)`` in a
headline became a live link in the user's sheet (D4umak/heylead-api#1685).
``safe_cell`` prefixes such a cell with ``'``, which spreadsheets show as
plain text. Numbers pass through untouched.

Write CSV only through ``writer`` / ``dict_writer`` here; the semgrep rule
``csv-writer-outside-csvsafe`` fails the suite on a bare ``csv.writer``.
The same file lives in heylead-api as ``app/services/csvsafe.py``.
"""

from __future__ import annotations

import csv
from typing import Any, Iterable, Mapping

FORMULA_LEADS = ("=", "+", "-", "@", "\t", "\r")


def safe_cell(value: Any) -> Any:
    """``value`` with a leading ``'`` when a spreadsheet would run it."""
    if isinstance(value, str) and value.startswith(FORMULA_LEADS):
        return "'" + value
    return value


class _SafeWriter:
    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def writerow(self, row: Iterable[Any]) -> Any:
        return self._inner.writerow([safe_cell(v) for v in row])

    def writerows(self, rows: Iterable[Iterable[Any]]) -> None:
        for row in rows:
            self.writerow(row)


class _SafeDictWriter:
    def __init__(self, inner: csv.DictWriter) -> None:
        self._inner = inner

    def writeheader(self) -> Any:
        return self._inner.writerow({f: safe_cell(f) for f in self._inner.fieldnames})

    def writerow(self, row: Mapping[str, Any]) -> Any:
        return self._inner.writerow({k: safe_cell(v) for k, v in row.items()})

    def writerows(self, rows: Iterable[Mapping[str, Any]]) -> None:
        for row in rows:
            self.writerow(row)


def writer(stream: Any, **kwargs: Any) -> _SafeWriter:
    """``csv.writer`` whose every cell goes through ``safe_cell``."""
    return _SafeWriter(csv.writer(stream, **kwargs))


def dict_writer(stream: Any, fieldnames: list[str], **kwargs: Any) -> _SafeDictWriter:
    """``csv.DictWriter`` whose every cell goes through ``safe_cell``."""
    return _SafeDictWriter(csv.DictWriter(stream, fieldnames=fieldnames, **kwargs))
