# -*- coding: utf-8 -*-
"""감리지적사례 개정 자동 대조 단위 테스트 (작업 A).

audit-sentinel이 미리 계산해 준 standard_superseded 를 그대로 믿지 않고,
이 프로젝트가 실제로 보유한 현행 기준서 목록(data/parsed/3001.jsonl 의 standard_no)과
대조해 자체 재계산하는 로직을 검증한다.

실행: python3 -m pytest tests/test_revision_detect.py -q
"""
import json

import pytest

from rag import sync_audit_cases as S


@pytest.fixture
def fake_3001(tmp_path):
    """현행 기준서 목록 대역: 제1109호·제1116호만 존재하는 작은 3001.jsonl."""
    p = tmp_path / "3001.jsonl"
    with p.open("w", encoding="utf-8") as f:
        for no, name in (("1109", "금융상품"), ("1109", "금융상품"), ("1116", "리스")):
            f.write(json.dumps({"record_type": "paragraph", "standard_no": no,
                                "standard_name": name}, ensure_ascii=False) + "\n")
    return p


@pytest.fixture(autouse=True)
def _clear_cache():
    S.current_standard_nos.cache_clear()
    yield
    S.current_standard_nos.cache_clear()


# ------------------------------------------------------------ 호수 추출
def test_extract_single_standard_no():
    assert S.extract_standard_nos("기업회계기준서 제1039호(금융상품)") == ["1039"]


def test_extract_multiple_and_comma_list():
    assert S.extract_standard_nos("제1039호, 제1109호 및 제1115호") == \
        ["1039", "1109", "1115"]


def test_extract_allows_spaces_around_number():
    assert S.extract_standard_nos("제 1027 호") == ["1027"]


def test_extract_dedups_preserving_order():
    assert S.extract_standard_nos("제1109호 … 제1039호 … 제1109호") == \
        ["1109", "1039"]


def test_extract_ignores_chapter_notation():
    # 일반기업회계기준은 '제N장' 체계 → 호수 추출 대상 아님
    assert S.extract_standard_nos("일반기업회계기준 제16장(수익)") == []


def test_extract_empty_when_no_reference():
    assert S.extract_standard_nos("") == []
    assert S.extract_standard_nos("외부감사법 시행령") == []


# ------------------------------------------------------ 현행 기준서 목록 로드
def test_current_standard_nos_reads_standard_no_field(fake_3001):
    assert S.current_standard_nos(fake_3001) == frozenset({"1109", "1116"})


def test_current_standard_nos_missing_file_is_empty(tmp_path):
    assert S.current_standard_nos(tmp_path / "없음.jsonl") == frozenset()


def test_current_standard_nos_is_cached(fake_3001):
    first = S.current_standard_nos(fake_3001)
    fake_3001.write_text("", encoding="utf-8")   # 파일을 비워도
    assert S.current_standard_nos(fake_3001) is first   # 재읽기 없이 같은 객체 반환


def test_current_standard_nos_defaults_to_project_3001():
    # 인자 없이 호출하면 프로젝트의 실제 3001.jsonl 을 사용 (60개 호수 수집됨)
    nos = S.current_standard_nos()
    assert "1116" in nos and "1109" in nos
    assert len(nos) > 30


# ------------------------------------------------------------ 개정 판정
def test_superseded_false_when_all_nos_current(fake_3001):
    assert S.is_superseded("기업회계기준서 제1109호(금융상품)", fake_3001) is False


def test_superseded_true_when_no_missing_from_current(fake_3001):
    # 제1039호는 폐지되어 현행 목록(3001.jsonl)에 없음 → 개정 경고
    assert S.is_superseded("기업회계기준서 제1039호(금융상품)", fake_3001) is True


def test_superseded_true_when_any_of_several_missing(fake_3001):
    assert S.is_superseded("제1109호 및 제1039호", fake_3001) is True


def test_superseded_true_on_old_marker_even_if_current(fake_3001):
    # '舊' 표기가 있으면 호수가 현행이어도 무조건 경고
    assert S.is_superseded("舊 기업회계기준서 제1109호", fake_3001) is True


def test_superseded_true_on_old_marker_without_any_no(fake_3001):
    assert S.is_superseded("舊 일반기업회계기준 제16장", fake_3001) is True


def test_superseded_false_when_no_standard_no_extracted(fake_3001):
    # '제N장'만 있거나 참조가 없으면 판단 근거 없음 → 경고하지 않음(오탐 방지)
    assert S.is_superseded("일반기업회계기준 제16장(수익)", fake_3001) is False
    assert S.is_superseded("", fake_3001) is False
    assert S.is_superseded(None, fake_3001) is False


# ------------------------------------------------------------ convert() 통합
CASE = {
    "case_id": "FSS/2311-01",
    "title": "매출액 과소계상",
    "facts": "사실관계", "violation": "지적사항", "basis": "판단근거",
    "audit_gap": "감사 미비", "implication": "시사점",
    "standard": "기업회계기준서 제1039호(금융상품)",
    "source_url": "https://example.invalid/case",
    "standard_superseded": False,      # audit-sentinel 원본 플래그(구식)
    "fiscal_year": "2018",
}


def test_convert_overrides_stale_false_with_true(fake_3001):
    # 원본은 False 였지만 제1039호는 현행 목록에 없음 → 자체 판정 True 가 이긴다
    out = S.convert(CASE, fake_3001)
    assert out["standard_superseded"] is True


def test_convert_overrides_stale_true_with_false(fake_3001):
    # 원본이 True 여도 제1116호가 현행이면 자체 판정 False 가 이긴다
    rec = dict(CASE, standard="기업회계기준서 제1116호(리스)", standard_superseded=True)
    assert S.convert(rec, fake_3001)["standard_superseded"] is False


def test_convert_sets_flag_even_when_source_field_absent(fake_3001):
    rec = {k: v for k, v in CASE.items() if k != "standard_superseded"}
    assert S.convert(rec, fake_3001)["standard_superseded"] is True


def test_convert_keeps_other_carry_fields(fake_3001):
    out = S.convert(CASE, fake_3001)
    assert out["record_type"] == "audit_case"
    for f in ("case_id", "title", "facts", "violation", "basis",
              "audit_gap", "implication", "standard", "source_url", "fiscal_year"):
        assert out[f] == CASE[f]


def test_convert_drops_unlisted_source_fields(fake_3001):
    out = S.convert(dict(CASE, issue_area="수익인식", decision_year=2023), fake_3001)
    assert "issue_area" not in out and "decision_year" not in out


def test_convert_flag_is_chroma_scalar_bool(fake_3001):
    # to_metadata 가 bool 로 넘기므로 numpy/None 등이 섞이면 안 됨
    from rag import common as C
    md = C.to_metadata(S.convert(CASE, fake_3001), "audit_cases")
    assert isinstance(md["standard_superseded"], bool)
