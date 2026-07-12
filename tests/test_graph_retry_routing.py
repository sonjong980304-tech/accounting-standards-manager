# -*- coding: utf-8 -*-
"""rag.graph의 조건부 rewrite 재시도 라우팅 순수함수 검증(LLM/Index 없이 그래프 배선 로직만).

배경: rewrite는 원래 '매 질의 무조건 실행'이었으나, 두 가지 서로 다른 이유로 필요한 기능이라
      트리거를 분리한다.
      ① 히스토리(후속질문 맥락 해소)가 있으면 검색 점수와 무관하게 항상 필요.
      ② 히스토리가 없는 단발 질문은 1차 검색 결과가 부실할 때만(top score < 0.6) 재시도.
      재시도는 무한루프 방지를 위해 1회로 제한한다.
"""
from rag.graph import _after_rewrite_node, _entry_node, _need_rewrite_retry


def test_need_rewrite_retry_true_when_top_score_below_threshold():
    retrieved = [{"score": 0.3}]
    assert _need_rewrite_retry(retrieved, already_retried=False) is True


def test_need_rewrite_retry_false_when_top_score_at_or_above_threshold():
    retrieved = [{"score": 0.6}]
    assert _need_rewrite_retry(retrieved, already_retried=False) is False
    retrieved_high = [{"score": 0.9}]
    assert _need_rewrite_retry(retrieved_high, already_retried=False) is False


def test_need_rewrite_retry_never_twice_even_with_low_score():
    """무한루프 방지: 이미 재시도했으면 점수가 여전히 낮아도 다시 rewrite하지 않는다."""
    retrieved = [{"score": 0.01}]
    assert _need_rewrite_retry(retrieved, already_retried=True) is False


def test_need_rewrite_retry_true_when_retrieved_is_empty():
    """검색 결과가 아예 없으면 점수 0.0으로 간주해 재시도한다."""
    assert _need_rewrite_retry([], already_retried=False) is True


def test_entry_node_goes_to_rewrite_when_history_present():
    """후속질문 맥락 해소는 검색 점수와 무관하게 항상 필요 → 히스토리 있으면 rewrite 먼저."""
    assert _entry_node(history=[{"role": "user", "content": "개발비는 어떤가?"}]) == "rewrite"


def test_entry_node_goes_to_route_when_no_history():
    assert _entry_node(history=[]) == "route"


def test_after_rewrite_node_goes_to_route_when_not_yet_routed():
    """히스토리 경로: rewrite가 route보다 먼저 실행됐으면 다음은 route."""
    assert _after_rewrite_node(route_already_set=False) == "route"


def test_after_rewrite_node_goes_to_retrieve_when_already_routed():
    """재시도 경로: route는 이미 끝났으므로 재시도는 retrieve로 바로 간다(route 재실행 안 함)."""
    assert _after_rewrite_node(route_already_set=True) == "retrieve"
