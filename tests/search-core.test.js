// Run with `node --test tests/*.test.js`.
const test = require('node:test');
const assert = require('node:assert');
const core = require('../assets/search-core.js');

// Same shape as build_site.encode_mfds_index output: one array per field, dictionary ids for repeated values.
const fieldOf = (row, field) => row[field];
const columnar = rows => {
  const dictFields = ['entp_name', 'main_item_ingr', 'main_item_ingr_eng', 'permit_date', 'withdrawn'];
  const index = { dicts: {} };
  for (const field of dictFields) {
    const table = [...new Set(rows.map(row => fieldOf(row, field)))];
    index.dicts[field] = table;
    index[field] = rows.map(row => table.indexOf(fieldOf(row, field)));
  }
  const groups = [...new Set(rows.map(row => row.content))];
  index.content_group = rows.map(row => groups.indexOf(row.content));
  index.item_seq = rows.map(row => row.seq);
  index.item_name = rows.map(row => row.name);
  index.revision_count = rows.map(row => row.revisions ?? 1);
  return index;
};
const product = (seq, name, overrides = {}) => ({
  seq, name, entp_name: '시험제약', main_item_ingr: '다파글리플로진', main_item_ingr_eng: 'Dapagliflozin',
  permit_date: '20200101', withdrawn: '', content: 'a', ...overrides,
});
const packed = () => columnar([
  product(1, '다파진정10밀리그램(다파글리플로진)', { revisions: 2 }),
  product(2, '무관정', { entp_name: '다른제약', main_item_ingr: '메트포르민', main_item_ingr_eng: 'Metformin',
                         permit_date: '20210202', content: 'b' }),
]);

// Reference definition of the tiers matchMfds must produce.
const mfdsTier = (item, terms) => {
  const exact = [item.entp_name, item.main_item_ingr, item.main_item_ingr_eng].join('|').toLocaleLowerCase('ko');
  if (terms.some(t => item.brand === t)) return 0;
  if (terms.some(t => item.brand.startsWith(t))) return 1;
  if (terms.some(t => item.nameKey.includes(t))) return 2;
  if (terms.some(t => exact.includes(t))) return 3;
  return 4;
};
const mfds = (row = 0) => core.materializeMfds(core.buildMfdsIndex(packed()), row, 0);

test('brandName strips strength and parentheses and lowercases', () => {
  assert.equal(core.brandName('다파진정10밀리그램(다파글리플로진)'), '다파진정');
  assert.equal(core.brandName('슈글렛정50밀리그램(이프라글리플로진L-프롤린)'), '슈글렛정');
  assert.equal(core.brandName('ABC Tab 5mg'), 'abc tab');
  assert.equal(core.brandName('무관정'), '무관정');
});

test('dateLabel formats YYYYMMDD with hyphens', () => {
  assert.equal(core.dateLabel('20260901'), '2026-09-01');
});

test('reference tiers go from exact brand match to no match', () => {
  const item = mfds();
  assert.equal(mfdsTier(item, ['다파진정']), 0);
  assert.equal(mfdsTier(item, ['다파진']), 1);
  assert.equal(mfdsTier(item, ['10밀리그램']), 2);
  assert.equal(mfdsTier(item, ['dapagliflozin']), 3);
  assert.equal(mfdsTier(item, ['zzz']), 4);
  assert.equal(mfdsTier(item, ['zzz', '다파진정']), 0);
});

test('buildMfdsIndex keeps columns and precomputed search keys', () => {
  const idx = core.buildMfdsIndex(packed());
  assert.equal(idx.n, 2);
  assert.deepEqual(idx.names, ['다파진정10밀리그램(다파글리플로진)', '무관정']);
  assert.deepEqual(idx.brands, ['다파진정', '무관정']);
  assert.deepEqual(idx.entp.table, ['시험제약', '다른제약']);
  assert.deepEqual([...idx.entp.ids], [0, 1]);
  assert.deepEqual(idx.ingrEngLow, ['dapagliflozin', 'metformin']);
});

