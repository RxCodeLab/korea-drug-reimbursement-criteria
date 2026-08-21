import argparse
import html
import re
import sqlite3
import webbrowser

from common import DB_PATH, DATA

ACTION_LABELS = {
    "notice": "고시문",
    "comparison": "변경대비표",
    "reason": "개정이유",
    "qa": "질의응답",
    "transition": "경과규정",
    "other": "기타자료",
    "annex": "별지",
    "": "기준",
}

ROLE_ORDER = ("notice", "reason", "comparison", "annex", "transition", "qa", "other")
_ROLE_RANK = {role: rank for rank, role in enumerate(ROLE_ORDER)}

FOOTER_URL = "https://github.com/RxCodeLab/korea-drug-reimbursement-criteria"
FOOTER_HTML = f'<footer><a href="{FOOTER_URL}">GitHub 저장소</a> · 데이터 자동 갱신</footer>'


def action_label(action: str) -> str:
    return ACTION_LABELS.get(action, action)


def role_rank(role: str) -> int:
    return _ROLE_RANK.get(role, len(ROLE_ORDER))


def date_label(effective_date: str) -> str:
    return f"{effective_date[:4]}-{effective_date[4:6]}-{effective_date[6:8]}"


def revision_header(effective_date: str, notice_number: str) -> str:
    return f"{date_label(effective_date)} 시행 · 고시 제{notice_number}호"


def sort_newest_first(records: list[dict]) -> list[dict]:
    return sorted(records, key=lambda record: (record["effective_date"], record["sequence"]), reverse=True)


def group_criteria(records: list[dict]) -> list[list[dict]]:
    """기준 레코드를 항목별로 묶어 그룹·행 모두 최신 순으로 정렬한다. items[0]이 그룹 머리글 출처다."""
    groups: dict[str, list[dict]] = {}
    for record in records:
        groups.setdefault(record["key"], []).append(record)
    ordered = [sort_newest_first(items) for items in groups.values()]
    ordered.sort(key=lambda items: (items[0]["effective_date"], items[0]["sequence"]), reverse=True)
    return ordered


def group_documents(records: list[dict]) -> list[list[dict]]:
    """문서 레코드를 행정규칙 일련번호(개정 회차)별로 묶어 개정 순 최신, 역할 순위·첨부 순번으로 정렬한다."""
    groups: dict[str, list[dict]] = {}
    for record in records:
        groups.setdefault(record["sequence"], []).append(record)
    ordered = [sorted(items, key=lambda record: (role_rank(record["role"]), record["ordinal"])) for items in groups.values()]
    ordered.sort(key=lambda items: (items[0]["effective_date"], items[0]["sequence"]), reverse=True)
    return ordered


def query(terms: list[str]) -> list[sqlite3.Row]:
    if not DB_PATH.is_file():
        raise RuntimeError(f"검색 DB가 없습니다: {DB_PATH} (`python ingest.py`를 먼저 실행하세요)")
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        fts_q = " OR ".join(f'"{t.replace(chr(34), chr(34) * 2)}"' for t in terms)
        try:
            return con.execute("""
                SELECT e.*, v.효력일, v.발령번호, v.일련번호,
                       a.original_name AS source_name, a.sha256 AS source_sha256,
                       a.role AS source_role, a.ordinal AS source_ordinal
                FROM fts JOIN entries e ON e.id = fts.rowid
                JOIN versions v ON v.ver_id = e.ver_id
                JOIN attachments a ON a.attachment_id = e.attachment_id
                WHERE fts MATCH ?
                ORDER BY e.norm_key, v.효력일, v.일련번호
            """, (fts_q,)).fetchall()
        except sqlite3.DatabaseError as exc:
            raise RuntimeError("검색 DB 스키마가 오래됐거나 손상됐습니다. `python ingest.py`로 다시 생성하세요") from exc
    finally:
        con.close()


def record_from_row(row: sqlite3.Row) -> dict:
    return {
        "key": row["norm_key"],
        "title": row["title"],
        "body": row["body"],
        "action": row["action"],
        "class_no": row["class_no"],
        "class_header": row["class_header"],
        "effective_date": row["효력일"],
        "notice_number": row["발령번호"],
        "sequence": row["일련번호"],
        "source_name": row["source_name"],
        "source_sha256": row["source_sha256"],
        "role": "" if row["class_no"] else row["source_role"],
        "ordinal": row["source_ordinal"],
    }


def highlight(text: str, terms: list[str]) -> str:
    out = html.escape(text)
    for t in terms:
        out = re.sub(f"({re.escape(html.escape(t))})", r"<mark>\1</mark>", out, flags=re.I)
    return out


