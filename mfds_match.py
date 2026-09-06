"""고시 검색어와 식약처 허가 품목을 API 호출 없이 로컬로 매칭하는 규칙.

`/tmp/kdrc-match-prototype.py`(리더 시제품)에서 출발했다. 알려진 한계 3가지를 검증하며
고쳤다:

1. **fallback 품명 매칭이 3건뿐이던 문제** — `words()`가 `[^a-z0-9]+`로 쪼개면서 한글은
   전부 구분자 취급되어 한글 품명 fallback이 통째로 사라졌다(`디클렉틴장용정` → 토큰 0개).
   한글 음절(`\\uac00-\\ud7a3`)을 유지어로 인정하도록 고쳐 47건으로 늘었다.
2. **접두 매칭 오탐 위험** — 원래 코드는 `key.startswith(component) or component.startswith(key)`로
   양방향이었다. `component.startswith(key)` 쪽은 색인에 있는 아무 짧은 키(`ammonium`,
   `bismuth`, `human` 등)가 길고 무관한 검색어 성분의 접두어이기만 하면 걸려버린다
   (`Ammonium lactate`가 `Ammonium Chloride`/`Ammonium Glycyrrhizinate`까지 끌어옴,
   `Antithrombin III, human`의 `human` 토큰이 90건을 끌어옴). `key.startswith(component)`
   방향만 남겼다 — 검색어 성분명이 색인의 더 긴 성분명(염·수식어가 그대로 붙은 키)의
   접두어인 경우만 허용한다. 이 방향은 실제로 표본 검사 결과 전부 같은 활성 성분의
   미분리(micronized)/추가 염(bromide, esilate, phosphate, hydrogen 등) 변형이었다
   (`Tiotropium`→`Tiotropium Bromide`, `Nintedanib`→`Nintedanib Esilate` 등). 길이
   8자 미만은 접두 매칭을 하지 않는다(오탐 여지가 큰 짧은 성분명 보호).
3. **복합제 교집합이 단일 성분 매칭을 막는지** — `components()`는 `+`/`,`/`/`/" 및 "/" and "`로
   검색어를 쪼개 부분마다 조회한 뒤 교집합을 취한다. 단일 성분 검색어(구분자 없음)는
   `parts`가 한 원소뿐이라 "교집합"이 곧 그 성분의 전체 색인이 된다 — 복합제 품목도
   포함된다(복합제의 `MAIN_INGR_ENG`을 `/`로 쪼개 성분별로 색인했기 때문). 테스트로
   `Metformin` 단일 검색어가 `Dapagliflozin/Metformin` 복합제 품목도 잡는지 확인했다.
4. **접두 매칭이 중간에 낀 단어를 못 잡는 문제**(gen2에서 리더가 발견) — `Ginkgo biloba
   extract` → `ginkgobilobaextract`는 `Ginkgo Biloba Leaf Dried Extract` →
   `ginkgobilobaleafdriedextract`와 앞도 뒤도 접두 관계가 아니다(`leaf dried`가
   중간에 낀다). 실측 결과 저장분 24,596건 중 gen1이 놓친 19,260건 가운데 이 유형이
   상당수였다(은행엽건조엑스 계열만 300건대). **토큰 부분집합 매칭**을 추가했다 —
   검색어의 정규화 토큰 집합이 색인 키의 토큰 집합의 부분집합이면 매칭
   (`{ginkgo,biloba,extract} ⊆ {ginkgo,biloba,leaf,dried,extract}`). 정확 일치 →
   접두 매칭 → 토큰 부분집합 순으로 시도한다(뒤로 갈수록 느슨하다).

   **과매칭 방어**: 토큰이 하나뿐인 검색어는 위험하다 — `human`은 국내 유통 51개
   화합물(대부분 무관: Erythropoietin, Papilloma Virus 단백질, Rotavirus 등)의
   공통 토큰이라 `Antithrombin III, human`이 `human` 하나로 좁혀지면 90여 건을
   무관하게 쓸어온다. 실측으로 두 가지 방어를 확인했다:
   - 길이 6자 미만 단일 토큰은 부분집합 매칭에서 제외한다(`제제`처럼 조사·조각어
     보호. 실제 검색어 중 가장 짧은 유효 단일 토큰은 `ferric`=6자였다).
   - `GENERIC_TOKEN_STOPWORDS`(human, recombinant, live, attenuated, murine,
     bovine, porcine, rabbit, anti, monoclonal, purified, normal, freeze, dried
     등 생물학적 제제에 흔한 수식어)는 길이와 무관하게 단일 토큰 매칭에서 제외한다.
     335개 실제 검색어를 전수 조사한 결과 이 스톱워드에 걸리는 단일 토큰 검색어는
     `Antithrombin III, human`의 `human` 하나뿐이었다(의도대로 실패 처리됨) —
     `Bismuth`(7자, 화합물 4종·165건), `Aspirin`(7자, 5종·113건), `Ferric citrate`
     (`ferric` 6자, 8종·38건), `Albumin`(7자, 1종) 등 다른 짧은 단일 토큰은 전부
     정상적으로 매칭되고, 표본 검사 결과 매칭된 화합물은 전부 검색어와 실제로
     관련된 변형(같은 활성 성분의 다른 염류)이었다.

   **결함**(리더 검증 중 발견) — 이 스톱워드 방어가 `Antithrombin III, human`
   전체를 실패 처리해버렸다. 그런데 우주에 국내 품목 2건이 실재한다(`안티트롬빈Ⅲ주
   500아이유`, `에스케이항트롬빈III주500단위`, 둘 다 EDI 코드 보유, 인덱스 성분명
   `Human Antithrombin Ⅲ Concentrate`). 원인은 두 가지가 겹쳐 있었다: (1) 로마
   숫자 `Ⅲ`(U+2162)가 `GREEK` 정규화 표에 없어 `words()`가 그냥 삼켰다 —
   검색어 쪽 `Antithrombin III`(아스키 `III`)는 토큰 `{antithrombin, iii}`가
   나오는데 인덱스 쪽 `Human Antithrombin Ⅲ Concentrate`는 `Ⅲ`가 사라져
   `{human, antithrombin}`이 나와 애초에 `iii` 토큰이 서로 어긋났다. `Ⅰ`~`Ⅹ`
   로마 숫자 유니코드 문자를 아스키 로마 숫자로 정규화하는 항목을 `GREEK`에
   추가했다. (2) `components()`가 `human` 컴포넌트를 스톱워드로 걸러내지 않고
   그대로 교집합에 넣어, `antithrombiniii` 쪽이 매칭돼도 `human` 쪽 빈 집합과의
   교집합이 공집합이 됐다. `match_terms()`에서 컴포넌트 토큰 집합 전체가
   `GENERIC_TOKEN_STOPWORDS`뿐인 컴포넌트(`human` 단독)는 교집합 대상에서
   제외하도록 고쳤다 — 스톱워드 토큰 자체를 없애는 게 아니라(단일 토큰 매칭
   방어는 그대로 유지), **나머지 유의미한 컴포넌트(`antithrombiniii`)만으로
   충분히 특이하면 매칭**하게 했다. 복합제 검색어처럼 모든 컴포넌트가
   스톱워드뿐이면(현실에 없음) 교집합 대상이 빈 리스트가 되어 실패 처리된다
   (기존 단일 토큰 방어와 동일하게 안전). 전량 우주 재측정 결과 `human` 토큰을
   가진 다른 150개 무관 품목(Erythropoietin·Albumin 등)은 여전히 매칭되지
   않았다 — `antithrombiniii`가 그 품목들의 인덱스 키에 없기 때문이다.
5. **무기염이 양이온으로 과하게 묭리는 문제**(토큰 부분집합 도입 직후 실측에서 발견) —
   `Ammonium lactate`가 `Ammonium Chloride`가 들어간 기침약 23건을 무관하게 쓸어왔다.
   원인: `base()`/`token_set()`이 수식어 끝자리를 반복 제거하면서 `Sodium Chloride`,
   `Ammonium Lactate`, `Potassium Citrate`처럼 **양이온+음이온 접미어 두 단어뿐인**
   성분명은 양이온(`sodium`, `ammonium` 등)도 음이온 접미어(`chloride`, `lactate` 등)도
   둘 다 QUALIFIERS에 있어 `ammonium` 하나로만 남았다 — 서로 다른 염류가 같은
   기본형으로 붕가진 셔이다. `_strip_qualifier_tokens()`에 보호 규칙을 추가했다 —
   토큰이 정확히 [양이온(`SALT_CATIONS`), 음이온 접미어] 두 개뿐이면 둘 다 보존한다
   (`Sodium Chloride` → `sodiumchloride`, `Ammonium Lactate` → `ammoniumlactate`).
   반면 `Diclofenac Sodium`처럼 양이온이 세 번째 단어로 붙는 일반적인 염 접미어는
   여전히 정상 제거된다(→ `diclofenac`). `Ammonium lactate`가 더 이상 `Ammonium Chloride`를
   끌어오지 않는다는 것을 회귀 테스트로 고정했다.

로컬 성분명 정규화는 영문 `MAIN_INGR_ENG`을 기준으로 한다(보유율 99.8%). 성분명 뒤에
붙는 염·수화물·부형·제형 수식어는 **단어 단위로** 뗀다(문자열을 이어붙인 뒤 접미어
문자열을 자르면 `sodium bicarbonate` → `sodium`처럼 오작동한다).

## 자료구조
- `Index` — `(eng_index, token_index, name_index)`.
  - `eng_index: dict[str, set[str]]` — 정규화된 영문 성분 기본형(붙여쓰기) → 매칭되는
    `ITEM_SEQ` 집합. 정확/접두 일치에 쓴다.
  - `token_index: dict[frozenset[str], set[str]]` — 정규화된 영문 성분 토큰 집합 → 매칭되는
    `ITEM_SEQ` 집합. 검색어 토큰 집합이 색인 키의 부분집합이면 매칭된다(중간에 단어가
    낀 상황 대응). 복합제는 `MAIN_INGR_ENG`을 `MULTIPART_INGR_SEPARATOR`(`/`와 가운데점
    계열)로 쪼개 성분마다 양쪽 색인에 등록한다.
  - `name_index: list[tuple[str, str]]` — (영숙자+한글만 남긴 소문자 품명, `ITEM_SEQ`).
    fallback 품명 부분 문자열 검색에 쓴다.
- `MatchResult` — `matched: set[str]`(매칭된 품목 seq), `matched_terms: int`,
  `by_ingredient: int`, `by_name: int`, `failed_terms: list[str]`(매칭 실패한 검색어 head,
  순서 보존, 조용한 유실 없음).

## 범용 성분 판단(메트포르민·아세트아미노펜·피리독신·감초)
`data/normalized/*.json`의 고시 제목(`entries[].title`)을 표본 확인한 결과, 이 네
성분은 **영문 검색어로도 한글로도 고시 제목에 등장하지 않는다**(`아세트아미노펜`,
`피리독신`, `감초`, `Metformin` 단독 검색어 0건 — 유일하게 걸린 건 다른 성분과의
복합제 `Pyridoxine Hydrochloride + Doxylamine Succinate`). 유일하게 메트포르민이
실제 고시 본문에 등장하는 곳은 `[일반원칙]당뇨병용제` 블록으로, 검색어는
'경구용 당뇨병치료제' 계열 복합제(`글리메피리드+메트포르민` 등)이지 메트포르민
단일 성분 검색어가 아니다.

기존 규칙(`stored_seed_sets`+`is_related`)이 이 네 성분을 대량으로 끌어온 이유는
`is_related`가 "저장된 임의 품목의 기본 성분 집합 ⊆ 새 품목의 기본 성분 집합"이면
전부 유관 판정하기 때문이다 — 메트포르민이 포함된 복합제 하나가 저장되면 메트포르민
단일 성분 시드가 생기고, 그 시드는 메트포르민이 들어간 무관한 품목(설포닐우레아
복합제, 타 적응증 제네릭 등) 전부를 유관 처리한다. 이건 리더의 `stored_seed_sets`
docstring이 지적한 "A → A+B → B+C → C 확장" 패턴 그대로다. **고시 제목에 성분명이
직접 등장하지 않는 검색어는 규칙에 넣지 않는다** — 범용 성분 단일 검색어를 인위적으로
추가하지 않았다. 최종 규칙은 `term_groups_from_titles`가 만든 검색어만 그대로 쓴다.

## 제외 규칙
- `수출용`이 품명에 든 품목(4,082건)은 **제외한다**. 우주 덤프 표본 확인 결과 이
  품목들의 99.4%(4,057/4,082)가 `EDI_CODE`(급여코드)가 없다 — 국내 급여 대상이
  아니라 순수 수출 전용 허가다. 이들만 갖는 성분 조합 상당수(576종)가 국내
  유통본에 아예 없다.
- `수출명`만 있고 `수출용`은 아닌 품목(676건, 예: `...(수출명:...)`)은 **제외하지
  않는다** — 국내 유통 품목에 수출용 별칭이 괄호로 덧붙은 것뿐이며 82.9%가
  `EDI_CODE`를 보유한다(국내 판매 중).
- 취소 품목(`CANCEL_DATE` 있음, 7,718건)은 **제외하지 않는다** — 과거 급여기준
  이력이 유효하다는 리더 지시를 그대로 따랐다.
"""
import collections
import re
from dataclasses import dataclass, field

