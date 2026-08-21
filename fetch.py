import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import urllib.parse
import zipfile
from pathlib import Path

from common import (
    RAW, RULE_NAME, api_json, credential_free_url, http_get, redact_text, redact_url, require_oc,
)

LIST_URL = "https://www.law.go.kr/DRF/lawSearch.do"
SVC_URL = "https://www.law.go.kr/DRF/lawService.do"
VERSION_FIELDS = ("행정규칙일련번호", "시행일자", "발령일자", "발령번호", "행정규칙명")
ZIP_FORMATS = {"hwpx", "docx", "xlsx", "zip"}
CFB_SIGNATURE = bytes.fromhex("D0CF11E0A1B11AE1")


def version_metadata(item: dict) -> dict:
    return {field: item.get(field, "") for field in VERSION_FIELDS}


def attachment_role(name: str) -> str:
    normalized = name.lower()
    checks = (
        ("comparison", ("신구", "대비", "비교", "대조", "comparison")),
        ("transition", ("경과", "transition")),
        ("reason", ("개정이유", "제개정이유", "이유서", "reason")),
        ("qa", ("질의", "응답", "q&a", "qa", "문답")),
        ("annex", ("별지", "별첨", "부록", "붙임", "annex")),
        ("notice", ("고시", "공고", "notice")),
    )
    return next((role for role, terms in checks if any(term in normalized for term in terms)), "other")


def safe_filename(name: str, ordinal: int) -> str:
    base = Path(name).name or f"attachment-{ordinal}"
    base = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", base).strip(" .")
    return f"{ordinal:03d}_{base or f'attachment-{ordinal}'}"


def format_for_name(name: str) -> str:
    return Path(name).suffix.lower().lstrip(".") or "unknown"


def validate_payload(path: Path, fmt: str) -> tuple[int, str]:
    size = path.stat().st_size
    if not size:
        raise ValueError("첨부파일 내용이 비어 있습니다")
    with path.open("rb") as source:
        header = source.read(8)
    if fmt == "pdf" and not header.startswith(b"%PDF-"):
        raise ValueError("첨부파일의 서명이 .pdf 형식과 일치하지 않습니다")
    if fmt in ZIP_FORMATS:
        if not zipfile.is_zipfile(path):
            raise ValueError(f"첨부파일의 서명이 .{fmt} 형식과 일치하지 않습니다")
    if fmt == "hwp" and not header.startswith(CFB_SIGNATURE):
        raise ValueError("첨부파일의 서명이 .hwp 형식과 일치하지 않습니다")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return size, digest.hexdigest()


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                     prefix=".meta-", suffix=".tmp", delete=False) as tmp:
        json.dump(value, tmp, ensure_ascii=False, indent=1)
        tmp.write("\n")
        temp_name = tmp.name
    os.replace(temp_name, path)


def completed_meta_valid(meta_path: Path, item: dict) -> bool:
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("schema_version") != 1 or meta.get("complete") is not True:
            return False
        if meta.get("version") != version_metadata(item):
            return False
        attachments = meta.get("attachments")
        if not isinstance(attachments, list):
            return False
        for attachment in attachments:
            required = {"ordinal", "source_url", "original_name", "stored_name", "format",
                        "role", "size", "sha256", "status"}
            if set(attachment) != required or attachment["status"] != "complete":
                return False
            if credential_free_url(attachment["source_url"]) != attachment["source_url"]:
                return False
            stored_name = attachment["stored_name"]
            if not isinstance(stored_name, str) or Path(stored_name).name != stored_name:
                return False
            size, digest = validate_payload(meta_path.parent / stored_name,
                                            attachment["format"])
            if size != attachment["size"] or digest != attachment["sha256"]:
                return False
        return True
    except (OSError, ValueError, TypeError, json.JSONDecodeError, zipfile.BadZipFile):
        return False


def list_versions() -> list[dict]:
    oc = require_oc()
    versions: list[dict] = []
    for nw in (1, 2):
        page = 1
        while True:
            j = api_json(LIST_URL, {
                "OC": oc, "target": "admrul", "type": "JSON", "nw": nw,
                "query": RULE_NAME, "display": 100, "page": page, "sort": "efdes",
            })
            body = j.get("AdmRulSearch", {})
            page_items = body.get("admrul", [])
            if isinstance(page_items, dict):
                page_items = [page_items]
            items = [it for it in page_items if it.get("행정규칙명") == RULE_NAME]
            versions.extend(items)
            total = int(body.get("totalCnt", 0))
            if page * 100 >= total or not page_items:
                break
            page += 1
    seen: dict[str, dict] = {}
    for it in versions:
        seq = it["행정규칙일련번호"]
        if seq in seen and version_metadata(seen[seq]) != version_metadata(it):
            raise ValueError(f"원본 일련번호 {seq}의 메타데이터가 서로 다릅니다")
        seen.setdefault(seq, it)
    uniq = list(seen.values())
    uniq.sort(key=lambda x: (x["시행일자"], x["발령일자"], x["행정규칙일련번호"]))
    return uniq