def build_html_report(terms: list[str], records: list[dict]) -> str:
    criteria = [record for record in records if record["class_no"]]
    documents = [record for record in records if not record["class_no"]]
    parts = [f"""<!doctype html><meta charset="utf-8">
<title>급여기준 이력: {html.escape(' '.join(terms))}</title>
<style>
 body{{font-family:'Malgun Gothic',sans-serif;max-width:960px;margin:2em auto;line-height:1.6}}
 details{{border:1px solid #ccc;border-radius:6px;margin:.4em 0;padding:.3em .8em}}
 summary{{cursor:pointer;font-weight:600}}
 .ver{{color:#06c;font-weight:700}} .act{{color:#c30}}
 pre{{white-space:pre-wrap;background:#fafafa;padding:1em;border-radius:6px}}
 h2{{border-bottom:2px solid #333;padding-bottom:.2em}}
 .revision{{font-weight:700;margin:1em 0 .2em}}
 mark{{background:#ffe58f}}
 footer{{margin-top:2em;border-top:1px solid #ccc;padding-top:.6em;color:#555}}
</style>
<h1>급여기준 변천: {html.escape(' / '.join(terms))}</h1>"""]

    for items in group_criteria(criteria):
        newest = items[0]
        parts.append(f"<h2>{highlight(newest['title'], terms)}</h2>")
        if newest["class_header"]:
            parts.append(f"<p><i>{html.escape(newest['class_header'])}</i></p>")
        for record in items:
            label = action_label(record["action"])
            parts.append(
                f"<details><summary><span class='ver'>{date_label(record['effective_date'])}"
                f" 시행</span> · 고시 제{html.escape(record['notice_number'])}호 · "
                f"<span class='act'>{html.escape(label)}</span></summary>"
                f"<pre>{highlight(record['body'], terms)}</pre></details>")

    if documents:
        parts.append(f"<h2>관련 고시문 및 첨부자료 {len(documents)}건</h2>")
        for items in group_documents(documents):
            head = items[0]
            badges = "".join(
                f"<span class='act'>{html.escape(action_label(role))}</span>"
                for role in sorted({record["role"] for record in items}, key=role_rank))
            parts.append(
                f"<p class='revision'>{html.escape(revision_header(head['effective_date'], head['notice_number']))}"
                f" · {badges}</p>")
            for record in items:
                parts.append(
                    f"<details><summary><span class='act'>{html.escape(action_label(record['role']))}</span> "
                    f"{html.escape(record['source_name'])} · {record['source_sha256'][:8]}…</summary>"
                    f"<pre>{highlight(record['body'], terms)}</pre></details>")

    parts.append(FOOTER_HTML)
    return "\n".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser(description="약제 급여기준 변경 이력을 검색합니다.")
    parser.add_argument("terms", nargs="+", help="검색할 성분명 또는 약제명")
    parser.add_argument("--no-open", action="store_true", help="HTML 보고서를 브라우저에서 열지 않음")
    args = parser.parse_args()
    terms = [term.strip() for term in args.terms if term.strip()]
    if not terms:
        parser.error("검색어가 필요합니다")
    try:
        rows = query(terms)
    except RuntimeError as exc:
        parser.exit(2, f"오류: {exc}\n")
    records = [record_from_row(row) for row in rows]
    criteria = [record for record in records if record["class_no"]]
    documents = [record for record in records if not record["class_no"]]
    criterion_groups = group_criteria(criteria)
    document_groups = group_documents(documents)

    print(f"검색어: {terms} — 급여기준 {len(criterion_groups)}개 · 개정 이력 {len(criteria)}건 · 관련 문서 {len(documents)}건")
    for items in criterion_groups:
        newest = items[0]
        print(f"\n== {newest['title'][:80]}")
        for record in items:
            label = action_label(record["action"])
            print(f"  {revision_header(record['effective_date'], record['notice_number'])} · {label}")
    if documents:
        print(f"\n관련 고시문 및 첨부자료 {len(documents)}건")
        for items in document_groups:
            head = items[0]
            roles = " ".join(action_label(role) for role in sorted({record["role"] for record in items}, key=role_rank))
            print(f"\n  {revision_header(head['effective_date'], head['notice_number'])} · {roles}")
            for record in items:
                print(f"    [{action_label(record['role'])}] {record['source_name']} · {record['source_sha256'][:8]}…")

    out = DATA / f"report_{re.sub(r'[^A-Za-z0-9가-힣]+', '_', '_'.join(terms))[:50]}.html"
    out.write_text(build_html_report(terms, records), encoding="utf-8")
    print(f"\nHTML 보고서: {out}")
    if not args.no_open:
        try:
            webbrowser.open(out.as_uri())
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    main()
