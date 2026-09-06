import mfds_match as mm


def row(seq, name, ingr_eng, cancel_date=None, edi_code="640000001"):
    return {
        "ITEM_SEQ": seq,
        "ITEM_NAME": name,
        "ENTP_NAME": "테스트제약",
        "MAIN_ITEM_INGR": "[M000000]테스트성분",
        "MAIN_INGR_ENG": ingr_eng,
        "ITEM_PERMIT_DATE": "20200101",
        "CANCEL_DATE": cancel_date,
        "CANCEL_NAME": "취하" if cancel_date else "정상",
        "CHANGE_DATE": "20200101",
        "EDI_CODE": edi_code,
        "ATC_CODE": None,
    }


UNIVERSE = [
    row("1", "다파메트정10/1000밀리그램", "Dapagliflozin Propanediol Hydrate/Metformin Hydrochloride"),
    row("2", "다파글리단독정10밀리그램", "Dapagliflozin Propanediol Hydrate"),
    row("3", "메트포르민단일정500밀리그램", "Metformin Hydrochloride"),
    row("4", "생리식염주사액", "Sodium Chloride"),
    row("5", "중조정", "Sodium Bicarbonate"),
    row("6", "모다피닐정200mg(모다피닐)", "Modafinil"),
    row("7", "파브라자임주35밀리그램(아갈시다제베타)", "Agalsidase Beta (Recombinant Human Galactosidase)"),
    row("8", "수출전용정(수출용)", "Erythromycin Stearate", edi_code=None),
    row("9", "국내정(수출명:ExportBrand)", "Erythromycin Stearate", edi_code="640000009"),
    row("10", "취소된메트포르민정(메트포르민염산염)", "Metformin Hydrochloride", cancel_date="20240101"),
]


def build():
    return mm.build_index(UNIVERSE)


def test_salt_and_hydrate_suffix_stripped():
    idx = build()
    # Dapagliflozin Propanediol Hydrate -> base "dapagliflozin"과 매칭
    result = mm.match_terms(idx, [("Dapagliflozin", [])])
    assert result.matched == {"1", "2"}
    assert result.by_ingredient == 1
    assert result.failed_terms == []


def test_combination_component_intersection_and_single_ingredient_reach():
    idx = build()
    # 복합제 검색어: 두 성분 모두 있는 품목만(교집합)
    combo = mm.match_terms(idx, [("Dapagliflozin + Metformin", [])])
    assert combo.matched == {"1"}
    # 단일 성분 검색어: 복합제(1)와 단일제(3) 양쪽 다 잡는다(교집합 규칙이 이 경로를 막지 않음)
    single = mm.match_terms(idx, [("Metformin", [])])
    assert single.matched == {"1", "3", "10"}


def test_sodium_chloride_not_mangled_by_qualifier_stripping():
    idx = build()
    result = mm.match_terms(idx, [("Sodium Chloride", [])])
    assert result.matched == {"4"}
    # Sodium Bicarbonate가 잘못 걸리지 않는다("sodium"까지 잘려나가는 문자열 절단 버그 회귀)
    assert "5" not in result.matched


def test_dose_suffix_ignored():
    idx = build()
    result = mm.match_terms(idx, [("Modafinil 200mg", [])])
    assert result.matched == {"6"}


def test_greek_letter_normalized_to_roman():
    idx = build()
    result = mm.match_terms(idx, [("Agalsidase β 35mg", ["파브라자임주"])])
    assert result.matched == {"7"}
    assert result.by_ingredient == 1


def test_export_only_items_excluded():
    idx = build()
    # 인덱스 자체에 "수출용" 품목(8)이 없으므로 어떤 검색어로도 잡히지 않는다
    result = mm.match_terms(idx, [("Erythromycin", [])])
    assert "8" not in result.matched
    assert result.matched == {"9"}


def test_export_name_only_item_retained():
    idx = build()
    result = mm.match_terms(idx, [("Erythromycin", [])])
    assert "9" in result.matched


def test_cancelled_item_retained():
    idx = build()
    result = mm.match_terms(idx, [("Metformin", [])])
    assert "10" in result.matched


def test_korean_fallback_name_match_when_ingredient_lookup_fails():
    idx = build()
    # 성분명이 색인에 없는 검색어라도 한글 fallback 품명으로 잡혀야 한다(회귀:
    # 예전 words()는 [^a-z0-9]+로 쪼개 한글 fallback을 통째로 날렸다)
    result = mm.match_terms(idx, [("Nonexistent Ingredient XYZ", ["다파메트정"])])
    assert result.matched == {"1"}
    assert result.by_name == 1
    assert result.by_ingredient == 0


def test_failed_terms_are_reported_not_dropped():
    idx = build()
    result = mm.match_terms(idx, [("Fluvoxamine", []), ("Dapagliflozin", [])])
    assert result.failed_terms == ["Fluvoxamine"]
    assert result.matched_terms == 1
    assert "1" in result.matched and "2" in result.matched


