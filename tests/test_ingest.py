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


def test_rhwp_reconstructs_nested_table_inside_criterion_body() -> None:
    nested = {
        "rows": 2,
        "cols": 3,
        "cellCount": 6,
        # containerPath의 문단 번호가 표가 있던 자리(빈 줄)를 가리킨다
        "containerPath": [{"kind": "tableCell", "cell": 3, "control": 0, "paragraph": 1}],
        "cells": [
            {"row": 0, "col": 0, "rowSpan": 1, "colSpan": 1, "isHeader": True, "text": "구 분"},
            {"row": 0, "col": 1, "rowSpan": 1, "colSpan": 1, "isHeader": True, "text": "Metformin"},
            {"row": 0, "col": 2, "rowSpan": 1, "colSpan": 1, "isHeader": True, "text": "SGLT-2 inhibitor"},
            {"row": 1, "col": 0, "rowSpan": 1, "colSpan": 1, "isHeader": False, "text": "Metformin"},
            {"row": 1, "col": 1, "rowSpan": 1, "colSpan": 1, "isHeader": False, "text": ""},
            {"row": 1, "col": 2, "rowSpan": 1, "colSpan": 1, "isHeader": False, "text": "인정"},
        ],
    }
    table = {
        "rows": 2,
        "cols": 2,
        "cellCount": 4,
        "cells": [
            {"row": 0, "col": 0, "rowSpan": 1, "colSpan": 1, "isHeader": True, "text": "구 분"},
            {"row": 0, "col": 1, "rowSpan": 1, "colSpan": 1, "isHeader": True, "text": "세부인정기준 및 방법"},
            {"row": 1, "col": 0, "rowSpan": 1, "colSpan": 1, "isHeader": False, "text": "[일반원칙]\n당뇨병용제"},
            {
                "row": 1,
                "col": 1,
                "rowSpan": 1,
                "colSpan": 1,
                "isHeader": False,
                "text": "(3) 인정 가능 2제 요법\n\n(4) 다음 조건",
                "nested": [nested],
            },
        ],
    }

    lines = documents._table_lines(table)

    assert lines[0] == "[일반원칙]\n당뇨병용제"
    # 앵커 문단(빈 줄) 자리에 표가 들어가 원문 위치를 유지한다
    assert "(3) 인정 가능 2제 요법\n[표]" in lines[1]
    assert lines[1].index("[표]") < lines[1].index("(4) 다음 조건")
    # 표시형 매트릭스는 조합별로 전개해 원문 의도를 보존한다
    assert "Metformin + SGLT-2 inhibitor: 인정" in lines[1]
    assert "Metformin + Metformin" not in lines[1]  # 빈 셀(자기 조합)은 전개하지 않는다


def test_rhwp_expands_span_matrix_with_compound_headers() -> None:
    """2단 헤더·병합 라벨 매트릭스가 원문 조합 그대로 전개되어야 한다."""
    cells = [
        {"row": 0, "col": 0, "rowSpan": 2, "colSpan": 2, "isHeader": True, "text": "구 분"},
        {"row": 0, "col": 2, "rowSpan": 2, "colSpan": 1, "isHeader": True, "text": "Metformin"},
        {"row": 0, "col": 3, "rowSpan": 1, "colSpan": 2, "isHeader": True, "text": "SGLT-2 inhibitor"},
        {"row": 1, "col": 3, "rowSpan": 1, "colSpan": 1, "isHeader": True, "text": "dapagliflozin"},
        {"row": 1, "col": 4, "rowSpan": 1, "colSpan": 1, "isHeader": True, "text": "ipragliflozin"},
        {"row": 2, "col": 0, "rowSpan": 1, "colSpan": 2, "isHeader": False, "text": "Metformin"},
        {"row": 2, "col": 2, "rowSpan": 1, "colSpan": 1, "isHeader": False, "text": ""},
        {"row": 2, "col": 3, "rowSpan": 1, "colSpan": 1, "isHeader": False, "text": "인정"},
        {"row": 2, "col": 4, "rowSpan": 1, "colSpan": 1, "isHeader": False, "text": ""},
        {"row": 3, "col": 0, "rowSpan": 2, "colSpan": 1, "isHeader": False, "text": "SGLT-2 inhibitor"},
        {"row": 3, "col": 1, "rowSpan": 1, "colSpan": 1, "isHeader": False, "text": "dapagliflozin"},
        {"row": 3, "col": 2, "rowSpan": 1, "colSpan": 1, "isHeader": False, "text": "인정"},
        {"row": 4, "col": 1, "rowSpan": 1, "colSpan": 1, "isHeader": False, "text": "ipragliflozin"},
        {"row": 4, "col": 2, "rowSpan": 1, "colSpan": 1, "isHeader": False, "text": "인정"},
    ]
    table = {"rows": 5, "cols": 5, "cellCount": len(cells), "cells": cells}

    rendered = documents._render_table_grid(table)

    assert rendered.splitlines() == [
        "[표]",
        "Metformin + SGLT-2 inhibitor dapagliflozin: 인정",
        "SGLT-2 inhibitor dapagliflozin + Metformin: 인정",
        "SGLT-2 inhibitor ipragliflozin + Metformin: 인정",
    ]


