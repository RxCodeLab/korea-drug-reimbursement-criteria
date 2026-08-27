from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import urllib.parse
from pathlib import Path

from common import DATA, DB_PATH, RAW

NORMALIZED = DATA / "normalized"
MFDS_ITEMS = DATA / "mfds" / "items"
SENSITIVE_KEYS = {"oc", "law_oc", "api_key", "apikey", "key", "token", "access_token"}


def digest(path: Path) -> tuple[int, str]:
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            size += len(block)
            h.update(block)
    return size, h.hexdigest()


def safe_url(url: str) -> bool:
    return not any(key.casefold() in SENSITIVE_KEYS for key, _ in urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))


def validate_mfds_items(items_dir: Path = MFDS_ITEMS) -> list[str]:
    errors: list[str] = []
    for path in sorted(items_dir.glob("*.json")) if items_dir.exists() else []:
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
            sequence = str(item["item_seq"])
            if item.get("schema_version") != 1 or item.get("complete") is not True:
                raise ValueError("schema-v1 MFDS 품목 형식에 맞지 않습니다")
            if path.stem != sequence:
                raise ValueError("파일명과 품목기준코드가 다릅니다")
            if not safe_url(item["source_url"]):
                raise ValueError("출처 URL에 인증정보가 들어 있습니다")
            revisions = item["revisions"]
            if not isinstance(revisions, list) or not revisions:
                raise ValueError("효능·효과 관찰 이력이 없습니다")
            seen: set[str] = set()
            for revision in revisions:
                text = revision["ee_text"]
                content_hash = revision["content_sha256"]
                if hashlib.sha256(text.encode("utf-8")).hexdigest() != content_hash:
                    raise ValueError("효능·효과 SHA-256이 일치하지 않습니다")
                if revision["revision_id"] != f"{sequence}-{content_hash[:8]}":
                    raise ValueError("허가사항 개정 식별자가 일치하지 않습니다")
                if content_hash in seen:
                    raise ValueError("같은 효능·효과 개정이 중복되었습니다")
                seen.add(content_hash)
                if not revision["first_observed_at"] or not revision["last_observed_at"]:
                    raise ValueError("관찰 시각이 없습니다")
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"{path}: {exc}")
    return errors


def validate() -> list[str]:
    errors: list[str] = []
    metas: dict[str, dict] = {}
    for meta_path in sorted(RAW.glob("*/meta.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if meta.get("schema_version") != 1 or meta.get("complete") is not True:
                raise ValueError("schema-v1 매니페스트 형식에 맞지 않습니다")
            version = meta["version"]
            sequence = version["행정규칙일련번호"]
            if sequence in metas:
                raise ValueError(f"일련번호가 중복되었습니다: {sequence}")
            attachments = meta["attachments"]
            if not attachments:
                raise ValueError("첨부파일이 없습니다")
            ordinals: set[int] = set()
            for attachment in attachments:
                ordinal = attachment["ordinal"]
                if ordinal in ordinals:
                    raise ValueError(f"첨부파일 순번이 중복되었습니다: {ordinal}")
                ordinals.add(ordinal)
                if attachment.get("status") != "complete" or not safe_url(attachment["source_url"]):
                    raise ValueError(f"안전하지 않거나 완료되지 않은 첨부파일입니다: {ordinal}")
                file_path = meta_path.parent / attachment["stored_name"]
                size, sha256 = digest(file_path)
                if size != attachment["size"] or sha256 != attachment["sha256"]:
                    raise ValueError(f"첨부파일 SHA-256이 일치하지 않습니다: {ordinal}")
            metas[sequence] = meta
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"{meta_path}: {exc}")

    normalized: dict[str, dict] = {}
    for path in sorted(NORMALIZED.glob("*.json")) if NORMALIZED.exists() else []:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            sequence = document["version"]["행정규칙일련번호"]
            if document.get("complete") is not True or sequence not in metas:
                raise ValueError("원본이 없거나 내용이 온전하지 않은 정규화 문서입니다")
            source = {a["ordinal"]: a["sha256"] for a in metas[sequence]["attachments"]}
            parsed = {a["ordinal"]: a for a in document["attachments"]}
            if set(source) != set(parsed):
                raise ValueError("첨부파일 출처 집합이 일치하지 않습니다")
            if any(parsed[o]["sha256"] != sha for o, sha in source.items()):
                raise ValueError("첨부파일 출처 SHA-256이 일치하지 않습니다")
            for entry in document["entries"]:
                if source.get(entry["attachment_ordinal"]) != entry["attachment_sha256"]:
                    raise ValueError("항목의 출처 정보가 일치하지 않습니다")
            normalized[sequence] = document
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"{path}: {exc}")
    if set(metas) != set(normalized):
        missing = sorted(set(metas) - set(normalized))
        extra = sorted(set(normalized) - set(metas))
        errors.append(f"정규화 문서 범위가 일치하지 않습니다: 누락={missing} 추가={extra}")

    errors.extend(validate_mfds_items())

    if not DB_PATH.is_file():
        errors.append(f"데이터베이스가 없습니다: {DB_PATH}")
    else:
        try:
            con = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
            integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                errors.append(f"SQLite integrity_check 결과: {integrity}")
            foreign_keys = con.execute("PRAGMA foreign_key_check").fetchall()
            if foreign_keys:
                errors.append(f"SQLite foreign_key_check 결과: {foreign_keys[:5]}")
            versions = con.execute("SELECT count(*) FROM versions").fetchone()[0]
            entries = con.execute("SELECT count(*) FROM entries").fetchone()[0]
            fts = con.execute("SELECT count(*) FROM fts").fetchone()[0]
            if versions != len(normalized):
                errors.append(f"SQLite 버전 수 {versions}개가 정규화 문서 수 {len(normalized)}개와 다릅니다")
            if entries != fts:
                errors.append(f"FTS 항목 수 {fts}개가 항목 수 {entries}개와 다릅니다")
            con.close()
        except sqlite3.Error as exc:
            errors.append(f"SQLite 검증 실패: {exc}")
    return errors


def main() -> None:
    errors = validate()
    if errors:
        for error in errors:
            print(f"[오류] {error}", file=sys.stderr)
        raise SystemExit(f"검증에서 오류 {len(errors)}건이 발생했습니다")
    print("검증을 통과했습니다")


if __name__ == "__main__":
    main()
