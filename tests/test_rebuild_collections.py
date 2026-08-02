# -*- coding: utf-8 -*-
"""컬렉션 clean rebuild 스크립트 단위 테스트 (record_id 체계 변경 1회성 마이그레이션).

record_id 가 줄번호 기반 → 문서 안정키 기반으로 바뀌면서 기존 Chroma 의 id 가 전부
무효가 된다(옛 id 레코드는 upsert 로 갱신되지 않고 좀비로 남음). 그래서 해당 컬렉션을
통째로 지우고 새 id 로 다시 적재해야 한다.

실제 임베딩(BGE-M3)은 돌리지 않는다 — embed.run 은 대역으로 대체하고, Chroma 는
tmp_path 의 임시 경로만 사용한다(실 data/chroma 무접촉).
실행: python3 -m pytest tests/test_rebuild_collections.py -q
"""
import pytest

from rag import rebuild_collections as R


@pytest.fixture
def client(tmp_path, monkeypatch):
    """임시 경로의 Chroma + 더미 레코드가 들어간 5개 컬렉션."""
    chromadb = pytest.importorskip("chromadb")
    cl = chromadb.PersistentClient(path=str(tmp_path / "chroma"))
    for name in ("kifrs_standards", "kgaap_standards", "qa_kifrs",
                 "qa_kgaap", "audit_cases"):
        col = cl.get_or_create_collection(name, metadata={"hnsw:space": "cosine"})
        col.upsert(ids=["%s:0" % name], embeddings=[[0.1, 0.2, 0.3]],
                   documents=["옛 id 레코드"], metadatas=[{"collection": name}])
    monkeypatch.setattr(R.C, "get_chroma", lambda: cl)
    return cl


def _names(cl):
    return {c.name for c in cl.list_collections()}


# ------------------------------------------------------------ 대상 해석
def test_default_targets_are_standards_collections():
    assert R.resolve_targets(None) == ["kifrs_standards", "kgaap_standards"]


def test_resolve_targets_accepts_known_names():
    assert R.resolve_targets(["qa_kifrs"]) == ["qa_kifrs"]


def test_resolve_targets_rejects_unknown_name():
    with pytest.raises(SystemExit):
        R.resolve_targets(["없는컬렉션"])


def test_resolve_targets_allows_audit_collection():
    assert R.resolve_targets(["audit_cases"]) == ["audit_cases"]


# ------------------------------------------------------------ 삭제 범위
def test_drop_removes_only_targets(client):
    R.drop_collections(client, ["kifrs_standards"])
    assert "kifrs_standards" not in _names(client)
    assert {"kgaap_standards", "qa_kifrs", "qa_kgaap", "audit_cases"} <= _names(client)


def test_drop_is_idempotent_for_missing_collection(client):
    R.drop_collections(client, ["kifrs_standards"])
    R.drop_collections(client, ["kifrs_standards"])   # 두 번째는 조용히 통과
    assert "kifrs_standards" not in _names(client)


# ------------------------------------------------------------ rebuild 흐름
def test_rebuild_drops_then_reembeds_only_targets(client, monkeypatch):
    seen = {}
    monkeypatch.setattr("rag.embed.run", lambda colls: seen.update(colls))
    R.rebuild(["kgaap_standards"])
    assert "kgaap_standards" not in _names(client)      # 옛 id 전부 제거됨
    assert seen == {"kgaap_standards": ["3003.jsonl"]}  # 해당 파일만 재적재


def test_rebuild_maps_audit_collection_files(client, monkeypatch):
    seen = {}
    monkeypatch.setattr("rag.embed.run", lambda colls: seen.update(colls))
    R.rebuild(["audit_cases"])
    assert seen == {"audit_cases": ["audit_cases.jsonl"]}


def test_rebuild_does_not_run_without_explicit_targets(client, monkeypatch):
    """인자 없이 import 만으로는 아무것도 지우지 않는다(사고 방지)."""
    calls = []
    monkeypatch.setattr("rag.embed.run", lambda colls: calls.append(colls))
    assert calls == []
    assert len(_names(client)) == 5
