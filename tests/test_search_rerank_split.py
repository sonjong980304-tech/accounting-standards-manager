# -*- coding: utf-8 -*-
"""rag.search._rerank: 512토큰 넘는 (질문,문서) 쌍만 선택적으로 1024로 재토큰화하는
하이브리드 리랭킹 로직. 실제 CrossEncoder/토크나이저 로딩 없이 스텁으로 검증한다.
"""
from rag.search import RERANK_MAX_LEN, RERANK_MAX_LEN_LONG, _rerank


class _FakeTokenizer:
    """pair -> 사전 지정 토큰 길이만 흉내내는 스텁. model_max_length 변경 이력을 기록."""

    def __init__(self, lengths_by_pair):
        self.model_max_length = RERANK_MAX_LEN
        self._lengths = lengths_by_pair

    def __call__(self, pairs, truncation=False, padding=False):
        return {"input_ids": [[0] * self._lengths[p] for p in pairs]}


class _FakeReranker:
    """predict 호출마다 (전달된 pairs, 그 시점의 model_max_length)를 기록."""

    def __init__(self, lengths_by_pair):
        self.tokenizer = _FakeTokenizer(lengths_by_pair)
        self.predict_calls = []

    def predict(self, pairs):
        self.predict_calls.append((list(pairs), self.tokenizer.model_max_length))
        return [float(_idx_of(p)) for p in pairs]


def _pair(i):
    return ("q{}".format(i), "d{}".format(i))


def _idx_of(pair):
    return int(pair[0][1:])


def test_all_short_pairs_single_predict_call_at_512():
    pairs = [_pair(i) for i in range(5)]
    lengths = {p: 100 for p in pairs}
    fake = _FakeReranker(lengths)

    scores = _rerank(fake, pairs)

    assert len(fake.predict_calls) == 1
    called_pairs, max_len_at_call = fake.predict_calls[0]
    assert max_len_at_call == RERANK_MAX_LEN
    assert scores == [float(i) for i in range(5)]


def test_mixed_lengths_split_into_two_predict_calls_and_restore_max_length():
    pairs = [_pair(i) for i in range(6)]
    # 짝수 인덱스=짧음(<=512), 홀수 인덱스=김(>512) — 섞어서 그룹핑/합치기를 제대로 검증
    lengths = {p: (100 if i % 2 == 0 else 700) for i, p in enumerate(pairs)}
    fake = _FakeReranker(lengths)

    scores = _rerank(fake, pairs)

    assert len(fake.predict_calls) == 2
    max_lens_used = sorted(call[1] for call in fake.predict_calls)
    assert max_lens_used == [RERANK_MAX_LEN, RERANK_MAX_LEN_LONG]

    long_call = next(c for c in fake.predict_calls if c[1] == RERANK_MAX_LEN_LONG)
    short_call = next(c for c in fake.predict_calls if c[1] == RERANK_MAX_LEN)
    assert all(_idx_of(p) % 2 == 1 for p in long_call[0])
    assert all(_idx_of(p) % 2 == 0 for p in short_call[0])

    # 되돌림: 호출 끝나면 다음 호출을 위해 512로 복원돼 있어야 함
    assert fake.tokenizer.model_max_length == RERANK_MAX_LEN

    # 순서 보존: 그룹을 나눴다 합쳐도 원래 pairs 순서와 정확히 일치해야 함
    assert scores == [float(i) for i in range(6)]


def test_empty_pairs_returns_empty_without_calling_predict():
    fake = _FakeReranker({})
    assert _rerank(fake, []) == []
    assert fake.predict_calls == []


def test_lock_is_acquired_around_predict_calls_when_provided():
    import threading

    pairs = [_pair(i) for i in range(2)]
    lengths = {p: 100 for p in pairs}
    fake = _FakeReranker(lengths)

    events = []

    class _RecordingLock:
        def __enter__(self):
            events.append("acquire")
            return self

        def __exit__(self, *exc):
            events.append("release")
            return False

    _rerank(fake, pairs, lock=_RecordingLock())

    assert events == ["acquire", "release"]