test('matchMfds returns rows ordered by tier and drops non-matches', () => {
  const idx = core.buildMfdsIndex(packed());
  assert.deepEqual(core.matchMfds(idx, ['다파진정']), [[0, 0]]);
  assert.deepEqual(core.matchMfds(idx, ['다파진']), [[1, 0]]);
  assert.deepEqual(core.matchMfds(idx, ['10밀리그램']), [[2, 0]]);
  assert.deepEqual(core.matchMfds(idx, ['metformin']), [[3, 1]]);
  assert.deepEqual(core.matchMfds(idx, ['zzz']), []);
  assert.deepEqual(core.matchMfds(idx, []), []);
  // Terms are OR-ed.
  assert.deepEqual(core.matchMfds(idx, ['metformin', '다파진정']), [[0, 0], [3, 1]]);
});

test('matchMfds agrees with the reference tiers', () => {
  const idx = core.buildMfdsIndex(packed());
  for (const terms of [['다파진정'], ['다파진'], ['10밀리그램'], ['dapagliflozin'], ['zzz'], ['시험제약']]) {
    const viaScan = new Map(core.matchMfds(idx, terms).map(([tier, row]) => [row, tier]));
    for (let row = 0; row < idx.n; row++) {
      const expected = mfdsTier(core.materializeMfds(idx, row, 0), terms);
      assert.equal(viaScan.has(row) ? viaScan.get(row) : 4, expected, `terms=${terms} row=${row}`);
    }
  }
});

test('materializeMfds builds a row object', () => {
  const idx = core.buildMfdsIndex(packed());
  const item = core.materializeMfds(idx, 0, 2);
  assert.equal(item.item_seq, 1);
  assert.equal(item.entp_name, '시험제약');
  assert.equal(item.main_item_ingr, '다파글리플로진');
  assert.equal(item.permit_date, '20200101');
  assert.equal(item.revision_count, 2);
  assert.equal(item.withdrawn, '');
  assert.equal(item.source_url, 'https://nedrug.mfds.go.kr/pbp/CCBBB01/getItemDetail?itemSeq=1');
  assert.equal(item.tier, 2);
});

test('indicationGroups groups by ingredient, then by indication', () => {
  const index = core.buildMfdsIndex(columnar([
    product(1, '가정', { main_item_ingr: '성분A', permit_date: '20200202', content: 'k1' }),
    product(2, '나정', { main_item_ingr: '성분A', permit_date: '20200101', content: 'k1' }),
    product(3, '다정', { main_item_ingr: '성분A', permit_date: '20200202', content: 'k2' }),
    product(4, '라정', { main_item_ingr: '', content: 'k3' }),
  ]));
  const groups = core.indicationGroups(index, [[0, 0], [0, 1], [0, 2], [0, 3]]);
  assert.equal(groups.length, 2);
  assert.equal(groups[0].ingredient, '성분A');
  assert.equal(groups[0].groups.length, 2);
  // Same indication together, oldest permit first.
  assert.deepEqual(groups[0].groups[0].map(m => index.names[m[1]]), ['나정', '가정']);
  assert.equal(groups[1].ingredient, '성분 미상');
});

test('indicationGroups keeps tier order', () => {
  const index = core.buildMfdsIndex(packed());
  const groups = core.indicationGroups(index, [[0, 1], [3, 0]]);
  assert.deepEqual(groups.map(g => g.ingredient), ['메트포르민', '다파글리플로진']);
});

test('searchableCriterion builds a lowercase haystack', () => {
  const record = core.searchableCriterion({
    title: 'Dapagliflozin 경구제', body: '제2형 당뇨병', class_header: '[219] 당뇨병용제',
    effective_date: '20260601', notice_number: '2026-117',
  });
  assert.ok(record.hay.includes('dapagliflozin'));
  assert.ok(record.hay.includes('2026-06-01'));
  assert.ok(record.hay.includes('고시 제2026-117호'));
});

test('groupsBy keeps first-seen order', () => {
  const groups = core.groupsBy([{ k: 'b', n: 1 }, { k: 'a', n: 2 }, { k: 'b', n: 3 }], 'k');
  assert.deepEqual(groups.map(g => g.map(x => x.n)), [[1, 3], [2]]);
});