GREEK = {
    "α": "alpha", "β": "beta", "γ": "gamma", "δ": "delta",
    "ⅰ": "i", "ⅱ": "ii", "ⅲ": "iii", "ⅳ": "iv", "ⅴ": "v",
    "ⅵ": "vi", "ⅶ": "vii", "ⅷ": "viii", "ⅸ": "ix", "ⅹ": "x",
}

# 성분명 뒤에 붙는 염·수화물·부형 수식어(단어 단위로 뗀다)
QUALIFIERS = {
    "hydrochloride", "hydrobromide", "dihydrochloride", "hcl", "hbr", "mesylate", "besylate",
    "besilate", "tosylate", "tosilate", "maleate", "fumarate", "tartrate", "succinate",
    "citrate", "acetate", "phosphate", "sulfate", "sulphate", "nitrate", "oxalate", "malate",
    "lactate", "gluconate", "stearate", "palmitate", "trifluoroacetate", "propanediol",
    "hydrate", "monohydrate", "dihydrate", "trihydrate", "hemihydrate", "sesquihydrate",
    "anhydrous", "mixture", "lactose", "sodium", "potassium", "calcium", "magnesium",
    "hemisuccinate", "dipropionate", "furoate", "valerate", "xinafoate", "chloride",
    "bromide", "etexilate", "micronized", "dispersion", "solution", "concentrate", "powder",
    "coated", "hydrogen",
}

