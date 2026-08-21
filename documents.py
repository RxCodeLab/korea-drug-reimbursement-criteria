from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

PARSER_VERSION = "documents-4-rhwp-0.8.4"
_MAX_RHWP_OUTPUT = 128 * 1024 * 1024
_MAX_STDERR = 8 * 1024
_HEADER = re.compile(r"^\[(?:\d{3}|일반원칙)\](?:\s+\S.*)?$")
_ACTION = re.compile(r"\[\s*(신\s*설|변\s*경|삭\s*제)\s*\]")


class ExtractionError(RuntimeError):
    pass


def _bounded_stderr(stderr: bytes | str | None) -> str:
    if isinstance(stderr, bytes):
        message = stderr.decode("utf-8", errors="replace")
    else:
        message = stderr or ""
    return message[-_MAX_STDERR:].strip()


def _rhwp_json(path: Path, command: str) -> dict[str, object]:
    executable = os.environ.get("RHWP_BIN", "rhwp")
    arguments = [executable, command, str(path), "--json"]
    if command == "export-text":
        arguments.extend(["--max-chars", "32000000"])
    try:
        result = subprocess.run(
            arguments,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
        )
    except FileNotFoundError as exc:
        raise ExtractionError("HWP/HWPX를 처리하려면 rhwp 0.8.4 명령이 설치되어 있어야 합니다") from exc
    except subprocess.TimeoutExpired as exc:
        raise ExtractionError(f"rhwp {command} 시간이 초과되었습니다: {path.name}: {_bounded_stderr(exc.stderr)}") from exc

    if result.returncode:
        detail = _bounded_stderr(result.stderr)
        suffix = f": {detail}" if detail else ""
        raise ExtractionError(f"rhwp {command} 실패: {path.name} (exit {result.returncode}){suffix}")
    if len(result.stdout) > _MAX_RHWP_OUTPUT:
        raise ExtractionError("rhwp JSON 출력이 용량 제한을 초과했습니다")
    try:
        payload = json.loads(result.stdout)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExtractionError(f"rhwp JSON 출력이 잘못되었습니다: {path.name}") from exc
    if not isinstance(payload, dict) or payload.get("schemaVersion") != "1.0":
        raise ExtractionError("rhwp JSON 스키마 버전이 지원되지 않습니다")
    return payload


def _extract_text_payload(path: Path) -> str:
    payload = _rhwp_json(path, "export-text")
    if payload.get("truncated") is not False:
        raise ExtractionError("rhwp 텍스트 출력이 잘렸습니다")
    page_count = payload.get("pageCount")
    pages = payload.get("pages")
    if isinstance(page_count, bool) or not isinstance(page_count, int) or page_count < 0:
        raise ExtractionError("rhwp JSON 페이지 수가 잘못되었습니다")
    if not isinstance(pages, list) or page_count != len(pages):
        raise ExtractionError("rhwp JSON 페이지 수와 페이지 목록이 일치하지 않습니다")
    previous_page: int | None = None
    texts: list[str] = []
    for entry in pages:
        if not isinstance(entry, dict):
            raise ExtractionError("rhwp JSON 페이지 항목이 잘못되었습니다")
        page = entry.get("page")
        text = entry.get("text")
        if isinstance(page, bool) or not isinstance(page, int) or (previous_page is not None and page <= previous_page):
            raise ExtractionError("rhwp JSON 페이지 순서가 잘못되었습니다")
        if not isinstance(text, str):
            raise ExtractionError("rhwp JSON 페이지 텍스트가 잘못되었습니다")
        previous_page = page
        texts.append(text)
    output = "\n".join(texts).strip()
    if not output:
        raise ExtractionError("rhwp에서 비어 있는 텍스트를 반환했습니다")
    return output


