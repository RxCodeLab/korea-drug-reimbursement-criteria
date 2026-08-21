from __future__ import annotations

import json
import shutil
from pathlib import Path

from common import BASE, DATA

NORMALIZED = DATA / "normalized"
MFDS_ITEMS = DATA / "mfds" / "items"
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
                "role": "" if entry["class_no"] else source["role"],
                "ordinal": source["ordinal"],
            })
    return sorted(records, key=lambda r: (r["key"], r["effective_date"], r["sequence"], r["source_sha256"]))
def _compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


def build_mfds_public() -> list[dict]:
    index: list[dict] = []
    output = PUBLIC / "mfds"
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    if not MFDS_ITEMS.is_dir():
        (output / "search-index.json").write_text(_compact_json(index), encoding="utf-8")
        return index
    items_dir = output / "items"
    for path in sorted(MFDS_ITEMS.glob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("complete") is not True:
            raise RuntimeError(f"완료되지 않은 MFDS 항목입니다: {path}")
        revisions = record.get("revisions") or []
        index.append({
            "item_seq": record["item_seq"],
            "item_name": record["item_name"],
            "entp_name": record["entp_name"],
            "main_item_ingr": record["main_item_ingr"],
            "status": record["status"],
            "permit_date": record["permit_date"],
            "revision_count": len(revisions),
            "last_observed_at": revisions[0]["last_observed_at"] if revisions else "",
            "source_url": record["source_url"],
        })
        target = items_dir / path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_compact_json(record), encoding="utf-8")
    index.sort(key=lambda r: (r["item_name"], r["item_seq"]))
    (output / "search-index.json").write_text(_compact_json(index), encoding="utf-8")
    return index