# 검색어/성분명에 흔히 붙는 한글 제형·투여경로 수식어. 성분명이 아니므로 함께 뗼다.
KOREAN_FORM_WORDS = {
    "제제", "경구제", "주사제", "경구용", "경구", "서방형", "일반형", "복합경구제",
    "복합주사제", "시럽제", "시럽", "이식제", "외용제", "성장호르몬제", "조절제", "조제",
    "흡입제", "점안제", "좌제", "연고제", "액제", "패취제", "비강분무제",
}

# 토큰 부분집합 매칭에서 단일 토큰으로는 사용하지 않는 생물학적/공정 수식어. 이 단어들은
# 수십 개의 무관한 화합물에 공통으로 붙어 있어(예: "human"은 Erythropoietin·Papilloma Virus
# 단백질·Rotavirus 등 51종의 공통 토큰) 단일 토큰으로 허용하면 과매칭한다.
GENERIC_TOKEN_STOPWORDS = {
    "human", "recombinant", "live", "attenuated", "murine", "bovine", "porcine",
    "rabbit", "anti", "monoclonal", "purified", "normal", "freeze", "dried",
}

# 토큰 부분집합 매칭에서 단일 토큰 검색어가 최소 이 길이는 되어야 허용한다(짧은 단어일수록
# 무관한 화합물을 붙잡을 위험이 커진다). 실제 335개 검색어 중 가장 짧은 유효 단일 토큰은
# `ferric`(6자)이었다.
SINGLE_TOKEN_MIN_LENGTH = 6