test('MFDS_GROUP_LIMIT is a positive number', () => {
  assert.equal(typeof core.MFDS_GROUP_LIMIT, 'number');
  assert.ok(core.MFDS_GROUP_LIMIT > 0);
});

const criterion = (key, title, date) => ({ key, title, effective_date: date, sequence: date });

test('groupCriteria ranks title matches before body-only matches', () => {
  const records = [
    criterion('class', '당뇨병용제', '20260601'),
    criterion('class', '당뇨병용제', '20260501'),
    criterion('drug', 'Dapagliflozin 경구제', '20240101'),
  ];
  const groups = core.groupCriteria(records, ['dapagliflozin']);
  assert.deepEqual(groups.map(g => g[0].key), ['drug', 'class']);
  assert.deepEqual(groups[1].map(r => r.effective_date), ['20260601', '20260501']);
  // Without a title match the order is newest first.
  assert.deepEqual(core.groupCriteria(records, ['zzz']).map(g => g[0].key), ['class', 'drug']);
  assert.deepEqual(core.groupCriteria(records).map(g => g[0].key), ['class', 'drug']);
});

test('ingredientLabel joins ingredients', () => {
  assert.equal(core.ingredientLabel('다파글리플로진|글리메피리드'), '다파글리플로진 + 글리메피리드');
  assert.equal(core.ingredientLabel(''), '성분 미상');
});

test('indicationGroups puts single ingredients before combinations within a tier', () => {
  const index = core.buildMfdsIndex(columnar([
    product(1, '가정', { main_item_ingr: '다파|메트', content: 'k1' }),
    product(2, '나정', { main_item_ingr: '다파', content: 'k2' }),
  ]));
  assert.deepEqual(core.indicationGroups(index, [[3, 0], [3, 1]]).map(g => g.ingredient), ['다파', '다파 + 메트']);
  // A better tier still wins over single-vs-combination.
  assert.deepEqual(core.indicationGroups(index, [[0, 0], [3, 1]]).map(g => g.ingredient), ['다파 + 메트', '다파']);
});

// Mirrors build_site.write_mfds_shards for a handful of rows.
const shards = (rows, blockRows) => {
  const files = {};
  const blocks = [];
  const order = rows.map((row, i) => i).sort((a, b) => rows[a].main_item_ingr.localeCompare(rows[b].main_item_ingr) || a - b);
  for (let start = 0; start < order.length; start += blockRows) {
    const ids = order.slice(start, start + blockRows), block = blocks.length;
    files[`blocks/${block}.json`] = { build: 'b', row: ids,
      ...Object.fromEntries(['item_seq', 'item_name', 'revision_count', 'entp_name', 'main_item_ingr', 'main_item_ingr_eng', 'permit_date', 'withdrawn']
        .map(field => [field, ids.map(i => field === 'item_seq' ? rows[i].seq : field === 'item_name' ? rows[i].name
          : field === 'revision_count' ? (rows[i].revisions ?? 1) : fieldOf(rows[i], field))])),
      content_group: ids.map(i => columnar(rows).content_group[i]) };
    blocks.push(ids);
    for (const i of ids) for (const text of [rows[i].name, rows[i].main_item_ingr, rows[i].main_item_ingr_eng, rows[i].entp_name]) {
      const chars = Array.from(text.toLowerCase());
      for (let k = 0; k + 1 < chars.length; k++) {
        const gram = chars[k] + chars[k + 1], path = `grams/${core.gramBucket(gram)}.json`;
        files[path] ??= { build: 'b', grams: {} };
        const list = files[path].grams[gram] ??= [];
        if (!list.includes(block)) list.push(block);
      }
    }
  }
  return path => Promise.resolve(files[path] ?? { build: 'b', grams: {} });
};

test('gramBucket matches build_site.gram_bucket', () => {
  assert.equal(core.gramBucket('다파'), 40);
  assert.deepEqual(core.gramsOf('다파글리'), ['다파', '파글', '글리']);
});

