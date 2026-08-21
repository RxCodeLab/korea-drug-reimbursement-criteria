from __future__ import annotations

import json
from pathlib import Path

from common import BASE, DATA

NORMALIZED = DATA / "normalized"
PUBLIC = BASE / "public"
MAX_BODY_CHARS = 50_000


def build_index() -> list[dict]:
    records: list[dict] = []
    if not NORMALIZED.is_dir():
        return records
    for path in sorted(NORMALIZED.glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        if document.get("complete") is not True:
            raise RuntimeError(f"완료되지 않은 정규화 문서입니다: {path}")
        version = document["version"]
        attachments = {a["ordinal"]: a for a in document["attachments"]}
        for entry in document["entries"]:
            source = attachments[entry["attachment_ordinal"]]
            body = entry["body"]
            records.append({
                "key": entry["block_identity"],
                "title": entry["title"],
                "body": body[:MAX_BODY_CHARS],
                "truncated": len(body) > MAX_BODY_CHARS,
                "action": entry["action"],
                "class_no": entry["class_no"],
                "class_header": entry["class_header"],
                "effective_date": version["시행일자"],
                "notice_number": version["발령번호"],
                "sequence": version["행정규칙일련번호"],
                "source_name": source["original_name"],
                "source_sha256": source["sha256"],
            })
    return sorted(records, key=lambda r: (r["key"], r["effective_date"], r["sequence"], r["source_sha256"]))


HTML = r'''<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>약제 급여기준 변경 이력 검색</title>
<meta name="description" content="약제명과 성분명으로 보건복지부 약제 급여기준의 신설·변경·삭제 이력, 시행일과 고시번호를 검색합니다. 법제처 Open API 자료를 이용한 비공식 서비스입니다.">
<meta name="robots" content="index,follow">
<meta property="og:type" content="website">
<meta property="og:title" content="약제 급여기준 변경 이력 검색">
<meta property="og:description" content="약제명과 성분명으로 시행일별 급여기준 변경 내용을 검색합니다.">
<style>
body{font-family:system-ui,"Malgun Gothic",sans-serif;max-width:1000px;margin:2rem auto;padding:0 1rem;line-height:1.55;color:#1d2433}input{width:100%;box-sizing:border-box;padding:.8rem;font-size:1rem;border:1px solid #8993a4;border-radius:6px}.hint,.meta{color:#5b6575}.group{margin:1.5rem 0;border-top:2px solid #28364d}.group h2{font-size:1.15rem}details{border:1px solid #ccd2dc;border-radius:6px;margin:.5rem 0;padding:.5rem .8rem}summary{cursor:pointer;font-weight:600}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f5f7fa;padding:.8rem}.badge{color:#a33;margin-left:.5rem}.empty{padding:2rem 0;color:#5b6575}
</style></head><body>
<h1>약제 급여기준 변경 이력 검색</h1><p class="hint">한글 또는 영문 검색어를 공백으로 나누어 입력하면, 하나라도 포함한 항목을 표시합니다.</p>
<input id="q" type="search" autocomplete="off" placeholder="예: dapagliflozin 다파글리플로진" autofocus><p id="status" class="meta"></p><main id="results"></main>
<script>
const q=document.querySelector('#q'),status=document.querySelector('#status'),root=document.querySelector('#results');let rows=[];
const el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n};
function render(){const terms=q.value.trim().toLocaleLowerCase('ko').split(/\s+/).filter(Boolean);root.replaceChildren();if(!terms.length){status.textContent=`전체 ${rows.length.toLocaleString()}개 항목`;root.append(el('p','검색어를 입력해 주세요.','empty'));return}const hits=rows.filter(r=>{const hay=(r.title+'\n'+r.body+'\n'+r.class_header).toLocaleLowerCase('ko');return terms.some(t=>hay.includes(t))});const groups=new Map();for(const r of hits){if(!groups.has(r.key))groups.set(r.key,[]);groups.get(r.key).push(r)}status.textContent=`항목 ${groups.size.toLocaleString()}개 · 개정 이력 ${hits.length.toLocaleString()}건`;for(const [key,items] of groups){const section=el('section',undefined,'group');section.append(el('h2',items[0].title));if(items[0].class_header)section.append(el('p',items[0].class_header,'meta'));for(const r of items){const d=el('details'),s=el('summary');s.append(document.createTextNode(`${r.effective_date.slice(0,4)}-${r.effective_date.slice(4,6)}-${r.effective_date.slice(6)} 시행 · 고시 제${r.notice_number}호`));s.append(el('span',r.action,'badge'));d.append(s);d.append(el('p',`출처: ${r.source_name} · ${r.source_sha256.slice(0,12)}…`,'meta'));d.append(el('pre',r.body+(r.truncated?'\n\n[검색 색인에는 본문 일부만 표시됩니다. 전체 내용은 DB에서 확인하세요.]':'')));section.append(d)}root.append(section)}}
fetch('search-index.json').then(r=>{if(!r.ok)throw new Error(`HTTP ${r.status}`);return r.json()}).then(data=>{rows=data;render()}).catch(e=>{status.textContent='검색 인덱스를 불러오지 못했습니다.';console.error(e)});q.addEventListener('input',render);
</script></body></html>'''


def main() -> None:
    PUBLIC.mkdir(parents=True, exist_ok=True)
    index = build_index()
    (PUBLIC / "search-index.json").write_text(
        json.dumps(index, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    (PUBLIC / "index.html").write_text(HTML + "\n", encoding="utf-8")
    print(f"검색 항목 {len(index)}개를 {PUBLIC}에 생성했습니다.")


if __name__ == "__main__":
    main()