# 무기염 양이온(이 단어들은 QUALIFIERS에도 들어있다 — `Diclofenac Sodium`처럼
# 유기물에 붙는 염 접미어로도 쓰이기 때문이다). 수식어가 정확히 [양이온, 음이온
# 접미어](예: `Sodium Chloride`, `Ammonium Lactate`, `Potassium Citrate`) 두 단어뿐이면
# 순수 무기염 염류로 보고 둘 다 남겨야 한다 — 그러지 않으면 `Ammonium Lactate`와 `Ammonium
# Chloride`가 둘 다 `ammonium` 하나로 묶여 서로 무관한 품목을 섞어 매칭한다
# (실측으로 확인된 오탐: `Ammonium lactate` 검색어가 `Ammonium Chloride` 종합감기약
# 23건을 무관하게 쓸어옴).
SALT_CATIONS = {"sodium", "potassium", "calcium", "magnesium", "ammonium"}

DOSE = re.compile(r"\d+(?:[.,]\d+)?\s*(?:mg|g|mcg|㎍|iu|ml|밀리그램|밀리그람|그램|단위|만단위|%)", re.I)
# 가운데점 계열(· U+00B7, ‧, •, ∙). 우주 성분명(`MAIN_INGR_ENG`)과 검색어 양쪽 다
# 복합제 성분 구분자로 쓴다(예: 우주 키 `Sacubitril·Valsartan Sodium Hydrate`, 검색어
# `Sacubitril· Valsartan`). `및`/`and`는 여기 넣지 않는다 — 우주 성분명에 `Root and Rhizome`처럼
# 단일 성분 명칭에 " and "가 박혀 들어가는 경우(한약 생약재 173건)가 있어 `build_index`에서
# 이 구분자를 쓰면 성분명이 잘려나간다.
MULTIPART_INGR_SEPARATOR = re.compile(r"[/·‧•∙]")
# 영숫자와 한글 사이 경계에 공백을 끼워 붙어버린 두 단어를 분리한다(예: Paricalcitol주사제).
LATIN_HANGUL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[\uac00-\ud7a3])|(?<=[\uac00-\ud7a3])(?=[a-z0-9])")
WORD_SPLIT = re.compile(r"[^a-z0-9\uac00-\ud7a3]+")