def test_rhwp_keeps_list_table_with_short_sublabels_as_grid() -> None:
    """짧은 부분류 라벨(DHP, loop 등)이 있는 목록 표는 전개하지 않고 격자를 유지한다."""
    cells = [
        {"row": 0, "col": 0, "rowSpan": 1, "colSpan": 2, "isHeader": True, "text": "성분군"},
        {"row": 0, "col": 2, "rowSpan": 1, "colSpan": 1, "isHeader": True, "text": "성분명"},
        {"row": 1, "col": 0, "rowSpan": 2, "colSpan": 1, "isHeader": False, "text": "칼슘 채널 차단제"},
        {"row": 1, "col": 1, "rowSpan": 1, "colSpan": 1, "isHeader": False, "text": "DHP"},
        {"row": 1, "col": 2, "rowSpan": 1, "colSpan": 1, "isHeader": False, "text": "Amlodipine 등"},
        {"row": 2, "col": 1, "rowSpan": 1, "colSpan": 1, "isHeader": False, "text": "non-DHP"},
        {"row": 2, "col": 2, "rowSpan": 1, "colSpan": 1, "isHeader": False, "text": "Diltiazem 등"},
        {"row": 3, "col": 0, "rowSpan": 1, "colSpan": 2, "isHeader": False, "text": "혈관확장제"},
        {"row": 3, "col": 2, "rowSpan": 1, "colSpan": 1, "isHeader": False, "text": "Cadralazine 등"},
    ]
    table = {"rows": 4, "cols": 3, "cellCount": len(cells), "cells": cells}

    rendered = documents._render_table_grid(table).splitlines()

    assert rendered == [
        "[표]",
        "성분군 | 성분명",
        "칼슘 채널 차단제 | DHP | Amlodipine 등",  # rowSpan 라벨은 행마다 복원
        "칼슘 채널 차단제 | non-DHP | Diltiazem 등",
        "혈관확장제 | Cadralazine 등",  # colSpan 중복은 한 번만
    ]


def test_rhwp_renders_plain_list_table_as_grid() -> None:
    """표시형이 아닌 목록 표는 격자 그대로 유지한다."""
    cells = [
        {"row": 0, "col": 0, "rowSpan": 1, "colSpan": 1, "isHeader": True, "text": "성분군"},
        {"row": 0, "col": 1, "rowSpan": 1, "colSpan": 1, "isHeader": True, "text": "성분명"},
        {"row": 1, "col": 0, "rowSpan": 1, "colSpan": 1, "isHeader": False, "text": "Biguanide계"},
        {"row": 1, "col": 1, "rowSpan": 1, "colSpan": 1, "isHeader": False, "text": "Metformin HCl"},
    ]
    table = {"rows": 2, "cols": 2, "cellCount": len(cells), "cells": cells}

    rendered = documents._render_table_grid(table)

    assert rendered == "[표]" + chr(10) + "성분군 | 성분명" + chr(10) + "Biguanide계 | Metformin HCl"