test('productResults returns what a full scan returns, in the same order', async () => {
  const rows = [
    product(1, '다파진정10밀리그램', { permit_date: '20200202' }),
    product(2, '메트정', { main_item_ingr: '메트포르민', main_item_ingr_eng: 'Metformin', content: 'b' }),
    product(3, '다파메트서방정', { main_item_ingr: '다파글리플로진|메트포르민', main_item_ingr_eng: 'Dapagliflozin/Metformin', content: 'c' }),
    product(4, '무관정', { main_item_ingr: '무관', main_item_ingr_eng: 'None', entp_name: '다른제약', content: 'd' }),
    product(5, '다파린정', { permit_date: '20190101' }),
  ];
  const full = core.buildMfdsIndex(columnar(rows));
  const load = shards(rows, 2);
  const key = (index, matches) => matches.map(([tier, row]) => `${tier}:${index.seqs[row]}`).join(',');
  for (const terms of [['다파'], ['메트포르민'], ['metformin'], ['다른제약'], ['다파진정'], ['없는말'], ['메트', '무관']]) {
    const result = await core.productResults(terms, load, async () => full);
    assert.equal(key(result.index, result.matches), key(full, core.matchMfds(full, terms)), terms.join(' '));
  }
  // Only blocks holding every fragment of the term are fetched.
  assert.deepEqual(await core.candidateBlocks(['무관'], load), [2]);
});

test('productResults falls back to the full index past FULL_INDEX_BLOCKS candidates', async () => {
  const rows = Array.from({ length: (core.FULL_INDEX_BLOCKS + 1) * 2 }, (_, i) => product(i + 1, `공통정${i}`, { content: `k${i}` }));
  let usedFull = false;
  const full = core.buildMfdsIndex(columnar(rows));
  const result = await core.productResults(['공통'], shards(rows, 1), async () => { usedFull = true; return full; });
  assert.ok(usedFull);
  assert.equal(result.matches.length, rows.length);
});

test('productTerms drops one-character terms', () => {
  assert.deepEqual(core.productTerms(['정', '다파', 'a', 'ab']), ['다파', 'ab']);
});

test('searchTerms keeps the input as one phrase; productTerms drops dates and notice numbers', () => {
  assert.deepEqual(core.searchTerms('  Dapagliflozin   경구제 '), ['dapagliflozin 경구제']);
  assert.deepEqual(core.searchTerms(' · , '), []);
  assert.deepEqual(core.productTerms(['2026-06-01', '20260601', '제2026-117호', '고시 제2026-117호', '2026-117', '다파', '10밀리그램']),
                   ['다파', '10밀리그램']);
});

test('a phrase with a space still finds its products through fragment files', async () => {
  const rows = [product(1, '다파진정 10밀리그램'), product(2, '다파진정'), product(3, '무관 10밀리그램', { main_item_ingr: '무관' })];
  const full = core.buildMfdsIndex(columnar(rows));
  const result = await core.productResults(['다파진정 10밀리그램'], shards(rows, 1), async () => full);
  assert.deepEqual(result.matches.map(([, row]) => full.seqs[row]), [1]);
});

test('fragments skip punctuation; a term with no indexed fragment uses the full index', async () => {
  assert.deepEqual(core.gramsOf('l-프롤린'), ['프롤', '롤린']);
  const rows = [product(1, 'a-b정'), product(2, '다파정')];
  let usedFull = false;
  const full = core.buildMfdsIndex(columnar(rows));
  const result = await core.productResults(['a-'], shards(rows, 1), async () => { usedFull = true; return full; });
  assert.ok(usedFull);
  assert.deepEqual(result.matches.map(([, row]) => full.seqs[row]), [1]);
});

