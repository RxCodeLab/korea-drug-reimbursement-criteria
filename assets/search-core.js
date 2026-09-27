// Pure search logic, shared by the page and tests/search-core.test.js. Must not touch the DOM.
const SearchCore=(()=>{
const MFDS_DETAIL_URL='https://nedrug.mfds.go.kr/pbp/CCBBB01/getItemDetail?itemSeq=';
const dateLabel=date=>`${date.slice(0,4)}-${date.slice(4,6)}-${date.slice(6)}`;
const byNewest=(a,b)=>b.effective_date.localeCompare(a.effective_date)||b.sequence.localeCompare(a.sequence);
const baseName=name=>name.replace(/[(].*$/,'');
const brandName=name=>baseName(name).replace(/[0-9][0-9./]*(밀리그램|밀리그람|그램|그람|밀리리터|리터|마이크로그램|mg|㎎|㎍|ml|㎖|g|iu|%|만단위|단위).*$/i,'').trim().toLowerCase();
const searchableCriterion=record=>({...record,hay:(record.title+'\n'+record.body+'\n'+(record.class_header||'')+'\n'+dateLabel(record.effective_date)+'\n'+record.effective_date+'\n고시 제'+record.notice_number+'호'+(record.aliases?'\n'+record.aliases:'')).toLowerCase()});
const distinctClassHeader=record=>{const header=record.class_header.replace(/^\[[^\]]+\]\s*/,'').replace(/\s+/g,''),title=record.title.replace(/\s+/g,'');return header&&!header.startsWith(title)&&!title.startsWith(header)};
const TRUNCATED='\n\n[검색 색인에는 본문 일부만 표시됩니다. 전체 내용은 DB에서 확인하세요.]';
// Results drawn per '더 보기' page; common one-letter terms match hundreds of criteria and thousands of products.
const MFDS_GROUP_LIMIT=200,CRITERIA_LIMIT=50;
// Product search works on two-character fragments (build_site.write_mfds_shards). GRAM_BUCKETS must equal
// build_site.MFDS_GRAM_BUCKETS. Past FULL_INDEX_BLOCKS candidate blocks the full index is the cheaper download.
const GRAM_BUCKETS=256,FULL_INDEX_BLOCKS=80,MIN_PRODUCT_TERM=2;
function groupsBy(records,field){const groups=new Map();for(const record of records){if(!groups.has(record[field]))groups.set(record[field],[]);groups.get(record[field]).push(record)}return[...groups.values()]}
// Title or alias (Korean ingredient/product name) matches rank above body-only matches, then newest first.
const titleHit=(record,terms)=>{const title=(record.title+'\n'+(record.aliases||'')).toLowerCase();return terms.some(term=>title.includes(term))?0:1};
function groupCriteria(records,terms=[]){const groups=groupsBy(records,'key').map(items=>items.sort(byNewest)),hit=new Map(groups.map(items=>[items,titleHit(items[0],terms)]));return groups.sort((a,b)=>hit.get(a)-hit.get(b)||byNewest(a[0],b[0]))}
// Kept column-oriented as shipped (build_site.encode_mfds_index): building ~40k row objects is too slow on
// mobile. Use materializeMfds for rows being drawn.
function buildMfdsIndex(index){
  const names=index.item_name,n=names.length,brands=new Array(n),nameKeys=new Array(n);
  for(let r=0;r<n;r++){const name=names[r];brands[r]=brandName(name);nameKeys[r]=name.toLowerCase()}
  const column=name=>({table:index.dicts[name],ids:Int32Array.from(index[name])});
  const entp=column('entp_name'),ingr=column('main_item_ingr'),ingrEng=column('main_item_ingr_eng'),permit=column('permit_date'),withdrawn=column('withdrawn');
  const lower=table=>table.map(value=>String(value).toLowerCase());
  return {n,names,brands,nameKeys,seqs:index.item_seq,revisions:index.revision_count,entp,ingr,ingrEng,permit,withdrawn,
          group:Int32Array.from(index.content_group),
          entpLow:lower(entp.table),ingrLow:lower(ingr.table),ingrEngLow:lower(ingrEng.table)};
}
// Returns [tier, row] pairs sorted by tier: 0 brand, 1 brand prefix, 2 product name, 3 maker or ingredient.
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
function materializeMfds(index,row,tier){
  const seq=index.seqs[row];
  return {item_seq:seq,item_name:index.names[row],entp_name:index.entp.table[index.entp.ids[row]],
    main_item_ingr:index.ingr.table[index.ingr.ids[row]],
    main_item_ingr_eng:index.ingrEng.table[index.ingrEng.ids[row]],
    permit_date:index.permit.table[index.permit.ids[row]],
    revision_count:index.revisions[row],withdrawn:index.withdrawn.table[index.withdrawn.ids[row]],
    source_url:MFDS_DETAIL_URL+encodeURIComponent(seq),
    brand:index.brands[row],nameKey:index.nameKeys[row],tier};
}
// 'A|B' -> ['A', 'B'] (the build already removed ingredient codes and repeats).
const ingredientParts=raw=>String(raw||'').split('|').filter(Boolean);
const ingredientLabel=raw=>ingredientParts(raw).join(' + ')||'성분 미상';
// Ingredient name without salt, hydrate and formulation words, so salt variants share a heading
// (다파글리플로진프로판디올수화물 -> 다파글리플로진). suffixes is build_site.PRODUCT_STRIPPED, longest first. A stem
// ending in 산, 화 or 소 is an acid or inorganic name, and there the salt is the drug itself (알긴산나트륨,
// 수산화마그네슘, 탄산수소나트륨).
function productBase(label,suffixes){
  label=label.replace(/\s*\([^()]*\)$/,'').trim()||label;
  label=(label.startsWith('미분화')?label.slice(3):label).trim()||label;
  for(;;){
    const suffix=suffixes.find(s=>label.endsWith(s)),rest=suffix?label.slice(0,-suffix.length).trimEnd():'';
    if(!suffix||rest.replace(/\s/g,'').length<3||!/[가-힣]/.test(rest)||/[산화소]$/.test(rest))return label.replace(/(?<=[가-힣])[\d.]+$/,'');
    label=rest;
  }
}
// Parts repeat across thousands of ingredient values, so each is worked out once per suffix list.
const baseCache=new WeakMap();
function cachedBase(part,suffixes){
  let cache=baseCache.get(suffixes);if(!cache){cache=new Map();baseCache.set(suffixes,cache)}
  let base=cache.get(part);if(base===undefined){base=productBase(part,suffixes);cache.set(part,base)}
  return base;
}
// Parts are sorted so one combination has one heading whatever order the permit lists them in.
const productBaseLabel=(raw,suffixes)=>[...new Set(ingredientParts(raw).map(part=>cachedBase(part,suffixes)))].sort().join('|');
// Groups by ingredient name without salts (productBase), then by identical current indication. Ordered by best
// tier, single ingredients before combinations. Names are worked out once per distinct ingredient.
function indicationGroups(index,matches,suffixes=[]){
  const byIngredient=new Map(),rankOf=new Map(),labelOf=new Map();
  for(const match of matches){
    const row=match[1],id=index.ingr.ids[row];
    let label=labelOf.get(id);
    if(label===undefined){label=ingredientLabel(productBaseLabel(index.ingr.table[id],suffixes));labelOf.set(id,label);
      if(!rankOf.has(label))rankOf.set(label,match[0]*2+(label.includes(' + ')?1:0))}
    let groups=byIngredient.get(label);
    if(!groups){groups=new Map();byIngredient.set(label,groups)}
    const contentId=index.group[row];
    let bucket=groups.get(contentId);
    if(!bucket){bucket=[];groups.set(contentId,bucket)}
    bucket.push(match);
  }
  // Oldest permit first, then name. Permit dates are fixed-width digits and rows are already in name order
  // (build_site sorts them), so plain comparisons do; localeCompare was the slowest step for broad terms.
  const permitOf=row=>index.permit.table[index.permit.ids[row]]||'99999999';
  const byPermit=(a,b)=>{const pa=permitOf(a[1]),pb=permitOf(b[1]);return pa<pb?-1:pa>pb?1:a[1]-b[1]};
  return [...byIngredient].sort((a,b)=>rankOf.get(a[0])-rankOf.get(b[0])).map(([ingredient,groups])=>({
    ingredient,groups:[...groups.values()].map(bucket=>bucket.sort(byPermit))
  }));
}
// The whole input is one phrase (lowercased, spaces collapsed); input without a letter or digit searches nothing.
// Kept as a one-element list so matching code takes the same shape for criteria and products.
const searchTerms=text=>{const phrase=text.trim().toLowerCase().replace(/\s+/g,' ');return /[\p{L}\p{N}]/u.test(phrase)?[phrase]:[]};
// Dates and notice numbers ('2026-06-01', '고시 제2026-117호') name criteria, never products.
const NOTICE_TERM=/^(\d{4}-\d{2}-\d{2}|\d{8}|(고시 )?제?\d{4}-\d+호?)$/;
const productTerms=terms=>terms.filter(term=>Array.from(term).length>=MIN_PRODUCT_TERM&&!NOTICE_TERM.test(term));
// Code-point pairs of letters and digits, as build_site indexes them (Python str.isalnum()).
const INDEXED_GRAM=/^[\p{L}\p{N}]{2}$/u;
function gramsOf(term){const chars=Array.from(term),grams=new Set();for(let i=0;i+1<chars.length;i++){const gram=chars[i]+chars[i+1];if(INDEXED_GRAM.test(gram))grams.add(gram)}return[...grams]}
const gramBucket=gram=>{const[a,b]=Array.from(gram);return(a.codePointAt(0)*65599+b.codePointAt(0))%GRAM_BUCKETS};
// A block holding a term holds every fragment of it, so the blocks listed under all of the term's fragments
// are a complete candidate set. load(path) resolves a parsed file under mfds/. Null when a term has no
// indexed fragment (e.g. 'l-'), so only the full index can answer it.
async function candidateBlocks(terms,load){
  if(terms.some(term=>!gramsOf(term).length))return null;
  const perTerm=await Promise.all(terms.map(async term=>{
    const lists=await Promise.all(gramsOf(term).map(gram=>load(`grams/${gramBucket(gram)}.json`).then(file=>file.grams[gram]||[])));
    return lists.reduce((found,list)=>{const keep=new Set(list);return found.filter(id=>keep.has(id))});
  }));
  return[...new Set(perTerm.flat())].sort((a,b)=>a-b);
}
// Joins blocks into the full index's shape, in global row order, so ranking ties break as in the full index.
function mergeBlocks(blocks){
  const dictFields=['entp_name','main_item_ingr','main_item_ingr_eng','permit_date','withdrawn'];
  const rows=[];
  for(const block of blocks)for(let i=0;i<block.row.length;i++)rows.push([block.row[i],block,i]);
  rows.sort((a,b)=>a[0]-b[0]);
  const index={dicts:{},item_seq:[],item_name:[],revision_count:[],content_group:[]},ids={};
  for(const field of dictFields){index.dicts[field]=[];index[field]=[];ids[field]=new Map()}
  for(const[,block,i]of rows){
    for(const field of['item_seq','item_name','revision_count','content_group'])index[field].push(block[field][i]);
    for(const field of dictFields){
      const value=block[field][i];let id=ids[field].get(value);
      if(id===undefined){id=index.dicts[field].length;ids[field].set(value,id);index.dicts[field].push(value)}
      index[field].push(id);
    }
  }
  return index;
}
// {index, matches} for terms of at least MIN_PRODUCT_TERM characters. fullIndex() resolves the built full index.
async function productResults(terms,load,fullIndex){
  const ids=await candidateBlocks(terms,load);
  const index=!ids||ids.length>FULL_INDEX_BLOCKS?await fullIndex():buildMfdsIndex(mergeBlocks(await Promise.all(ids.map(id=>load(`blocks/${id}.json`)))));
  return {index,matches:matchMfds(index,terms)};
}
// The group's shown product: best tier, then a marketed one, then the bucket's order (oldest permit first).
// withdrawn counts the other products that are no longer marketed.
function groupRepresentative(index,bucket){
  const off=match=>index.withdrawn.table[index.withdrawn.ids[match[1]]]?1:0;
  let best=bucket[0],withdrawn=0;
  for(const match of bucket)if(match[0]<best[0]||(match[0]===best[0]&&off(match)<off(best)))best=match;
  for(const match of bucket)if(match!==best)withdrawn+=off(match);
  return {best,withdrawn};
}
const compact=text=>String(text).replace(/[\s·ㆍ]/g,'');
// '포시가정10밀리그램(다파글리플로진프로판디올수화물)' -> '포시가정10밀리그램' when the parentheses repeat an ingredient
// shown in the group heading; other notes such as '(수출용)' stay.
function shortProductName(name,ingredientRaw){
  const match=/^(.*\S)\s*\(([^()]*)\)$/.exec(name);
  if(!match)return name;
  const inside=compact(match[2]);
  return ingredientParts(ingredientRaw).some(part=>{const head=compact(part).slice(0,3);return head.length===3&&inside.includes(head)})?match[1]:name;
}
// Puts '1. … 2. … 3. …' items of one paragraph on their own lines. Only the next number in sequence starts a
// line, so a reference such as '11. 전문가를 위한 정보' inside item 1 stays in place.
function numberedLines(text){
  let next=1;
  return String(text).replace(/(^|\s+)(\d{1,2})\.\s/g,(all,space,number)=>{
    if(Number(number)!==next)return all;
    next++;
    return(next===2&&!space?'':'\n')+number+'. ';
  }).replace(/^\n/,'');
}
return {productBase,productBaseLabel,groupRepresentative,shortProductName,numberedLines,MFDS_DETAIL_URL,GRAM_BUCKETS,searchTerms,NOTICE_TERM,FULL_INDEX_BLOCKS,MIN_PRODUCT_TERM,productTerms,gramsOf,gramBucket,candidateBlocks,mergeBlocks,productResults,MFDS_GROUP_LIMIT,CRITERIA_LIMIT,TRUNCATED,dateLabel,baseName,brandName,searchableCriterion,distinctClassHeader,byNewest,groupsBy,groupCriteria,buildMfdsIndex,matchMfds,materializeMfds,ingredientParts,ingredientLabel,indicationGroups};
})();
if(typeof module!=='undefined'&&module.exports)module.exports=SearchCore;