def test_rhwp_keeps_nested_tables_with_their_owning_cells() -> None:
    """서로 다른 셀의 중첩 표는 각자 자기 셀 뒤에 남아야 한다."""
    def small(text):
        return {
            "rows": 1, "cols": 1, "cellCount": 1,
            "cells": [{"row": 0, "col": 0, "rowSpan": 1, "colSpan": 1,
                       "isHeader": False, "text": text}],
        }
    table = {
        "rows": 2, "cols": 1, "cellCount": 2,
        "cells": [
            {"row": 0, "col": 0, "rowSpan": 1, "colSpan": 1, "isHeader": False,
             "text": "첫 셀", "nested": [small("첫 표")]},
            {"row": 1, "col": 0, "rowSpan": 1, "colSpan": 1, "isHeader": False,
             "text": "둘째 셀", "nested": [small("둘째 표")]},
        ],
    }

    lines = documents._table_lines(table)

    assert lines == ["첫 셀", "[표]\n첫 표", "둘째 셀", "[표]\n둘째 표"]


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


def test_split_blocks_moves_general_principle_subtitle_into_title_and_removes_page_headers() -> None:
    text = "\n".join([
        "[일반원칙]",
        "경구용 항혈전제",
        "(항혈소판제 및",
        "Heparinoid 제제)",
        "각 약제의 기준",
        "- 7 -",
        "구  분",
        "세부인정기준 및 방법",
        "[일반원칙]",
        "경구용 항혈전제",
        "계속되는 기준",
    ])

    (block,) = ingest.split_blocks(text)

    assert block["title"] == "경구용 항혈전제 (항혈소판제 및 Heparinoid 제제)"
    assert block["class_header"] == "[일반원칙] 경구용 항혈전제 (항혈소판제 및 Heparinoid 제제)"
    assert block["body"] == "각 약제의 기준\n계속되는 기준"


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


REAL_ANNEX_PDF = Path(__file__).resolve().parent.parent / "data" / "raw" / "20260901_2026-176" / "002_별지. 신설 및 변경 급여기준.pdf"


def test_real_2026_176_annex_separates_sirolimus_from_copd_inhaler() -> None:
    """2026-176 고시 원문은 `(품명∶라파뮨` 과 `[222］` 처럼 전각 구분 기호를 쓴다.

    이 문자를 놓치면 Sirolimus 제목에 본문이 들어가고 Budesonide 복합제 기준이
    Sirolimus 본문에 합쳐진다(실제 배포됐던 오염). 원문 파일이 없으면 실패한다.
    """
    assert REAL_ANNEX_PDF.is_file(), f"회귀 원문이 없습니다: {REAL_ANNEX_PDF}"
    blocks = ingest.split_blocks(documents.extract_document(REAL_ANNEX_PDF, "pdf"))
    (sirolimus,) = [block for block in blocks if block["title"].startswith("Sirolimus")]
    (inhaler,) = [block for block in blocks if block["title"].startswith("Budesonide +Formoterol")]
    assert (sirolimus["class_no"], inhaler["class_no"]) == ("142", "222")
    assert sirolimus["title"] == "Sirolimus시럽제 (품명:라파뮨 시럽)"
    assert sirolimus["class_header"] == "[142] 자격료법제(비특이성 면역원제포함)"
    assert sirolimus["body"].startswith("식품의약품안전처장이 인정한 범위 내에서 투여 시 약값 전액을")
    assert "Budesonide" not in sirolimus["body"]
    assert "[222]" not in sirolimus["body"]
    assert inhaler["title"] == "Budesonide +Formoterol fumarate + Glycopyrronium bromide 흡입제"
    assert inhaler["class_header"] == "[222] 진해거담제"
    assert inhaler["body"].startswith("허가사항범위 내에서아래와같은 기준으로투여 시 요양급여를")
    assert all(len(block["title"]) <= 200 for block in blocks)


def test_split_blocks_normalizes_fullwidth_punctuation_before_matching() -> None:
    text = "\n".join([
        "［142］ 자격료법제",
        "［142］",
        "Sirolimus시럽제",
        "（품명∶라파뮨",
        "시럽）",
        "약값 전액을 환자가 부담토록 함.",
        "［222］",
        "Budesonide 흡입제",
        "허가사항 범위 내에서 인정함.",
    ])
    blocks = ingest.split_blocks(text)
    assert [(b["class_no"], b["title"]) for b in blocks] == [
        ("142", "Sirolimus시럽제 (품명:라파뮨 시럽)"),
        ("222", "Budesonide 흡입제"),
    ]
    assert blocks[0]["body"] == "약값 전액을 환자가 부담토록 함."
    assert blocks[0]["class_header"] == "[142] 자격료법제"
    assert ingest.norm_title("142", blocks[0]["title"]) == ingest.norm_title("142", "Sirolimus시럽제 (품명:라파뮨 시럽)")


