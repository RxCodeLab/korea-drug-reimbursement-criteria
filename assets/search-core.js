// 검색 로직 중 DOM·주입 데이터에 기대지 않는 순수 부분. 브라우저에서는 인라인으로 실려
// SearchCore 전역을 만들고, Node에서는 module.exports로 잡혀 tests/search-core.test.js가 검증한다.
// DOM을 만지는 코드는 search-ui.js에 둔다 — 이 파일은 document 없이 실행돼야 한다.
const SearchCore=(()=>{
const MFDS_DETAIL_URL='https://nedrug.mfds.go.kr/pbp/CCBBB01/getItemDetail?itemSeq=';
const dateLabel=date=>`${date.slice(0,4)}-${date.slice(4,6)}-${date.slice(6)}`;
const byNewest=(a,b)=>b.effective_date.localeCompare(a.effective_date)||b.sequence.localeCompare(a.sequence);
const baseName=name=>name.replace(/[(].*$/,'');
const brandName=name=>baseName(name).replace(/[0-9][0-9./]*(밀리그램|밀리그람|그램|그람|밀리리터|리터|마이크로그램|mg|㎎|ml|㎖|g|iu|%|만단위|단위).*$/i,'').trim().toLocaleLowerCase('ko');
// 검색 키는 로드 시 한 번만 계산한다. 키 입력마다 2만 행의 문자열을 다시 만들면 입력이 끊긴다.
const searchableCriterion=record=>({...record,hay:(record.title+'\n'+record.body+'\n'+record.class_header+'\n'+dateLabel(record.effective_date)+'\n'+record.effective_date+'\n고시 제'+record.notice_number+'호').toLocaleLowerCase('ko')});
const mfdsTier=(item,terms)=>{if(terms.some(t=>item.brand===t))return 0;if(terms.some(t=>item.brand.startsWith(t)))return 1;if(terms.some(t=>item.nameKey.includes(t)))return 2;if(terms.some(t=>item.exactKey.includes(t)))return 3;return 4};
const distinctClassHeader=record=>{const header=record.class_header.replace(/^\[[^\]]+\]\s*/,'').replace(/\s+/g,''),title=record.title.replace(/\s+/g,'');return header&&!header.startsWith(title)&&!title.startsWith(header)};
const TRUNCATED='\n\n[검색 색인에는 본문 일부만 표시됩니다. 전체 내용은 DB에서 확인하세요.]';
const MFDS_GROUP_LIMIT=200;
function groupsBy(records,field){const groups=new Map();for(const record of records){if(!groups.has(record[field]))groups.set(record[field],[]);groups.get(record[field]).push(record)}return[...groups.values()]}
function groupCriteria(records){return groupsBy(records,'key').map(items=>items.sort(byNewest)).sort((a,b)=>byNewest(a[0],b[0]))}
// 허가 색인은 객체 배열이 아니라 열 배열로 들고 있는다. 4만 행을 객체로 펼치면 그것만으로
// 중급 모바일에서 1초가 넘게 걸린다(실측 CPU 4x 스로틀 1,088ms). 검색에 필요한 파생 키만
// 열 단위로 미리 만들고, 실제 객체는 화면에 그릴 그룹에 한해 materializeMfds가 만든다.
function buildMfdsIndex(index){
  const fields=index.fields,dicts=index.dicts||{},rows=index.rows,n=rows.length;
  const at=name=>fields.indexOf(name);
  const iSeq=at('item_seq'),iName=at('item_name'),iRev=at('revision_count');
  // 사전으로 압축된 열은 정수 id만 뽑는다(build_site.MFDS_INDEX_DICT_FIELDS 참고).
  // 사전이 없는 옛 형식 색인도 읽을 수 있도록 그때는 seen 맵으로 즉석에서 사전을 만든다.
  const column=name=>({at:at(name),table:dicts[name]?dicts[name]:[],
                       ids:new Int32Array(n),seen:dicts[name]?null:new Map()});
  const entp=column('entp_name'),ingr=column('main_item_ingr'),ingrEng=column('main_item_ingr_eng'),
        permit=column('permit_date'),key=column('content_key');
  const columns=[entp,ingr,ingrEng,permit,key];
  const seqs=new Array(n),names=new Array(n),revisions=new Array(n),brands=new Array(n),nameKeys=new Array(n);
  // 행은 딱 한 번만 훑는다. 열마다 따로 훑으면 그 횟수만큼 로드 시간이 곱해진다.
  for(let r=0;r<n;r++){
    const row=rows[r],name=row[iName];
    seqs[r]=row[iSeq];names[r]=name;revisions[r]=row[iRev];
    brands[r]=brandName(name);nameKeys[r]=name.toLocaleLowerCase('ko');
    for(let c=0;c<columns.length;c++){
      const target=columns[c],value=row[target.at];
      if(!target.seen){target.ids[r]=value;continue}
      let id=target.seen.get(value);
      if(id===undefined){id=target.table.length;target.seen.set(value,id);target.table.push(value)}
      target.ids[r]=id;
    }
  }
  const lower=table=>table.map(value=>String(value).toLocaleLowerCase('ko'));
  return {n,names,brands,nameKeys,seqs,revisions,entp,ingr,ingrEng,permit,key,
          entpLow:lower(entp.table),ingrLow:lower(ingr.table),ingrEngLow:lower(ingrEng.table)};
}
// 키 입력마다 도는 부분. 제조사·성분은 값 종류가 행 수보다 훨씬 적으므로(43,017행 대 13,886종)
// 사전에서 먼저 걸러 두고 행에서는 정수 조회만 한다 — 문자열 검사가 43,017번에서 13,886번으로 준다.
// 반환값은 [계층, 행번호] 목록이며 계층 오름차순이다. mfdsTier와 같은 계층 정의를 쓴다.
function matchMfds(index,terms){
  if(!terms.length)return[];
  const dictionaryHits=low=>{const out=new Uint8Array(low.length);
    for(let i=0;i<low.length;i++){const value=low[i];
      for(const term of terms)if(value.includes(term)){out[i]=1;break}}
    return out};
  const entpHit=dictionaryHits(index.entpLow),ingrHit=dictionaryHits(index.ingrLow),ingrEngHit=dictionaryHits(index.ingrEngLow);
  const matches=[];
  for(let r=0;r<index.n;r++){
    const brand=index.brands[r];
    let tier=4;
    for(const term of terms)if(brand===term){tier=0;break}
    if(tier===4)for(const term of terms)if(brand.startsWith(term)){tier=1;break}
    if(tier===4){const nameKey=index.nameKeys[r];for(const term of terms)if(nameKey.includes(term)){tier=2;break}}
    if(tier===4&&(entpHit[index.entp.ids[r]]||ingrHit[index.ingr.ids[r]]||ingrEngHit[index.ingrEng.ids[r]]))tier=3;
    if(tier<4)matches.push([tier,r]);
  }
  return matches.sort((a,b)=>a[0]-b[0]);
}
// 화면에 실제로 그릴 그룹의 품목만 객체로 만든다. 계층은 matchMfds가 이미 구했으므로
// 다시 계산하지 않고 그대로 실어 준다(대표 품목 선정에 쓴다).
function materializeMfds(index,row,tier){
  const seq=index.seqs[row];
  return {item_seq:seq,item_name:index.names[row],entp_name:index.entp.table[index.entp.ids[row]],
    main_item_ingr:index.ingr.table[index.ingr.ids[row]],
    main_item_ingr_eng:index.ingrEng.table[index.ingrEng.ids[row]],
    permit_date:index.permit.table[index.permit.ids[row]],
    revision_count:index.revisions[row],content_key:index.key.table[index.key.ids[row]],
    source_url:MFDS_DETAIL_URL+encodeURIComponent(seq),
    brand:index.brands[row],nameKey:index.nameKeys[row],tier};
}
// 성분으로 묶고 같은 효능·효과(content_key)끼리 다시 묶는다. 정수 id로 묶어 문자열 비교를 피하고,
// matches가 계층 오름차순이라 Map 삽입 순서가 그대로 관련도 순서가 된다.
function indicationGroups(index,matches){
  const byIngredient=new Map();
  for(const match of matches){
    const row=match[1];
    let groups=byIngredient.get(index.ingr.ids[row]);
    if(!groups){groups=new Map();byIngredient.set(index.ingr.ids[row],groups)}
    const contentId=index.key.ids[row];
    let bucket=groups.get(contentId);
    if(!bucket){bucket=[];groups.set(contentId,bucket)}
    bucket.push(match);
  }
  const permitOf=row=>index.permit.table[index.permit.ids[row]]||'99999999';
  return [...byIngredient].map(([ingredientId,groups])=>({
    ingredient:index.ingr.table[ingredientId]||'성분 미상',
    groups:[...groups.values()].map(bucket=>bucket.sort((a,b)=>
      permitOf(a[1]).localeCompare(permitOf(b[1]))||index.names[a[1]].localeCompare(index.names[b[1]])))
  }));
}
return {MFDS_DETAIL_URL,MFDS_GROUP_LIMIT,TRUNCATED,dateLabel,baseName,brandName,searchableCriterion,mfdsTier,distinctClassHeader,byNewest,groupsBy,groupCriteria,buildMfdsIndex,matchMfds,materializeMfds,indicationGroups};
})();
if(typeof module!=='undefined'&&module.exports)module.exports=SearchCore;