HTML = r'''<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>약제 급여기준 변경 이력 검색</title>
<meta name="description" content="약제명과 성분명으로 보건복지부 약제 급여기준의 신설·변경·삭제 이력, 시행일과 고시번호를 검색합니다.">
<meta name="robots" content="index,follow">
<meta property="og:type" content="website">
<meta property="og:title" content="약제 급여기준 변경 이력 검색">
<meta property="og:description" content="약제명과 성분명으로 시행일별 급여기준 변경 내용을 검색합니다.">
<style>
body{font-family:system-ui,"Malgun Gothic",sans-serif;max-width:1000px;margin:2rem auto;padding:0 1rem;line-height:1.55;color:#1d2433}input{width:100%;box-sizing:border-box;padding:.8rem;font-size:1rem;border:1px solid #8993a4;border-radius:6px}.hint,.meta{color:#5b6575}.group{margin:1.5rem 0;border-top:2px solid #28364d}.group h2{font-size:1.15rem}details{border:1px solid #ccd2dc;border-radius:6px;margin:.5rem 0;padding:.5rem .8rem}summary{cursor:pointer;font-weight:600}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f5f7fa;padding:.8rem}.badge{color:#a33;margin-left:.5rem}.revision{font-weight:700;margin:.6rem 0 .2rem}.empty{padding:2rem 0;color:#5b6575}.related{margin-top:2rem;border-color:#8993a4}.related>.group{margin-left:.5rem}footer{margin-top:2.5rem;padding-top:.8rem;border-top:1px solid #ccd2dc;color:#5b6575;font-size:.9rem}
</style></head><body>
<h1>약제 급여기준 변경 이력 검색</h1><p class="hint">한글 또는 영문 검색어를 공백으로 나누어 입력하면, 하나라도 포함한 항목을 표시합니다.</p>
<input id="q" type="search" autocomplete="off" placeholder="예: dapagliflozin 다파글리플로진" autofocus><p id="status" class="meta"></p><main id="results"></main>
<footer><a href="https://github.com/RxCodeLab/korea-drug-reimbursement-criteria">GitHub 저장소</a> · 데이터 자동 갱신</footer>
<script>
const q=document.querySelector('#q'),status=document.querySelector('#status'),root=document.querySelector('#results');let rows=[],mfdsRows=null;
const el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n};
const actionLabels={notice:'고시문',comparison:'변경대비표',reason:'개정이유',qa:'질의응답',transition:'경과규정',other:'기타자료',annex:'별지','':'기준'};
const actionLabel=action=>actionLabels[action]??action;
const roleRanks={notice:0,reason:1,comparison:2,annex:3,transition:4,qa:5,other:6};
const roleRank=role=>roleRanks[role]??7;
const dateLabel=date=>`${date.slice(0,4)}-${date.slice(4,6)}-${date.slice(6)}`;
const versionHeader=record=>`${dateLabel(record.effective_date)} 시행 · 고시 제${record.notice_number}호`;
const byNewest=(a,b)=>b.effective_date.localeCompare(a.effective_date)||b.sequence.localeCompare(a.sequence);
const byRole=(a,b)=>roleRank(a.role)-roleRank(b.role)||a.ordinal-b.ordinal;
const TRUNCATED='\n\n[검색 색인에는 본문 일부만 표시됩니다. 전체 내용은 DB에서 확인하세요.]';
function groupsBy(records,field){const groups=new Map();for(const record of records){if(!groups.has(record[field]))groups.set(record[field],[]);groups.get(record[field]).push(record)}return[...groups.values()]}
function groupCriteria(records){return groupsBy(records,'key').map(items=>items.sort(byNewest)).sort((a,b)=>byNewest(a[0],b[0]))}
function groupDocuments(records){return groupsBy(records,'sequence').map(items=>items.sort(byRole)).sort((a,b)=>byNewest(a[0],b[0]))}
function appendCriteria(groups,container){for(const items of groups){const section=el('section',undefined,'group'),newest=items[0];section.append(el('h2',newest.title));if(newest.class_header)section.append(el('p',newest.class_header,'meta'));for(const record of items){const details=el('details'),summary=el('summary');summary.append(document.createTextNode(versionHeader(record)));summary.append(el('span',actionLabel(record.action),'badge'));details.append(summary);details.append(el('p',`출처: ${record.source_name} · ${record.source_sha256.slice(0,12)}…`,'meta'));details.append(el('pre',record.body+(record.truncated?TRUNCATED:'')));section.append(details)}container.append(section)}}
function appendDocuments(groups,container){for(const items of groups){const section=el('section',undefined,'group'),head=el('p',undefined,'revision');head.append(document.createTextNode(versionHeader(items[0])));for(const role of[...new Set(items.map(item=>item.role))].sort((a,b)=>roleRank(a)-roleRank(b)))head.append(el('span',actionLabel(role),'badge'));section.append(head);for(const record of items){const details=el('details'),summary=el('summary');summary.append(el('span',actionLabel(record.role),'badge'));summary.append(document.createTextNode(` ${record.source_name} · ${record.source_sha256.slice(0,8)}…`));details.append(summary);details.append(el('pre',record.body+(record.truncated?TRUNCATED:'')));section.append(details)}container.append(section)}}
function appendMfds(items,container){
  const section=el('section',undefined,'group mfds');
  section.append(el('h2','식약처 허가 적응증'));
  section.append(el('p','허가 적응증은 식약처 허가사항이며 급여기준과는 별개입니다.','meta'));
  for(const item of items){
    const details=el('details'),summary=el('summary');
    summary.append(document.createTextNode(`${item.item_name} · ${item.entp_name}`));
    summary.append(el('span',item.status,'badge'));
    details.append(summary);
    const permit=item.permit_date?` · 허가일 ${dateLabel(item.permit_date)}`:'';
    const latest=item.last_observed_at?item.last_observed_at.slice(0,10):'-';
    const meta=el('p',`품목기준코드 ${item.item_seq} · 주성분 ${item.main_item_ingr||'-'}${permit} · 관찰 ${item.revision_count}개 개정 · 최신 ${latest}`,'meta');
    details.append(meta);
    const link=el('a','의약품안전나라 원문');
    link.href=item.source_url;link.target='_blank';link.rel='noopener';
    meta.append(document.createTextNode(' · '));meta.append(link);
    const body=el('pre','상세 정보를 열어 효능·효과를 확인하세요.');
    details.append(body);
    let loaded=false;
    details.addEventListener('toggle',()=>{
      if(!details.open||loaded)return;
      loaded=true;body.textContent='불러오는 중…';
      fetch(`mfds/items/${item.item_seq}.json`).then(r=>{if(!r.ok)throw new Error(`HTTP ${r.status}`);return r.json()}).then(doc=>{
        const revisions=doc.revisions||[];
        body.textContent=revisions.length?revisions.map((revision,index)=>{
          const label=index===0?'현재':'이전';
          return `[${label} · 최초 관찰 ${revision.first_observed_at.slice(0,10)} · 최종 관찰 ${revision.last_observed_at.slice(0,10)}]\n${revision.ee_text}`;
        }).join('\n\n'):'효능·효과 정보가 없습니다.';
      }).catch(()=>{body.textContent='상세 정보를 불러오지 못했습니다.'});
    });
    section.append(details);
  }
  container.append(section);
}
function render(){const terms=q.value.trim().toLocaleLowerCase('ko').split(/\s+/).filter(Boolean);root.replaceChildren();if(!terms.length){status.textContent=`전체 ${rows.length.toLocaleString()}개 항목`;root.append(el('p','검색어를 입력해 주세요.','empty'));return}const hits=rows.filter(record=>{const hay=(record.title+'\n'+record.body+'\n'+record.class_header).toLocaleLowerCase('ko');return terms.some(term=>hay.includes(term))});const criteria=hits.filter(record=>record.class_no),documents=hits.filter(record=>!record.class_no),criterionGroups=groupCriteria(criteria),documentGroups=groupDocuments(documents),mfdsItems=(mfdsRows||[]).filter(item=>{const hay=(item.item_name+'\n'+item.entp_name+'\n'+item.main_item_ingr).toLocaleLowerCase('ko');return terms.some(term=>hay.includes(term))});status.textContent=`급여기준 ${criterionGroups.length.toLocaleString()}개 · 개정 이력 ${criteria.length.toLocaleString()}건 · 관련 문서 ${documents.length.toLocaleString()}건${mfdsRows?` · 허가 품목 ${mfdsItems.length.toLocaleString()}개`:''}`;appendCriteria(criterionGroups,root);if(mfdsItems.length)appendMfds(mfdsItems,root);if(documents.length){const related=el('details',undefined,'related');related.append(el('summary',`관련 고시문 및 첨부자료 ${documents.length.toLocaleString()}건`));appendDocuments(documentGroups,related);root.append(related)}}
fetch('search-index.json').then(r=>{if(!r.ok)throw new Error(`HTTP ${r.status}`);return r.json()}).then(data=>{rows=data;render()}).catch(e=>{status.textContent='검색 인덱스를 불러오지 못했습니다.';console.error(e)});q.addEventListener('input',render);
fetch('mfds/search-index.json').then(r=>{if(!r.ok)throw new Error(`HTTP ${r.status}`);return r.json()}).then(data=>{mfdsRows=data;render()}).catch(()=>{mfdsRows=null});
</script></body></html>'''


def main() -> None:
    PUBLIC.mkdir(parents=True, exist_ok=True)
    index = build_index()
    mfds_index = build_mfds_public()
    (PUBLIC / "search-index.json").write_text(_compact_json(index), encoding="utf-8")
    (PUBLIC / "index.html").write_text(HTML + "\n", encoding="utf-8")
    print(f"검색 항목 {len(index)}개, 허가 품목 {len(mfds_index)}개를 {PUBLIC}에 생성했습니다.")


if __name__ == "__main__":
    main()