// A few entries of build_site.PRODUCT_STRIPPED, longest first as the page receives them.
const SUFFIXES = ['프로판디올수화물', '포르메이트', '오수화물', '시트르산', '염산염', '나트륨', '수화물'];
test('productBase drops salts but keeps salts that name the drug', () => {
  assert.equal(core.productBase('다파글리플로진프로판디올수화물', SUFFIXES), '다파글리플로진');
  assert.equal(core.productBase('메만틴 염산염', SUFFIXES), '메만틴');
  assert.equal(core.productBase('아셀렌산나트륨오수화물', SUFFIXES), '아셀렌산나트륨');
  assert.equal(core.productBase('다파글리플로진시트르산(미분화)', SUFFIXES), '다파글리플로진');
  // An acid, inorganic or hydrogen stem means the salt is the drug itself.
  assert.equal(core.productBase('알긴산나트륨', SUFFIXES), '알긴산나트륨');
  assert.equal(core.productBase('탄산수소나트륨', SUFFIXES), '탄산수소나트륨');
  assert.equal(core.productBaseLabel('메트포르민염산염|다파글리플로진프로판디올수화물', SUFFIXES), '다파글리플로진|메트포르민');
});

test('indicationGroups puts salt variants under one ingredient heading', () => {
  const index = core.buildMfdsIndex(columnar([
    product(1, '가정', { main_item_ingr: '다파글리플로진프로판디올수화물', content: 'k1' }),
    product(2, '나정', { main_item_ingr: '다파글리플로진포르메이트', content: 'k1' }),
  ]));
  const groups = core.indicationGroups(index, [[3, 0], [3, 1]], SUFFIXES);
  assert.deepEqual(groups.map(g => g.ingredient), ['다파글리플로진']);
  assert.deepEqual(groups[0].groups.map(bucket => bucket.map(match => match[1])), [[0, 1]]);
});

test('groupRepresentative prefers a marketed product in the best tier and counts withdrawn others', () => {
  const index = core.buildMfdsIndex(columnar([
    product(1, '오리지널정', { withdrawn: '2024-04-25 취하' }),
    product(2, '제네릭정'),
    product(3, '다른제네릭정', { withdrawn: '2025-01-01 취소' }),
  ]));
  const { best, withdrawn } = core.groupRepresentative(index, [[3, 0], [3, 1], [3, 2]]);
  assert.equal(best[1], 1);
  assert.equal(withdrawn, 2);
  // A better tier still wins over marketing status.
  assert.equal(core.groupRepresentative(index, [[0, 0], [3, 1]]).best[1], 0);
  // All withdrawn: the first (oldest) stays.
  assert.equal(core.groupRepresentative(index, [[3, 0], [3, 2]]).best[1], 0);
});

test('shortProductName drops parentheses that repeat the ingredient only', () => {
  assert.equal(core.shortProductName('포시가정10밀리그램(다파글리플로진프로판디올수화물)', '다파글리플로진프로판디올수화물'), '포시가정10밀리그램');
  assert.equal(core.shortProductName('직듀오서방정(다파글리플로진/메트포르민염산염)', '메트포르민염산염|다파글리플로진'), '직듀오서방정');
  assert.equal(core.shortProductName('가정(수출용)', '다파글리플로진'), '가정(수출용)');
  assert.equal(core.shortProductName('나정', '다파글리플로진'), '나정');
});

test('numberedLines breaks only at the next item number', () => {
  const text = "1. 제 2형 당뇨병: '사용상의 주의사항, 11. 전문가를 위한 정보' 참고 2. 만성 심부전 3. 만성 신장병";
  assert.equal(core.numberedLines(text), "1. 제 2형 당뇨병: '사용상의 주의사항, 11. 전문가를 위한 정보' 참고\n2. 만성 심부전\n3. 만성 신장병");
  assert.equal(core.numberedLines('번호 없는 문장'), '번호 없는 문장');
  assert.equal(core.numberedLines('이 약은 1. 가 2. 나'), '이 약은\n1. 가\n2. 나');
});

test('productBaseLabel gives one label for a combination in any order', () => {
  assert.equal(core.productBaseLabel('메트포르민염산염|다파글리플로진프로판디올수화물', SUFFIXES),
               core.productBaseLabel('다파글리플로진프로판디올수화물|메트포르민염산염', SUFFIXES));
});
