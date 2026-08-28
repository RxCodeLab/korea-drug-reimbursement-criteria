import hashlib
import json

import pytest

import fetch_law
from common import credential_free_url, redact_url
from fetch_law import attachment_pairs, attachment_role, completed_meta_valid, filter_versions
from common import parse_changes_since


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
    file_path.write_bytes(payload)
    meta["attachments"][0]["role"] = "annex"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
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
    monkeypatch.setattr(fetch_law, "RAW", tmp_path)
    monkeypatch.setattr(fetch_law, "require_oc", lambda: "unused")
    monkeypatch.setattr(fetch_law, "api_json", lambda *args, **kwargs: pytest.fail("should not fetch"))
    fetch_law.fetch_version(version)


def _version_with_effective(effective):
    return {
        "행정규칙일련번호": "1", "시행일자": effective, "발령일자": "20200101",
        "발령번호": "1", "행정규칙명": "name",
    }


@pytest.mark.parametrize("value", ["19990101", "20180101", "20200101"])
def test_parse_changes_since_accepts_eight_digit_past_dates(value):
    assert parse_changes_since(value) == value


@pytest.mark.parametrize("value", ["2020-01-01", "202001", "abcdefgh", "20200101x"])
def test_parse_changes_since_rejects_invalid_format(value):
    with pytest.raises(ValueError, match="YYYYMMDD"):
        parse_changes_since(value)


def test_parse_changes_since_rejects_future_dates():
    with pytest.raises(ValueError, match="오늘 이후"):
        parse_changes_since("99991231")


def test_filter_versions_changes_since_is_inclusive():
    versions = [_version_with_effective(d) for d in ("20200101", "20200201", "20200301")]
    assert [v["시행일자"] for v in filter_versions(versions, "20200201")] == [
        "20200201", "20200301",
    ]


def test_filter_versions_without_changes_since_keeps_all():
    versions = [_version_with_effective(d) for d in ("20200101", "20200201")]
    assert filter_versions(versions, None) == versions
    assert filter_versions(versions, "") == versions


def test_fetch_reconcile_refetches_even_when_meta_is_valid(tmp_path, monkeypatch):
    version = _version_with_effective("20260101")
    version_dir = tmp_path / "20260101_1"
    version_dir.mkdir()
    payload = b"%PDF-1.7\ncontents"
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
    monkeypatch.setattr(fetch_law, "RAW", tmp_path)
    monkeypatch.setattr(fetch_law, "require_oc", lambda: "unused")
    calls = []

    def fake_api_json(url, params):
        calls.append((url, params))
        return {"AdmRulService": {}}

    monkeypatch.setattr(fetch_law, "api_json", fake_api_json)
    fetch_law.fetch_version(version, reconcile=True)
    assert len(calls) == 1
    url, params = calls[0]
    assert url == fetch_law.SVC_URL
    assert params["ID"] == "1"
