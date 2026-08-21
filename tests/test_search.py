import json

import pytest

import build_site
import search


def normalized_document():
    return {
        "schema_version": 1,
        "complete": True,
        "version": {
            "시행일자": "20250101",
            "발령번호": "2024-1",
            "발령일자": "20241231",
            "행정규칙일련번호": "seq-1",
            "행정규칙명": "test",
        },
        "attachments": [{
            "ordinal": 1,
            "original_name": "별지.hwpx",
            "sha256": "a" * 64,
        }],
        "entries": [{
            "action": "변경",
            "class_no": "219",
            "class_header": "[219] 기타",
            "title": "Dapagliflozin 경구제",
            "body": "다파글리플로진 급여기준",
            "attachment_ordinal": 1,
            "attachment_sha256": "a" * 64,
            "block_identity": "[219]dapagliflozin경구제",
        }],
    }


def test_highlight_escapes_html_before_marking():
    rendered = search.highlight("<script>약제</script>", ["약제"])
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered
    assert "<mark>약제</mark>" in rendered


def test_static_index_is_deterministic_and_contains_provenance(tmp_path, monkeypatch):
    normalized = tmp_path / "normalized"
    public = tmp_path / "public"
    normalized.mkdir()
    document = normalized_document()
    (normalized / "seq-1.json").write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(build_site, "NORMALIZED", normalized)
    monkeypatch.setattr(build_site, "PUBLIC", public)

    build_site.main()
    first = (public / "search-index.json").read_bytes()
    build_site.main()
    second = (public / "search-index.json").read_bytes()

    assert first == second
    row = json.loads(first)[0]
    assert row["sequence"] == "seq-1"
    assert row["source_sha256"] == "a" * 64
    assert "다파글리플로진" in row["body"]


def test_query_reports_missing_database(tmp_path, monkeypatch):
    monkeypatch.setattr(search, "DB_PATH", tmp_path / "missing.db")
    with pytest.raises(RuntimeError, match="검색 DB가 없습니다"):
        search.query(["test"])