EXPORT_ONLY = re.compile(r"수출용")


def words(value: object) -> list[str]:
    """영숫자 토큰과 한글 토큰으로 쪼갠다. 한글은 구분자가 아니라 유효 문자다."""
    text = str(value or "").lower()
    for greek, roman in GREEK.items():
        text = text.replace(greek, roman)
    text = DOSE.sub(" ", text)
    text = LATIN_HANGUL_BOUNDARY.sub(" ", text)
    return [w for w in WORD_SPLIT.split(text) if w]


def _strip_qualifier_tokens(tokens: list[str]) -> list[str]:
    """수식어 단어(염·수화물·제형)를 떼고 남은 핵심 단어 리스트를 반환한다.

    끝자리부터 수식어를 하나씩 떼어 나가지만, 남은 두 단어가 `[양이온,
    음이온 접미어]`(예: `Ammonium Lactate`)가 되는 순간 멈추고 둘 다 보존한다 —
    음이온 접미어도 QUALIFIERS에 있어 보호 없이 떼면 `Ammonium Lactate`와 `Ammonium
    Chloride`가 둘 다 같은 `ammonium` 하나로 묶여 서로 무관한 품목을 섞어
    매칭하게 된다(세 단어짜리 `Ammonium Lactate Solution`도 `solution`만 떼어내고
    반드시 이 보호를 거친다). 보호 대상이 아닌 경우 끝자리 수식어를 계속 제거한
    뒤 남은 단어 중 수식어가 아닌 것만 남긴다. 전부 떼어 비면 원래 단어를 쓴다
    (성분명 자체가 QUALIFIERS 단어로만 구성된 경우 보호).
    """
    tokens = list(tokens)
    quals = QUALIFIERS | KOREAN_FORM_WORDS
    while len(tokens) > 1 and tokens[-1] in quals:
        if len(tokens) == 2 and tokens[0] in SALT_CATIONS:
            break
        tokens.pop()
    if len(tokens) == 2 and tokens[0] in SALT_CATIONS and tokens[1] in quals:
        return tokens
    filtered = [w for w in tokens if w not in quals]
    return filtered or tokens


