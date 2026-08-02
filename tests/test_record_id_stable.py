# -*- coding: utf-8 -*-
"""record_id 안정키 단위 테스트 (작업 B의 핵심).

기존 record_id 는 "파일 안 몇 번째 줄"(3001:42)이라 특정 기준서를 재수집해 레코드 수가
바뀌면 **뒤에 있던 무관한 기준서들의 id 까지 전부 밀린다** → Chroma 에 옛 id 좀비 레코드가
대량 잔존 + 같은 내용이 새 id 로 중복 적재. 그래서 id 를 "문서 안 안정키"로 바꾼다.

실행: python3 -m pytest tests/test_record_id_stable.py -q
"""
import json

from rag import common as C


def _para(doc_no, ref_key, text="본문"):
    return {"doc_no": doc_no, "record_type": "paragraph",
            "ref_key": ref_key, "text": text}


# ------------------------------------------------------ 키 구성
def test_id_uses_doc_no_and_ref_key():
    rid = C.record_id("3001.jsonl", _para("3001-2974", "제1116호 문단 7"), 0)
    assert rid == "3001-2974#제1116호 문단 7"


def test_id_falls_back_to_section_key():
    rec = {"doc_no": "3001-2974", "record_type": "term",
           "section_key": "제1116호 용어의 정의", "term": "리스"}
    assert C.record_id("3001.jsonl", rec, 3) == "3001-2974#제1116호 용어의 정의"


def test_ref_key_wins_over_section_key():
    rec = {"doc_no": "3001-2974", "ref_key": "제1116호 용어의 정의:리스",
           "section_key": "제1116호 용어의 정의"}
    assert C.record_id("3001.jsonl", rec, 0).endswith("#제1116호 용어의 정의:리스")


def test_qa_record_without_ref_key_uses_doc_internal_index():
    # 질의회신 레코드에는 ref_key 가 없음 → 문서 내 순번 폴백 (파일 전체 줄번호가 아님)
    rec = {"post_id": "016005-40670", "doc_no": "2025-I-KQA006", "question": "q"}
    assert C.record_id("016005.jsonl", rec, 0) == "016005-40670#0"


def test_falls_back_to_case_id_when_no_doc_no(tmp_path, monkeypatch):
    # 감리지적사례는 doc_no 가 없고 case_id 가 고유 식별자 → 사례 1건 = 문서 1개라 순번은 0
    monkeypatch.setattr(C, "PARSED", tmp_path)
    with (tmp_path / "audit_cases.jsonl").open("w", encoding="utf-8") as f:
        for cid in ("FSS/2311-01", "FSS/2311-02"):
            f.write(json.dumps({"record_type": "audit_case", "case_id": cid},
                               ensure_ascii=False) + "\n")
    ids = [C.record_id(fn, rec, i)
           for _, fn, i, rec in C.iter_records(C.AUDIT_COLLECTIONS)]
    assert ids == ["FSS/2311-01#0", "FSS/2311-02#0"]


def test_falls_back_to_file_stem_when_no_identifier():
    assert C.record_id("3001.jsonl", {"text": "x"}, 5) == "3001#5"


# ------------------------------------------------------ 핵심: 안정성
def test_ids_unaffected_by_other_document_record_count(tmp_path, monkeypatch):
    """다른 문서(3001-A)의 레코드 수가 바뀌어도 3001-B 의 id 는 그대로여야 한다."""
    monkeypatch.setattr(C, "PARSED", tmp_path)
    colls = {"kifrs_standards": ["3001.jsonl"]}

    def write(a_paras):
        with (tmp_path / "3001.jsonl").open("w", encoding="utf-8") as f:
            for i in range(a_paras):
                f.write(json.dumps(_para("3001-A", "제1001호 문단 %d" % i),
                                   ensure_ascii=False) + "\n")
            for k in ("제1116호 문단 7", "제1116호 문단 8"):
                f.write(json.dumps(_para("3001-B", k), ensure_ascii=False) + "\n")

    def ids_of_b():
        return [C.record_id(fn, rec, i) for _, fn, i, rec in C.iter_records(colls)
                if rec["doc_no"] == "3001-B"]

    write(2)
    before = ids_of_b()
    write(5)              # 3001-A 가 재수집돼 문단이 2→5개로 늘어남
    assert ids_of_b() == before
    assert before == ["3001-B#제1116호 문단 7", "3001-B#제1116호 문단 8"]


def test_ids_unique_within_document(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "PARSED", tmp_path)
    colls = {"kifrs_standards": ["3001.jsonl"]}
    with (tmp_path / "3001.jsonl").open("w", encoding="utf-8") as f:
        for k in ("제1116호 문단 7", "제1116호 문단 7⑴", "제1116호 문단 8"):
            f.write(json.dumps(_para("3001-B", k), ensure_ascii=False) + "\n")
    ids = [C.record_id(fn, rec, i) for _, fn, i, rec in C.iter_records(colls)]
    assert len(set(ids)) == 3


def test_iter_records_yields_doc_internal_index(tmp_path, monkeypatch):
    """3번째 위치 값은 '파일 줄번호'가 아니라 '문서 내 순번'이어야 한다."""
    monkeypatch.setattr(C, "PARSED", tmp_path)
    colls = {"qa_kifrs": ["016005.jsonl"]}
    with (tmp_path / "016005.jsonl").open("w", encoding="utf-8") as f:
        for doc, n in (("016005-1", 2), ("016005-2", 3)):
            for _ in range(n):
                f.write(json.dumps({"doc_no": doc}, ensure_ascii=False) + "\n")
    got = [(rec["doc_no"], idx) for _, _, idx, rec in C.iter_records(colls)]
    assert got == [("016005-1", 0), ("016005-1", 1),
                   ("016005-2", 0), ("016005-2", 1), ("016005-2", 2)]


# ------------------------------------------------------ 실데이터 충돌 없음
def test_real_corpus_ids_have_no_collisions():
    """실제 코퍼스 전체에서 새 id 체계가 충돌하지 않는지(= 레코드 유실 없음) 확인."""
    for coll, files in C.COLLECTIONS.items():
        ids = [C.record_id(fn, rec, i)
               for c, fn, i, rec in C.iter_records({coll: files})]
        if not ids:
            continue
        assert len(set(ids)) == len(ids), "%s 에서 id 충돌" % coll
