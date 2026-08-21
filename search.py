import argparse
import html
import re
import sqlite3
import webbrowser

from common import DB_PATH, DATA


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
                       a.original_name AS source_name, a.sha256 AS source_sha256
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


def highlight(text: str, terms: list[str]) -> str:
    out = html.escape(text)
    for t in terms:
        out = re.sub(f"({re.escape(html.escape(t))})", r"<mark>\1</mark>", out, flags=re.I)
    return out


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
    groups: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        groups.setdefault(r["norm_key"], []).append(r)

    keys = sorted(groups, key=lambda k: (k.startswith("__gaejeong__"), k))

    print(f"검색어: {terms} — 항목 {sum(1 for k in keys if not k.startswith('__gaejeong__'))}개, "
          f"개정 고시 전문 검색 결과 {sum(1 for k in keys if k.startswith('__gaejeong__'))}건")
    parts = [f"""<!doctype html><meta charset="utf-8">
<title>급여기준 이력: {html.escape(' '.join(terms))}</title>
<style>
 body{{font-family:'Malgun Gothic',sans-serif;max-width:960px;margin:2em auto;line-height:1.6}}
 details{{border:1px solid #ccc;border-radius:6px;margin:.4em 0;padding:.3em .8em}}
 summary{{cursor:pointer;font-weight:600}}
 .ver{{color:#06c;font-weight:700}} .act{{color:#c30}}
 pre{{white-space:pre-wrap;background:#fafafa;padding:1em;border-radius:6px}}
 h2{{border-bottom:2px solid #333;padding-bottom:.2em}}
 mark{{background:#ffe58f}}
</style>
<h1>급여기준 변천: {html.escape(' / '.join(terms))}</h1>"""]

    for k in keys:
        g = groups[k]
        first = g[0]
        head = first["title"] if not k.startswith("__gaejeong__") else "개정 고시 전문 검색 결과"
        print(f"\n== {head[:80]}")
        parts.append(f"<h2>{highlight(head, terms)}</h2>")
        if first["class_header"]:
            parts.append(f"<p><i>{html.escape(first['class_header'])}</i></p>")
        for r in g:
            line = f"  {r['효력일'][:4]}-{r['효력일'][4:6]}-{r['효력일'][6:]} 시행 · 고시 제{r['발령번호']}호 · {r['action']}"
            print(line)
            parts.append(
                f"<details><summary><span class='ver'>{r['효력일'][:4]}-{r['효력일'][4:6]}-{r['효력일'][6:]}"
                f" 시행</span> · 고시 제{html.escape(r['발령번호'])}호 · "
                f"<span class='act'>{html.escape(r['action'])}</span></summary>"
                f"<pre>{highlight(r['body'], terms)}</pre></details>")

    out = DATA / f"report_{re.sub(r'[^A-Za-z0-9가-힣]+', '_', '_'.join(terms))[:50]}.html"
    out.write_text("\n".join(parts), encoding="utf-8")
    print(f"\nHTML 보고서: {out}")
    if not args.no_open:
        try:
            webbrowser.open(out.as_uri())
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    main()