def test_split_blocks_stops_title_at_body_opening_line() -> None:
    """품명 없이 제목이 여러 줄이면 본문 첫 줄('허가사항…')은 제목에 붙지 않는다."""
    text = "\n".join([
        "[259] 기타의 비뇨생식기관 및 항문용약",
        "[259]",
        "만성폐쇄성폐질환",
        "흡입용 치료제",
        "허가사항 범위 내에서 아래와 같은 기준으로 투여 시 요양급여를 인정함.",
        "- 아 래 -",
        "○ 조건",
    ])
    (block,) = ingest.split_blocks(text)
    assert block["title"] == "만성폐쇄성폐질환 흡입용 치료제"
    assert block["body"].startswith("허가사항 범위 내에서")


def test_split_blocks_assigns_class_header_by_class_number() -> None:
    """분류 헤더가 한 쪽에 여럿 나열되면 각 항목은 자기 분류번호의 헤더를 가진다."""
    text = "\n".join([
        "[142] 자격료법제",
        "[222] 진해거담제",
        "[142]",
        "A(품명: 가)",
        "본문 A",
        "[222]",
        "B(품명: 나)",
        "본문 B",
    ])
    first, second = ingest.split_blocks(text)
    assert first["class_header"] == "[142] 자격료법제"
    assert second["class_header"] == "[222] 진해거담제"


def test_norm_title_folds_compatibility_characters_into_one_identity() -> None:
    """Ⅷ와 VIII로 표기만 다른 같은 약제가 두 이력으로 갈라지지 않는다(실데이터에서 1쌍 발견)."""
    assert ingest.norm_title("339", "Recombinant blood coagulation factor Ⅷ 주사제 (품명: 애드베이트주 등)") == \
        ingest.norm_title("339", "Recombinant blood coagulation factor VIII 주사제 (품명: 애드베이트주)")
    assert ingest.norm_title("131", "Dexamethasone 700㎍ 이식제") == "[131]dexamethasone700μg이식제"


def test_next_class_header_does_not_leak_into_previous_body() -> None:
    """다음 분류의 헤더 줄('[222] 진해거담제')이 직전 항목 본문 끝에 붙지 않는다(배포 페이지 367개 중 295개에 있던 결함)."""
    text = "\n".join([
        "[142] 자격료법제(비특이성 면역원제포함)",
        "[142]",
        "Sirolimus 시럽제 (품명:라파뮨 시럽)",
        "약값 전액을 환자가 부담토록 함.",
        " - 장기이식거부 반응",
        "[222] 진해거담제",
        "구  분",
        "세부인정기준 및 방법",
        "[222]",
        "Budesonide 흡입제",
        "허가사항 범위 내에서 인정함.",
    ])
    first, second = ingest.split_blocks(text)
    assert first["body"] == "약값 전액을 환자가 부담토록 함.\n - 장기이식거부 반응"
    assert second["class_header"] == "[222] 진해거담제"
    assert second["body"] == "허가사항 범위 내에서 인정함."


def test_split_blocks_accepts_class_number_and_title_on_one_line() -> None:
    """'[113] Cannabidiol (품명: …)'처럼 번호와 제목이 한 줄이면 분류 헤더가 아니라 항목이다(예전엔 조용히 사라짐)."""
    text = "\n".join([
        "[113] 항전간제",
        "[113] Cannabidiol (품명: 에피디올렉스 내복액)",
        "식품의약품안전처장이 인정한 범위 내에서 인정함.",
        "[114] 해열진통소염제",
        "[114] Fremanezumab 주사제 (품명: 아조비오토인젝터주,",
        "아조비프리필드시린지주)",
        "허가사항 범위 내에서 인정함.",
    ])
    first, second = ingest.split_blocks(text)
    assert (first["class_no"], first["title"], first["class_header"]) == ("113", "Cannabidiol (품명: 에피디올렉스 내복액)", "[113] 항전간제")
    assert first["body"] == "식품의약품안전처장이 인정한 범위 내에서 인정함."
    assert second["title"] == "Fremanezumab 주사제 (품명: 아조비오토인젝터주, 아조비프리필드시린지주)"
    assert second["body"] == "허가사항 범위 내에서 인정함."
