from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

import documents
import ingest


def _rhwp_payload(pages: list[dict[str, object]], **overrides: object) -> bytes:
    payload: dict[str, object] = {
        "schemaVersion": "1.0",
        "pageCount": len(pages),
        "pages": pages,
        "truncated": False,
        "omittedCount": 0,
        "untrustedContent": False,
        "untrustedFields": [],
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _tables_payload(tables: list[dict[str, object]]) -> bytes:
    return json.dumps({"schemaVersion": "1.0", "tableCount": len(tables), "tables": tables}, ensure_ascii=False).encode("utf-8")


def _structure_payload(preamble: list[str]) -> bytes:
    return json.dumps({"schemaVersion": "1.0", "structure": {"preamble": preamble}}, ensure_ascii=False).encode("utf-8")


def _table(*rows: list[tuple[bool, str]]) -> dict[str, object]:
    cells = [
        {"row": row, "col": column, "rowSpan": 1, "colSpan": 1, "isHeader": header, "text": text}
        for row, values in enumerate(rows)
        for column, (header, text) in enumerate(values)
    ]
    return {"rows": len(rows), "cols": max(map(len, rows)), "cellCount": len(cells), "cells": cells}


def test_select_renditions_prefers_hwpx_per_role_and_stem() -> None:
    attachments = [
        {"ordinal": 3, "original_name": "별지 1.PDF", "format": "pdf", "role": "annex"},
        {"ordinal": 2, "original_name": "별지-1.hwpx", "format": "hwpx", "role": "annex"},
        {"ordinal": 1, "original_name": "별지 1.pdf", "format": "pdf", "role": "notice"},
    ]
    assert [item["ordinal"] for item in ingest.select_renditions(attachments)] == [2, 1]


def test_rhwp_extracts_ordered_pages_for_hwp_and_hwpx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source.hwp"
    source.write_bytes(b"fixture")
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(command)
        if command[1] == "export-tables":
            output = _tables_payload([])
        elif command[1] == "export-structure":
            output = _structure_payload([])
        else:
            output = _rhwp_payload([{"page": 1, "text": "first"}, {"page": 2, "text": "second"}])
        return subprocess.CompletedProcess(
            command, 0, output, b""
        )

    monkeypatch.setattr(documents.subprocess, "run", fake_run)
    assert documents.extract_document(source, "hwp") == "first\nsecond"
    assert documents.extract_document(source, "hwpx") == "first\nsecond"
    assert calls == [
        ["rhwp", "export-text", str(source), "--json", "--max-chars", "32000000"],
        ["rhwp", "export-tables", str(source), "--json"],
        ["rhwp", "export-structure", str(source), "--json"],
        ["rhwp", "export-text", str(source), "--json", "--max-chars", "32000000"],
        ["rhwp", "export-tables", str(source), "--json"],
        ["rhwp", "export-structure", str(source), "--json"],
    ]


@pytest.mark.parametrize(
    "stdout",
    [
        b"{not json",
        _rhwp_payload([{"page": 1, "text": "partial"}], truncated=True),
        _rhwp_payload([{"page": 2, "text": "second"}, {"page": 1, "text": "first"}]),
    ],
)
def test_rhwp_rejects_malformed_truncated_or_unordered_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stdout: bytes
) -> None:
    source = tmp_path / "source.hwpx"
    source.write_bytes(b"fixture")

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(command, 0, stdout, b"")

    monkeypatch.setattr(documents.subprocess, "run", fake_run)
    with pytest.raises(documents.ExtractionError):
        documents.extract_document(source, "hwpx")


def test_rhwp_reconstructs_structured_tables_in_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source.hwpx"
    source.write_bytes(b"fixture")
    tables = [
        _table([(True, "구 분"), (True, "품명")], [(False, "Lanadelumab"), (False, "주사제")],
               [(False, "(품명 :"), (False, "탁자이로프리필드시린지주)")], [(False, "1. 첫 조건"), (False, "")]),
        _table([(True, "구 분")], [(False, "성분명(품명: 제품 2)")], [(False, "1. 둘째 조건")]),
        _table([(True, "구 분")], [(False, "성분명(품명: 제품 3)")], [(False, "1. 셋째 조건")]),
    ]

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        outputs = {
            "export-text": _rhwp_payload([{"page": 1, "text": "wrong order"}]),
            "export-tables": _tables_payload(tables),
            "export-structure": _structure_payload(["[신설]", "[219]", "[변경]", "[220]", "[221]"]),
        }
        return subprocess.CompletedProcess(command, 0, outputs[command[1]], b"")

    monkeypatch.setattr(documents.subprocess, "run", fake_run)
    blocks = ingest.split_blocks(documents.extract_document(source, "hwpx"))
    assert [(block["action"], block["class_no"]) for block in blocks] == [("신설", "219"), ("변경", "220"), ("변경", "221")]
    assert blocks[0]["title"] == "Lanadelumab 주사제 (품명 : 탁자이로프리필드시린지주)"
    assert blocks[0]["body"] == "1. 첫 조건"


