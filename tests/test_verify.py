from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import documents
import ingest
import verify

DIGEST = "b" * 64


def _attachment(ordinal: int, role: str, status: str = "complete") -> dict:
    return {
        "ordinal": ordinal,
        "source_url": f"https://www.law.go.kr/file/{ordinal}",
        "original_name": f"{role}.hwpx",
        "stored_name": f"{role}.hwpx",
        "format": "hwpx",
        "role": role,
        "size": 10,
        "sha256": DIGEST,
        "status": "complete",
        "parser_version": documents.PARSER_VERSION,
        "parser_status": status,
    }


def _entry(title: str, body: str, ordinal: int = 1, class_no: str = "142") -> dict:
    return {
        "action": "신설",
        "class_no": class_no,
        "class_header": f"[{class_no}] 분류",
        "title": title,
        "body": body,
        "attachment_ordinal": ordinal,
        "attachment_sha256": DIGEST,
        "block_identity": ingest.norm_title(class_no, title),
    }


def _document(entries: list[dict], attachments: list[dict] | None = None) -> dict:
    return {
        "schema_version": 1,
        "complete": True,
        "version": {"시행일자": "20260901", "발령번호": "2026-176", "발령일자": "20260828",
                    "행정규칙일련번호": "2100000284388", "행정규칙명": "약제"},
        "attachments": attachments or [_attachment(1, "annex")],
        "entries": entries,
    }


def test_boundary_check_rejects_body_swallowed_into_title() -> None:
    title = "Sirolimus시럽제 (품명:라파뮨 시럽) " + "식품의약품안전처장이 인정한 범위 내에서 투여 시 약값 전액을 환자가 부담토록 함. " * 4
    errors = verify.entry_boundary_errors(_document([_entry(title, "본문")]))
    assert len(errors) == 1 and "200자" in errors[0]


def test_boundary_check_rejects_foreign_item_header_inside_body() -> None:
    body = "식품의약품안전처장이 인정한 범위\n[222]\nBudesonide 흡입제\n허가사항 범위 내에서"
    errors = verify.entry_boundary_errors(_document([_entry("Sirolimus시럽제 (품명:라파뮨 시럽)", body)]))
    assert len(errors) == 1 and "[222]" in errors[0]


def test_boundary_check_rejects_required_attachment_without_entries() -> None:
    attachments = [_attachment(1, "annex"), _attachment(2, "notice"), _attachment(3, "qa")]
    errors = verify.entry_boundary_errors(_document([_entry("A(품명: 가)", "본문", ordinal=1)], attachments))
    assert len(errors) == 1 and "notice.hwpx" in errors[0]
    not_selected = [_attachment(1, "annex"), _attachment(2, "notice", status="not_selected")]
    assert verify.entry_boundary_errors(_document([_entry("A(품명: 가)", "본문")], not_selected)) == []


def test_boundary_check_accepts_clean_document() -> None:
    document = _document([
        _entry("Sirolimus시럽제 (품명:라파뮨 시럽)", "식품의약품안전처장이 인정한 범위\n - 장기이식거부 반응"),
        _entry("Budesonide 흡입제", "허가사항 범위 내에서 인정함.", class_no="222"),
    ])
    assert verify.entry_boundary_errors(document) == []


@pytest.fixture
def built_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict[str, dict]]:
    document = _document([
        _entry("Sirolimus시럽제 (품명:라파뮨 시럽)", "식품의약품안전처장이 인정한 범위"),
        _entry("Budesonide 흡입제", "허가사항 범위 내에서 인정함.", class_no="222"),
    ])
    database = tmp_path / "criteria.db"
    monkeypatch.setattr(ingest, "DB_PATH", database)
    monkeypatch.setattr(verify, "DB_PATH", database)
    ingest._rebuild_database([document])
    return database, {document["version"]["행정규칙일련번호"]: document}


def test_database_check_accepts_matching_index(built_database: tuple[Path, dict[str, dict]]) -> None:
    _, normalized = built_database
    assert verify.validate_database(normalized) == []


def test_database_check_rejects_entry_set_mismatch(built_database: tuple[Path, dict[str, dict]]) -> None:
    _, normalized = built_database
    (document,) = normalized.values()
    document["entries"].append(_entry("Extra(품명: 추가)", "본문", class_no="259"))
    errors = verify.validate_database(normalized)
    assert len(errors) == 1 and "정규화 항목 3개" in errors[0]


def test_database_check_rejects_stale_fts_index(built_database: tuple[Path, dict[str, dict]]) -> None:
    """외부 content FTS는 count(*)로는 잡히지 않는 색인 누락을 잡아야 한다."""
    database, normalized = built_database
    con = sqlite3.connect(database)
    con.execute("DELETE FROM fts_data")
    con.execute("DELETE FROM fts_idx")
    con.execute("DELETE FROM fts_docsize")
    con.commit()
    con.close()
    errors = verify.validate_database(normalized)
    assert errors and all("FTS" in error or "SQLite" in error for error in errors)


def test_database_check_leaves_original_untouched(built_database: tuple[Path, dict[str, dict]]) -> None:
    """검증 관문은 검사 대상 DB를 쓰기 모드로 열지 않는다(임시 사본 검사)."""
    database, normalized = built_database
    before = database.read_bytes()
    mtime = database.stat().st_mtime_ns
    assert verify.validate_database(normalized) == []
    assert database.read_bytes() == before and database.stat().st_mtime_ns == mtime
    assert not list(database.parent.glob("*.db-*"))
