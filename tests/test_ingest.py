from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import documents
import ingest
from documents import extract_hwpx


def _hwpx(path: Path, sections: dict[int, str]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for number, text in sections.items():
            archive.writestr(
                f"Contents/section{number}.xml",
                f'<root xmlns="urn:test"><p><t>{text}</t></p></root>',
            )


def test_select_renditions_prefers_hwpx_per_role_and_stem() -> None:
    attachments = [
        {"ordinal": 3, "original_name": "별지 1.PDF", "format": "pdf", "role": "annex"},
        {"ordinal": 2, "original_name": "별지-1.hwpx", "format": "hwpx", "role": "annex"},
        {"ordinal": 1, "original_name": "별지 1.pdf", "format": "pdf", "role": "notice"},
    ]
    assert [item["ordinal"] for item in ingest.select_renditions(attachments)] == [2, 1]


def test_hwpx_sections_are_read_in_numeric_order(tmp_path: Path) -> None:
    path = tmp_path / "source.hwpx"
    _hwpx(path, {10: "ten", 2: "two"})
    assert extract_hwpx(path) == "two\nten"


def test_hwp_extraction_keeps_table_paragraphs(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.hwp"
    source.write_bytes(b"fixture")

    def fake_run(command, **kwargs):
        output = Path(command[command.index("--output") + 1])
        (output / "index.xhtml").write_text(
            '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
            "<table><tr><td><p>[219]</p><p>성분명(품명: 제품)</p>"
            "<p>급여 기준</p></td></tr></table></body></html>",
            encoding="utf-8",
        )

    monkeypatch.setattr(documents.subprocess, "run", fake_run)
    assert documents.extract_hwp(source).splitlines() == [
        "[219]", "성분명(품명: 제품)", "급여 기준",
    ]


def test_split_blocks_rejects_gu_bun_pseudo_item() -> None:
    text = "[142]\n구분\n설명\n[143]\n성분명(품명: 제품)\n급여 기준"
    blocks = ingest.split_blocks(text)
    assert len(blocks) == 1
    assert blocks[0]["class_no"] == "143"


def test_normalized_output_is_deterministic(tmp_path: Path, monkeypatch) -> None:
    raw = tmp_path / "raw" / "20260101_1"
    raw.mkdir(parents=True)
    source = raw / "annex.hwpx"
    _hwpx(source, {1: "[142]\n성분명(품명: 제품)\n급여 기준"})
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    meta = {
        "schema_version": 1,
        "complete": True,
        "version": {"행정규칙일련번호": "1", "시행일자": "20260101", "발령일자": "20251231", "발령번호": "1", "행정규칙명": "약제"},
        "attachments": [{"ordinal": 1, "source_url": "https://example.test/annex", "original_name": "annex.hwpx", "stored_name": "annex.hwpx", "format": "hwpx", "role": "annex", "size": source.stat().st_size, "sha256": digest, "status": "complete"}],
    }
    (raw / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(ingest, "NORMALIZED", tmp_path / "normalized")
    first = ingest._parse_version(raw, ingest._validate_meta(raw / "meta.json"))
    contents = (tmp_path / "normalized" / "1.json").read_bytes()
    second = ingest._parse_version(raw, ingest._validate_meta(raw / "meta.json"))
    assert first == second
    assert contents == (tmp_path / "normalized" / "1.json").read_bytes()
