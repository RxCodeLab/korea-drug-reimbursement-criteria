import hashlib
import json

import pytest

import fetch
from common import credential_free_url, redact_url
from fetch import attachment_pairs, attachment_role, completed_meta_valid


def test_urls_redact_or_remove_query_secrets():
    url = "https://example.test/file?OC=secret@example.test&token=abc&safe=1"
    assert credential_free_url(url) == "https://example.test/file?safe=1"
    assert "secret@example.test" not in redact_url(url)
    assert "abc" not in redact_url(url)


@pytest.mark.parametrize(("name", "expected"), [
    ("별지 제1호.hwp", "annex"),
    ("신구 대비표.pdf", "comparison"),
    ("경과조치.hwp", "transition"),
    ("개정이유서.pdf", "reason"),
    ("질의응답.hwp", "qa"),
    ("고시문.pdf", "notice"),
    ("붙임_성인 ADHD 소견서.hwp", "other"),
    ("첨부자료.txt", "other"),
])
def test_attachment_role_is_deterministic(name, expected):
    assert attachment_role(name) == expected


def test_attachment_pairs_rejects_mismatched_cardinality():
    with pytest.raises(ValueError, match="링크와 이름의 개수가 다릅니다"):
        attachment_pairs(["/one", "/two"], ["one.pdf"])


def test_attachment_pairs_rejects_external_hosts():
    with pytest.raises(ValueError, match="신뢰할 수 없는 첨부파일 URL"):
        attachment_pairs(["https://example.test/file.pdf"], ["notice.pdf"])


def test_attachment_pairs_deduplicates_exact_repeats_with_first_ordinal():
    assert attachment_pairs(
        ["/file?OC=secret", "/file?OC=secret"], ["notice.pdf", "notice.pdf"],
    ) == [(1, "https://www.law.go.kr/file?OC=secret", "notice.pdf")]


def test_completed_meta_validation_rejects_corrupt_payload(tmp_path):
    version = {
        "행정규칙일련번호": "1", "시행일자": "20260101", "발령일자": "20251201",
        "발령번호": "1", "행정규칙명": "name",
    }
    payload = b"%PDF-1.7\ncontents"
    file_path = tmp_path / "001_notice.pdf"
    file_path.write_bytes(payload)
    meta = {
        "schema_version": 1,
        "complete": True,
        "version": version,
        "attachments": [{
            "ordinal": 1, "source_url": "https://example.test/file", "original_name": "notice.pdf",
            "stored_name": file_path.name, "format": "pdf", "role": "notice", "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(), "status": "complete",
        }],
    }
    meta_path = tmp_path / "meta.json"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    assert completed_meta_valid(meta_path, version)
    file_path.write_bytes(b"not a PDF")
    assert not completed_meta_valid(meta_path, version)


def test_fetch_skips_only_a_revalidated_completed_meta(tmp_path, monkeypatch):
    version = {
        "행정규칙일련번호": "1", "시행일자": "20260101", "발령일자": "20251201",
        "발령번호": "1", "행정규칙명": "name",
    }
    version_dir = tmp_path / "20260101_1"
    version_dir.mkdir()
    payload = b"%PDF-1.7\\ncontents"
    attachment = version_dir / "001_notice.pdf"
    attachment.write_bytes(payload)
    (version_dir / "meta.json").write_text(json.dumps({
        "schema_version": 1, "complete": True, "version": version,
        "attachments": [{
            "ordinal": 1, "source_url": "https://example.test/file",
            "original_name": "notice.pdf", "stored_name": attachment.name, "format": "pdf",
            "role": "notice", "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
            "status": "complete",
        }],
    }), encoding="utf-8")
    monkeypatch.setattr(fetch, "RAW", tmp_path)
    monkeypatch.setattr(fetch, "require_oc", lambda: "unused")
    monkeypatch.setattr(fetch, "api_json", lambda *args, **kwargs: pytest.fail("should not fetch"))
    fetch.fetch_version(version)