def _table_lines(table: object) -> list[str]:
    if not isinstance(table, dict):
        raise ExtractionError("rhwp JSON 표 항목이 잘못되었습니다")
    rows = table.get("rows")
    cols = table.get("cols")
    cells = table.get("cells")
    cell_count = table.get("cellCount")
    if (isinstance(rows, bool) or not isinstance(rows, int) or rows < 0
            or isinstance(cols, bool) or not isinstance(cols, int) or cols < 0
            or not isinstance(cells, list) or cell_count != len(cells)):
        raise ExtractionError("rhwp JSON 표 구조가 잘못되었습니다")
    grouped: dict[int, list[tuple[int, bool, str, int]]] = {}
    seen: set[tuple[int, int]] = set()
    for cell in cells:
        if not isinstance(cell, dict):
            raise ExtractionError("rhwp JSON 표 셀이 잘못되었습니다")
        row, column, text, is_header = cell.get("row"), cell.get("col"), cell.get("text"), cell.get("isHeader")
        row_span, col_span = cell.get("rowSpan"), cell.get("colSpan")
        if (isinstance(row, bool) or not isinstance(row, int) or not 0 <= row < rows
                or isinstance(column, bool) or not isinstance(column, int) or not 0 <= column < cols
                or isinstance(row_span, bool) or not isinstance(row_span, int) or row_span < 1
                or isinstance(col_span, bool) or not isinstance(col_span, int) or col_span < 1
                or not isinstance(is_header, bool) or not isinstance(text, str)
                or (row, column) in seen):
            raise ExtractionError("rhwp JSON 표 셀 필드가 잘못되었습니다")
        seen.add((row, column))
        grouped.setdefault(row, []).append((column, is_header, text, col_span))

    headers = [
        (column, re.sub(r"\s+", "", text), col_span)
        for row_cells in grouped.values()
        for column, is_header, text, col_span in row_cells
        if is_header
    ]
    current = any(text.startswith("현행") for _, text, _ in headers)
    reason = any(text == "사유" for _, text, _ in headers)
    revision = next(
        ((column, col_span) for column, text, col_span in headers if text.startswith("개정")),
        None,
    )
    selected_columns: set[int] | None = None
    if current and reason and revision:
        revision_column, revision_span = revision
        selected_columns = set(range(revision_column, revision_column + revision_span))
        if revision_span == 1:
            division_columns = [column for column, text, _ in headers if text == "구분"]
            if division_columns:
                selected_columns.add(min(division_columns))

    lines: list[str] = []
    for _, row_cells in sorted(grouped.items()):
        if all(is_header for _, is_header, _, _ in row_cells):
            continue
        for column, _, text, col_span in sorted(row_cells):
            if selected_columns is not None and (column not in selected_columns or col_span == cols):
                continue
            if text.strip():
                lines.append(text.strip())
    return lines


def _reconstruct_tables(tables_payload: dict[str, object], structure_payload: dict[str, object]) -> str | None:
    table_count = tables_payload.get("tableCount")
    tables = tables_payload.get("tables")
    if isinstance(table_count, bool) or not isinstance(table_count, int) or table_count < 0 or not isinstance(tables, list):
        raise ExtractionError("rhwp JSON 표 목록이 잘못되었습니다")
    if table_count != len(tables):
        raise ExtractionError("rhwp JSON 표 수와 표 목록이 일치하지 않습니다")
    structure = structure_payload.get("structure")
    if not isinstance(structure, dict):
        raise ExtractionError("rhwp JSON 구조 preamble이 잘못되었습니다")
    preamble = structure["preamble"]
    if not isinstance(preamble, list) or not all(isinstance(line, str) for line in preamble):
        raise ExtractionError("rhwp JSON 구조 preamble이 잘못되었습니다")
    table_lines = [_table_lines(table) for table in tables]
    headers = [line.strip() for line in preamble if _HEADER.fullmatch(line.strip())]
    if table_count == 0:
        return None
    if len(headers) != table_count:
        comparison_tables: list[tuple[str, list[str]]] = []
        for table, lines_for_table in zip(tables, table_lines, strict=True):
            cells = table["cells"]
            header_texts = [
                cell["text"]
                for cell in cells
                if cell["isHeader"]
            ]
            compact_headers = {re.sub(r"\s+", "", text) for text in header_texts}
            if not (
                any(text.startswith("현행") for text in compact_headers)
                and any(text.startswith("개정") for text in compact_headers)
                and "사유" in compact_headers
            ):
                continue
            class_header = next(
                (
                    line.strip()
                    for text in header_texts
                    for line in text.splitlines()
                    if _HEADER.fullmatch(line.strip())
                ),
                None,
            )
            if class_header:
                comparison_tables.append((class_header, lines_for_table))
        if not comparison_tables:
            return None
        lines = ["[변경]"]
        for class_header, lines_for_table in comparison_tables:
            lines.append(class_header)
            lines.extend(lines_for_table)
        return "\n".join(lines).strip()

    action = ""
    header_actions: list[str] = []
    for line in preamble:
        marker = _ACTION.search(line)
        if marker:
            action = re.sub(r"\s", "", marker.group(1))
        elif _HEADER.fullmatch(line.strip()):
            header_actions.append(action)

    lines: list[str] = []
    emitted_action = ""
    for header, header_action, lines_for_table in zip(headers, header_actions, table_lines, strict=True):
        if header_action and header_action != emitted_action:
            lines.append(f"[{header_action}]")
            emitted_action = header_action
        lines.append(header)
        lines.extend(lines_for_table)
    output = "\n".join(lines).strip()
    return output or None


def extract_rhwp(path: Path) -> str:
    text = _extract_text_payload(path)
    tables = _rhwp_json(path, "export-tables")
    structure = _rhwp_json(path, "export-structure")
    return _reconstruct_tables(tables, structure) or text


def extract_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        return "\n".join(page.extract_text() or "" for page in reader.pages).strip()
    except Exception as exc:
        raise ExtractionError(f"PDF 텍스트 추출 실패: {path.name}: {exc}") from exc


def extract_document(path: Path, document_format: str) -> str:
    dispatch = {"hwpx": extract_rhwp, "hwp": extract_rhwp, "pdf": extract_pdf}
    try:
        return dispatch[document_format.lower()](path)
    except KeyError as exc:
        raise ExtractionError(f"지원하지 않는 문서 형식입니다: {document_format}") from exc