@pytest.mark.parametrize(
    ("cols", "cells", "expected"),
    [
        (
            4,
            [
                {"row": 0, "col": 0, "rowSpan": 1, "colSpan": 4, "isHeader": True, "text": "[일반원칙]"},
                {"row": 1, "col": 0, "rowSpan": 2, "colSpan": 1, "isHeader": True, "text": "구 분"},
                {"row": 1, "col": 1, "rowSpan": 1, "colSpan": 2, "isHeader": True, "text": "세부인정기준"},
                {"row": 1, "col": 3, "rowSpan": 2, "colSpan": 1, "isHeader": True, "text": "사유"},
                {"row": 2, "col": 1, "rowSpan": 1, "colSpan": 1, "isHeader": True, "text": "현 행"},
                {"row": 2, "col": 2, "rowSpan": 1, "colSpan": 1, "isHeader": True, "text": "개 정(안)"},
                {"row": 3, "col": 0, "rowSpan": 1, "colSpan": 1, "isHeader": False, "text": "[일반원칙]\n새 제목"},
                {"row": 3, "col": 1, "rowSpan": 1, "colSpan": 1, "isHeader": False, "text": "현행 본문"},
                {"row": 3, "col": 2, "rowSpan": 1, "colSpan": 1, "isHeader": False, "text": "개정 본문"},
                {"row": 3, "col": 3, "rowSpan": 1, "colSpan": 1, "isHeader": False, "text": "변경 사유"},
                {"row": 4, "col": 0, "rowSpan": 1, "colSpan": 4, "isHeader": False, "text": "관련 근거"},
            ],
            ["[일반원칙]\n새 제목", "개정 본문"],
        ),
        (
            5,
            [
                {"row": 0, "col": 0, "rowSpan": 1, "colSpan": 5, "isHeader": True, "text": "[232] 소화성궤양용제"},
                {"row": 1, "col": 0, "rowSpan": 1, "colSpan": 2, "isHeader": True, "text": "현 행"},
                {"row": 1, "col": 2, "rowSpan": 1, "colSpan": 2, "isHeader": True, "text": "개 정(안)"},
                {"row": 1, "col": 4, "rowSpan": 2, "colSpan": 1, "isHeader": True, "text": "사유"},
                {"row": 2, "col": 0, "rowSpan": 1, "colSpan": 1, "isHeader": True, "text": "구 분"},
                {"row": 2, "col": 1, "rowSpan": 1, "colSpan": 1, "isHeader": True, "text": "세부인정기준"},
                {"row": 2, "col": 2, "rowSpan": 1, "colSpan": 1, "isHeader": True, "text": "구 분"},
                {"row": 2, "col": 3, "rowSpan": 1, "colSpan": 1, "isHeader": True, "text": "세부인정기준"},
                {"row": 3, "col": 0, "rowSpan": 1, "colSpan": 1, "isHeader": False, "text": "기존 제목"},
                {"row": 3, "col": 1, "rowSpan": 1, "colSpan": 1, "isHeader": False, "text": "기존 본문"},
                {"row": 3, "col": 2, "rowSpan": 1, "colSpan": 1, "isHeader": False, "text": "개정 제목"},
                {"row": 3, "col": 3, "rowSpan": 1, "colSpan": 1, "isHeader": False, "text": "개정 본문"},
                {"row": 3, "col": 4, "rowSpan": 1, "colSpan": 1, "isHeader": False, "text": "변경 사유"},
            ],
            ["개정 제목", "개정 본문"],
        ),
    ],
)
def test_rhwp_comparison_tables_keep_only_revised_criteria(
    cols: int, cells: list[dict[str, object]], expected: list[str]
) -> None:
    table = {"rows": max(cell["row"] for cell in cells) + 1, "cols": cols, "cellCount": len(cells), "cells": cells}
    assert documents._table_lines(table) == expected