def base(value: object) -> str:
    """수식어 단어(염·수화물·제형)를 떼고 남은 핵심 성분명(붙여쓰기)."""
    return "".join(_strip_qualifier_tokens(words(value)))


def token_set(value: object) -> frozenset[str]:
    """수식어 단어(염·수화물·제형)를 떼어 남은 단어 집합으로 남긴다(`base()`와 달리 순서·
    사이 단어에 무관하다).
    """
    return frozenset(_strip_qualifier_tokens(words(value)))


def components(term: object) -> list[tuple[str, frozenset[str]]]:
    """복합제 검색어를 성분별 (붙여쓼 기본형, 토큰 집합) 쌍으로 쪼개다.

    단일 성분 검색어(구분자 없음)는 원소 하나짜리 리스트이다.
    """
    parts = re.split(r"[+,/]|[·‧•∙]| 및 | and ", str(term or ""))
    out: list[tuple[str, frozenset[str]]] = []
    for p in parts:
        b = base(p)
        if b and len(b) >= 4:
            out.append((b, token_set(p)))
    return out


def is_export_only(item_name: object) -> bool:
    """'수출용' 품목만 True. '수출명'만 붙은 국내 유통 품목은 포함하지 않는다."""
    return bool(EXPORT_ONLY.search(str(item_name or "")))


@dataclass
class Index:
    """`build_index`의 반환 자료구조. 필드는 읽기 전용으로 다룬다."""

    eng_index: dict[str, set[str]] = field(default_factory=lambda: collections.defaultdict(set))
    token_index: dict[frozenset[str], set[str]] = field(
        default_factory=lambda: collections.defaultdict(set)
    )
    name_index: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class MatchResult:
    """`match_terms`의 반환 자료구조. 매칭 실패 검색어를 조용히 버리지 않는다."""

    matched: set[str]
    matched_terms: int
    by_ingredient: int
    by_name: int
    failed_terms: list[str]


def build_index(universe_rows: list[dict]) -> Index:
    """식약처 허가 품목 전량에서 로컬 매칭용 색인을 만든다.

    `수출용` 품목은 색인 단계에서 제외한다(EDI_CODE 보유율 0.6% — 국내 급여 대상이
    아님). 취소 품목은 제외하지 않는다(과거 급여기준 이력이 유효함).
    """
    eng_index: dict[str, set[str]] = collections.defaultdict(set)
    token_index: dict[frozenset[str], set[str]] = collections.defaultdict(set)
    name_index: list[tuple[str, str]] = []
    for row in universe_rows:
        if is_export_only(row.get("ITEM_NAME")):
            continue
        seq = str(row["ITEM_SEQ"])
        for part in MULTIPART_INGR_SEPARATOR.split(str(row.get("MAIN_INGR_ENG") or "")):
            key = base(part)
            if key:
                eng_index[key].add(seq)
            tokens = token_set(part)
            if tokens:
                token_index[tokens].add(seq)
        name_index.append(("".join(words(row.get("ITEM_NAME"))), seq))
    return Index(eng_index=eng_index, token_index=token_index, name_index=name_index)