def attachment_pairs(links: object, names: object) -> list[tuple[int, str, str]]:
    links = [links] if isinstance(links, str) else list(links or [])
    names = [names] if isinstance(names, str) else list(names or [])
    if len(links) != len(names):
        raise ValueError(f"첨부파일 링크와 이름의 개수가 다릅니다: 링크 {len(links)}개, 이름 {len(names)}개")
    unique: list[tuple[int, str, str]] = []
    seen = set()
    for ordinal, (link, name) in enumerate(zip(links, names), start=1):
        full_url = link if link.startswith(("http://", "https://")) else urllib.parse.urljoin("https://www.law.go.kr", link)
        parts = urllib.parse.urlsplit(full_url)
        host = (parts.hostname or "").casefold()
        if parts.scheme == "http" and (host == "law.go.kr" or host.endswith(".law.go.kr")):
            parts = parts._replace(scheme="https")
            full_url = urllib.parse.urlunsplit(parts)
        if parts.scheme != "https" or not (host == "law.go.kr" or host.endswith(".law.go.kr")):
            raise ValueError(f"신뢰할 수 없는 첨부파일 URL입니다: {redact_url(full_url)}")
        key = (credential_free_url(full_url), name)
        if key not in seen:
            seen.add(key)
            unique.append((ordinal, full_url, name))
    return unique


def download_attachment(vdir: Path, ordinal: int, url: str, name: str) -> tuple[dict, Path]:
    fmt = format_for_name(name)
    stored_name = safe_filename(name, ordinal)
    fd, temporary_name = tempfile.mkstemp(prefix=".download-", suffix=".tmp", dir=vdir)
    try:
        with os.fdopen(fd, "wb") as temporary:
            temporary.write(http_get(url))
        temporary = Path(temporary_name)
        size, digest = validate_payload(temporary, fmt)
    except Exception:
        Path(temporary_name).unlink(missing_ok=True)
        raise
    return {
        "ordinal": ordinal, "source_url": credential_free_url(url),
        "original_name": name, "stored_name": stored_name, "format": fmt,
        "role": attachment_role(name), "size": size, "sha256": digest, "status": "complete",
    }, temporary


def fetch_version(it: dict, reconcile: bool = False) -> None:
    oc = require_oc()
    seq = it["행정규칙일련번호"]
    vdir = RAW / f"{it['시행일자']}_{it['발령번호'].replace('/', '-')}"
    meta_p = vdir / "meta.json"
    if not reconcile and meta_p.exists() and completed_meta_valid(meta_p, it):
        return
    j = api_json(SVC_URL, {"OC": oc, "target": "admrul", "ID": seq, "type": "JSON"})
    svc = j.get("AdmRulService", {})
    att = svc.get("첨부파일", {}) or {}
    pairs = attachment_pairs(att.get("첨부파일링크", []), att.get("첨부파일명", []))
    vdir.mkdir(parents=True, exist_ok=True)
    staged: list[tuple[dict, Path]] = []
    try:
        for ordinal, link, name in pairs:
            staged.append(download_attachment(vdir, ordinal, link, name))
            time.sleep(0.3)
        for attachment, temporary in staged:
            os.replace(temporary, vdir / attachment["stored_name"])
        attachments = [attachment for attachment, _ in staged]
        atomic_json(meta_p, {
            "schema_version": 1, "complete": True, "version": version_metadata(it),
            "attachments": attachments,
        })
    finally:
        for _, temporary in staged:
            temporary.unlink(missing_ok=True)
    print(f"[완료] {it['시행일자']} 고시 제{it['발령번호']}호  첨부파일={len(attachments)}개")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="최신 N개 버전만")
    ap.add_argument("--reconcile", action="store_true", help="원격 첨부파일을 다시 내려받고 해시를 검증")
    args = ap.parse_args()

    versions = list_versions()
    if not versions:
        raise SystemExit("API에서 정확히 일치하는 고시 버전을 반환하지 않았습니다")
    print(f"연혁 총 {len(versions)}건 (시행 {versions[0]['시행일자']} ~ {versions[-1]['시행일자']})")
    atomic_json(RAW.parent / "versions.json", [version_metadata(version) for version in versions])

    targets = versions[-args.limit:] if args.limit else versions
    failures = []
    for i, it in enumerate(targets):
        try:
            fetch_version(it, reconcile=args.reconcile)
        except Exception as e:  # noqa: BLE001
            failures.append(it)
            print(f"[실패] {it['시행일자']} {it['발령번호']}: {redact_text(str(e))}", file=sys.stderr)
        time.sleep(0.2)
    if failures:
        raise SystemExit(f"{len(failures)}개 버전을 처리하지 못했습니다")


if __name__ == "__main__":
    main()