def test_rhwp_reconstructs_comparison_table_without_preamble_headers() -> None:
    cells = [
        {"row": 0, "col": 0, "rowSpan": 1, "colSpan": 5, "isHeader": True, "text": "[232] 소화성궤양용제"},
        {"row": 1, "col": 0, "rowSpan": 1, "colSpan": 2, "isHeader": True, "text": "현 행"},
        {"row": 1, "col": 2, "rowSpan": 1, "colSpan": 2, "isHeader": True, "text": "개 정(안)"},
        {"row": 1, "col": 4, "rowSpan": 1, "colSpan": 1, "isHeader": True, "text": "사유"},
        {"row": 2, "col": 2, "rowSpan": 1, "colSpan": 1, "isHeader": False, "text": "[232]\nTegoprazan(품명: 개정 제품 등)"},
        {"row": 2, "col": 3, "rowSpan": 1, "colSpan": 1, "isHeader": False, "text": "개정 본문"},
    ]
    table = {"rows": 3, "cols": 5, "cellCount": len(cells), "cells": cells}
    tables = {"tableCount": 1, "tables": [table]}
    structure = {"structure": {"preamble": ["변경대비표", "[별지 2]"]}}
    text = documents._reconstruct_tables(tables, structure)
    assert text == "[변경]\n[232] 소화성궤양용제\n[232]\nTegoprazan(품명: 개정 제품 등)\n개정 본문"


def test_split_blocks_joins_multiline_pummyeong_title_before_numbered_body() -> None:
    text = "\n".join([
        "[219]",
        "Lanadelumab",
        "주사제",
        "(품명 :",
        "탁자이로프리필드시린지주)",
        "1. 유전성 혈관부종 환자에게 투여한다.",
        "2. 추가 조건",
    ])
    blocks = ingest.split_blocks(text)
    assert blocks == [{
        "action": "",
        "class_no": "219",
        "class_header": "",
        "title": "Lanadelumab 주사제 (품명 : 탁자이로프리필드시린지주)",
        "body": "1. 유전성 혈관부종 환자에게 투여한다.\n2. 추가 조건",
    }]


def test_split_blocks_rejects_gu_bun_pseudo_item() -> None:
    text = "[142]\n구 분\n설명\n[143]\n성분명(품명: 제품)\n급여 기준"
    blocks = ingest.split_blocks(text)
    assert len(blocks) == 1
    assert blocks[0]["class_no"] == "143"


def test_normalized_output_is_deterministic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    raw = tmp_path / "raw" / "20260101_1"
    raw.mkdir(parents=True)
    source = raw / "annex.hwpx"
    source.write_bytes(b"fixture")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    meta = {
        "schema_version": 1,
        "complete": True,
        "version": {"행정규칙일련번호": "1", "시행일자": "20260101", "발령일자": "20251231", "발령번호": "1", "행정규칙명": "약제"},
        "attachments": [{"ordinal": 1, "source_url": "https://example.test/annex", "original_name": "annex.hwpx", "stored_name": "annex.hwpx", "format": "hwpx", "role": "annex", "size": source.stat().st_size, "sha256": digest, "status": "complete"}],
    }
    (raw / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(ingest, "NORMALIZED", tmp_path / "normalized")
    monkeypatch.setattr(ingest, "extract_document", lambda _path, _format: "[142]\n성분명(품명: 제품)\n급여 기준")
    first = ingest._parse_version(raw, ingest._validate_meta(raw / "meta.json"))
    contents = (tmp_path / "normalized" / "1.json").read_bytes()
    second = ingest._parse_version(raw, ingest._validate_meta(raw / "meta.json"))
    assert first == second
    assert contents == (tmp_path / "normalized" / "1.json").read_bytes()


def test_database_keeps_distinct_attachments_with_same_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest = "a" * 64
    attachments = [
        {
            "ordinal": ordinal,
            "source_url": f"https://www.law.go.kr/file/{ordinal}",
            "original_name": name,
            "stored_name": name,
            "format": "hwpx",
            "role": role,
            "size": 10,
            "sha256": digest,
            "status": "complete",
            "parser_version": documents.PARSER_VERSION,
            "parser_status": status,
        }
        for ordinal, name, role, status in [
            (1, "별지.hwpx", "annex", "complete"),
            (2, "고시.hwpx", "notice", "not_selected"),
        ]
    ]
    document = {
        "version": {
            "시행일자": "20260101",
            "발령번호": "1",
            "발령일자": "20251231",
            "행정규칙일련번호": "1",
            "행정규칙명": "약제",
        },
        "attachments": attachments,
        "entries": [{
            "action": "신설",
            "class_no": "219",
            "class_header": "[219] 기타",
            "title": "성분명(품명: 제품)",
            "body": "급여 기준",
            "attachment_ordinal": 1,
            "attachment_sha256": digest,
            "block_identity": "[219]성분명",
        }],
    }
    database = tmp_path / "criteria.db"
    monkeypatch.setattr(ingest, "DB_PATH", database)
    assert ingest._rebuild_database([document]) == (1, 1)
    connection = sqlite3.connect(database)
    assert connection.execute("SELECT count(*) FROM attachments").fetchone()[0] == 2
    connection.close()