def lookup(
    eng_index: dict[str, set[str]],
    token_index: dict[frozenset[str], set[str]],
    component: str,
    tokens: frozenset[str],
) -> set[str]:
    """정규화된 성분으로 품목을 찾는다. 정확 일치 ∪ 접두 일치를 먼저 잡고, 둘 다
    비어있을 때만 토큰 부분집합으로 널힌다(뒤로 갈수록 느슨하다).

    1. 정확 일치: `eng_index[component]`.
    2. 접두 일치: 색인 키가 `component`로 시작하는 경우만(예: 검색어 `Tiotropium`
       → 색인 키 `Tiotropium Bromide`). 반대 방향(짧은 색인 키가 긴 검색어의 접두어)는
       오탐이 심해 제외했다 — `Ammonium`(검색어 성분)이 `Ammonium Chloride`/
       `Ammonium Glycyrrhizinate`(무관한 색인 키)까지 끌어오는 식이다. 길이 8자 미만
       성분명은 접두 매칭하지 않는다. **정확 일치가 있어도 접두 단계를 건너뛰지 않는다**
       — 검색어 `Clopidogrel`은 정확히 `Clopidogrel`로만 남는 이상한 2건과만 정확
       일치하지만, 실제 국내 유통은 거의 모두 `Clopidogrel Bisulfate` 등 염류 변형이라
       정확 일치만 보면 171건을 놓친다(실측으로 확인된 과소합).
    3. 토큰 부분집합: 1·2 단계가 뭐도 못 찾았을 때만 시도한다. `tokens`가 색인 키
       (토큰 집합)의 부분집합이면 매칭한다(중간에 단어가 낀 경우 대응: `Ginkgo biloba
       extract` → `Ginkgo Biloba Leaf Dried Extract`). 토큰이 하나뿐인 검색어는 과매칭
       위험이 커 `SINGLE_TOKEN_MIN_LENGTH` 미만 길이나 `GENERIC_TOKEN_STOPWORDS`에 속하면
       이 단계를 건너뛴다(예: `Antithrombin III, human`의 `human` 단독 토큰은
       여기서 거르게 된다).
    """
    hit = set(eng_index.get(component, ()))
    if len(component) >= 8:
        for key, seqs in eng_index.items():
            if key.startswith(component):
                hit |= seqs
    if hit:
        return hit
    if len(tokens) == 1:
        (tok,) = tokens
        if tok in GENERIC_TOKEN_STOPWORDS or len(tok) < SINGLE_TOKEN_MIN_LENGTH:
            return hit
    for key, seqs in token_index.items():
        if tokens <= key:
            hit |= seqs
    return hit


def match_terms(index: Index, groups: list[tuple[str, list[str]]]) -> MatchResult:
    """검색어 그룹을 색인과 매칭한다.

    각 그룹은 (head, fallbacks) — head를 성분 검색어로 우선 시도하고, 실패하면
    fallback 품명 부분 문자열로 재시도한다. 복합제 검색어는 모든 성분 토큰이
    각각 매칭돼야(교집합) 하나로 묶인 품목만 잡는다 — 성분 하나만 겹쳐도 잡으면
    범용 성분이 무관한 복합제까지 끌어온다(기존 `stored_seed_sets`가 겪은 문제).
    단일 성분 검색어는 성분 토큰이 하나뿐이라 그 성분이 든 모든 품목(단일제+복합제)을
    잡는다 — 이건 의도된 동작이다(예: `Metformin` 검색어는 메트포르민이 든 복합제도
    급여기준 대상이므로 잡아야 한다).

    교집합 대상에서 `GENERIC_TOKEN_STOPWORDS`로만 이루어진 컴포넌트(`significant`가
    거른 버린 컴포넌트)는 제외한다 — `Antithrombin III, human`의 `human`처럼
    수십 종의 무관 화합물에 공통으로 붙는 수식어는 교집합 조건으로 쓰면 대부분의
    무관 품목을 걸러낸다(그 토큰이 단독이라 집합이 되더라도 자체 허용되지도 못함).
    나머지 유의미한 컴포넌트(예: `antithrombiniii`)만으로 교집합을 요구하면 실제로
    관련있는 품목만 남고, 모든 컴포넌트가 스톱워드뿐이면(현실에 없음) 이전처럼
    실패 처리된다.
    """
    eng_index, token_index, name_index = index.eng_index, index.token_index, index.name_index
    matched: set[str] = set()
    by_ingredient = by_name = 0
    failures: list[str] = []
    for head, fallbacks in groups:
        found: set[str] = set()
        parts = components(head)
        significant = [
            (b, t) for b, t in parts if not (t and t <= GENERIC_TOKEN_STOPWORDS)
        ]
        if significant:
            sets = [lookup(eng_index, token_index, b, t) for b, t in significant]
            if all(sets):
                found = set.intersection(*sets)
        if found:
            by_ingredient += 1
        else:
            for fallback in fallbacks:
                needle = "".join(words(fallback))
                if len(needle) >= 3:
                    found = {seq for name, seq in name_index if needle in name}
                    if found:
                        break
            if found:
                by_name += 1
            else:
                failures.append(head)
        matched |= found
    return MatchResult(
        matched=matched,
        matched_terms=by_ingredient + by_name,
        by_ingredient=by_ingredient,
        by_name=by_name,
        failed_terms=failures,
    )