def test_short_ingredient_no_bidirectional_prefix_false_positive():
    """짧은 성분명(Amino, 5자)이 무관한 긴 색인 키(Aminophylline)를 끌어오지 않는다.

    시제품의 양방향 접두 매칭(`component.startswith(key)`)은 색인의 아무 짧은 키가
    검색어 성분의 접두어이기만 하면 매칭됐다. 이제는 색인 키가 검색어의 접두어인
    방향만 허용하고, 길이 8자 미만은 접두 매칭 대상에서 제외한다.
    """
    idx = mm.build_index([
        row("20", "아미노정", "Amino"),
        row("21", "아미노필린정", "Aminophylline Hydrate"),
    ])
    result = mm.match_terms(idx, [("Amino", [])])
    # "amino"는 길이 5<8이라 접두 매칭 대상이 아니므로 Aminophylline까지 끌어오지 않고, 정확 키로만 잡는다(20번).
    assert result.matched == {"20"}


def test_prefix_match_same_active_ingredient_variant():
    """검색어 성분명이 색인의 더 긴(같은 활성 성분+추가 염) 키의 접두어면 매칭한다."""
    idx = mm.build_index([
        row("30", "티오트로피움흡입제", "Tiotropium Bromide Hydrate"),
    ])
    result = mm.match_terms(idx, [("Tiotropium", [])])
    assert result.matched == {"30"}


def test_token_subset_matches_ingredient_with_word_inserted_in_middle():
    """접두/정확 일치로는 못 잡는, 중간에 단어가 낀 성분명을 토큰 부분집합으로 잡는다.

    `Ginkgo biloba extract` -> `ginkgobilobaextract`는 실제 성분명 `Ginkgo Biloba Leaf
    Dried Extract` -> `ginkgobilobaleafdriedextract`와 앞도 뒤도 접두 관계가 아니다
    (`leaf dried`가 중간에 낀다). 토큰 집합 {ginkgo,biloba,extract}가 {ginkgo,biloba,
    leaf,dried,extract}의 부분집합이므로 매칭돼야 한다.
    """
    idx = mm.build_index([
        row("40", "징코민정(은행엽건조엑스)", "Ginkgo Biloba Leaf Dried Extract"),
    ])
    result = mm.match_terms(idx, [("Ginkgo biloba extract", [])])
    assert result.matched == {"40"}
    assert result.by_ingredient == 1


def test_token_subset_single_token_below_min_length_rejected():
    """토큰이 하나뿐인 검색어는 `SINGLE_TOKEN_MIN_LENGTH` 미만이면 부분집합 매칭에서 제외한다.

    보호가 없으면 짧은 단일 토큰이 무관한 긴 화합물명을 대량으로 끌어올 위험이 크다
    (실측: `ferric`=6자가 실제 최단 유효 단일 토큰이었다). 5자짜리 토큰은 제외된다.
    """
    idx = mm.build_index([
        row("41", "아미노산제제", "Amino Acid Complex"),
    ])
    result = mm.match_terms(idx, [("Amino", [])])
    assert result.matched == set()
    assert result.failed_terms == ["Amino"]


def test_token_subset_generic_stopword_rejected_even_if_long_enough():
    """`GENERIC_TOKEN_STOPWORDS`에 속한 단일 토큰은 길이가 충분해도 부분집합 매칭에서
    제외한다(예: `human`은 수십 종의 무관한 생물학적 제제에 공통으로 붙는다).
    """
    idx = mm.build_index([
        row("42", "사람에리스로포이에틴주", "Recombinant Human Erythropoietin"),
        row("43", "사람혈청알부민주", "Human Normal Serum Albumin"),
    ])
    result = mm.match_terms(idx, [("Antithrombin III, human", [])])
    # "human" 토큰은 스톱워드라 부분집합 매칭에서 제외되고, "antithrombiniii"는 색인에
    # 없으므로 무관한 Erythropoietin/Albumin 품목을 전혀 끌어오지 않는다.
    assert result.matched == set()
    assert result.failed_terms == ["Antithrombin III, human"]


def test_inorganic_salt_cation_anion_pair_not_collapsed_to_bare_cation():
    """`Ammonium Lactate`처럼 [양이온, 음이온 접미어] 두 단어짜리 무기염은 양이온
    하나로 뭉개지면 안 된다. 그러지 않으면 서로 무관한 `Ammonium Chloride`
    함유 품목까지 `Ammonium lactate` 검색어가 끌어온다(실측으로 확인된 회귀 오탐:
    `Ammonium lactate`가 종합감기약 `Ammonium Chloride` 성분 품목 23건을 무관하게
    끌어옴).
    """
    idx = mm.build_index([
        row("50", "암모늄락테이트로션", "Ammonium Lactate Solution"),
        row("51", "종합감기약", "Ammonium Chloride/Chlorpheniramine Maleate"),
    ])
    result = mm.match_terms(idx, [("Ammonium lactate", [])])
    assert result.matched == {"50"}
    assert "51" not in result.matched


def test_exact_match_does_not_suppress_prefix_broadening():
    """검색어 기본형이 색인에 정확히 일치하는 품목이 있어도, 접두 매칭으로 넓힐 수 있는
    같은 활성 성분의 다른 염류 변형을 놓치면 안 된다.

    회귀: 예전에는 정확 일치가 하나라도 있으면 접두 단계를 건너뛰어, `Clopidogrel`
    검색어가 정확히 `Clopidogrel`인 품목만 잡고 실제 대다수인 `Clopidogrel Bisulfate`
    변형을 놓쳤다.
    """
    idx = mm.build_index([
        row("60", "클로피도그렐단일정", "Clopidogrel"),
        row("61", "플라빅스정", "Clopidogrel Bisulfate"),
    ])
    result = mm.match_terms(idx, [("Clopidogrel", [])])
    assert result.matched == {"60", "61"}
