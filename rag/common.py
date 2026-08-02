# -*- coding: utf-8 -*-
"""RAG 공용 모듈: 컬렉션 정의, 임베딩 텍스트/메타데이터 매핑, 모델 로딩.

임베딩(일괄)과 검색(상시)이 각각 필요한 모델만 로드하도록 분리:
  - 임베딩: load_embedder()          (BGE-M3만)
  - 검색:   load_embedder() + load_reranker()
"""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PARSED = ROOT / "data" / "parsed"
CHROMA_DIR = Path(os.environ.get("KASB_CHROMA_DIR", str(ROOT / "data" / "chroma")))

EMB_MODEL = os.environ.get("KASB_EMB_MODEL", "BAAI/bge-m3")
RERANK_MODEL = "BAAI/bge-reranker-v2-m3"
MAX_SEQ = 8192          # 8192 초과 레코드는 자동 절단 (13건, 병합 아티팩트)
EMB_DIM = 1024

# 컬렉션 = 라우팅 단위. 파일 → 컬렉션 매핑.
COLLECTIONS = {
    "kifrs_standards": ["3001.jsonl"],
    "kgaap_standards": ["3003.jsonl"],
    "qa_kifrs": ["016001.jsonl", "016002.jsonl", "016005.jsonl"],
    "qa_kgaap": ["016003.jsonl", "016006.jsonl"],
}

# 감리지적사례 사이드카 컬렉션 — COLLECTIONS와 **완전히 분리**된 별도 상수.
#   · graph.ALL_COLLS(라우터 후보)에 절대 섞이면 안 됨(참고용 사이드카, 답변 근거 아님).
#   · 컬렉션명은 *_standards 접미사를 피함(search.retrieve_routed의 min_standards 오작동 방지).
#   · JSONL은 rag.sync_audit_cases 가 audit-sentinel cases.jsonl 을 변환해 생성(레포 결합도↓).
AUDIT_COLLECTIONS = {
    "audit_cases": ["audit_cases.jsonl"],
}
BASE_URL = "https://www.kasb.or.kr"


def embed_text(rec):
    """레코드 → 임베딩 대상 텍스트.

    - 기준서(문단/용어): text
    - 질의회신: 'question' + 'answer' 를 합쳐 하나로 (질문 유사도·답변 내용 둘 다 매칭)
      분리 실패(body_fallback) 문서는 body 사용.
    """
    rt = rec.get("record_type")
    if rt == "audit_case":
        # 감리지적사례: 사실관계+지적사항+판단근거를 결합해 1임베딩(레코드=청크 1개).
        #   audit_gap/implication은 임베딩에 넣지 않음(표시용 메타로만 보존). 라벨은 검색·표시 보조.
        return "사실관계: {}\n\n지적사항: {}\n\n판단근거: {}".format(
            rec.get("facts", ""), rec.get("violation", ""), rec.get("basis", ""))
    if rt == "term":
        # 용어명을 앞에 붙여 임베딩 (정의문에는 용어명이 안 들어가 매칭 실패 방지)
        term = rec.get("term", "")
        return "{}: {}".format(term, rec.get("text", "")) if term else rec.get("text", "")
    if rt == "paragraph":
        return rec.get("text", "")
    q, a = rec.get("question"), rec.get("answer")
    if q or a:
        return "질의: {}\n회신: {}".format(q or "", a or "")
    return rec.get("body", "")


def to_metadata(rec, coll):
    """검색 결과에서 원문카드·PDF페이지·KASB링크를 만들 재료.

    Chroma 메타데이터는 str/int/float/bool만 허용 → None/리스트는 제외·변환.
    """
    md = {
        "collection": coll,
        "doc_no": rec.get("doc_no", ""),
        "source": rec.get("source", ""),
        "record_type": rec.get("record_type", ""),
    }
    for k in ("ref_key", "section_key", "src_file", "standard_no",
              "standard_name", "para_no", "term", "title", "reply_date",
              "qa_source"):
        v = rec.get(k)
        if v not in (None, ""):
            md[k] = v
    if rec.get("page_no") is not None:
        md["page_no"] = int(rec["page_no"])
    # url: 질의회신은 저장된 url, 기준서는 게시판 상세로 구성
    if rec.get("url"):
        md["url"] = rec["url"]
    # 첨부는 리스트라 문자열로 join
    if rec.get("attachments"):
        md["attachments"] = " | ".join(rec["attachments"])
    if rec.get("record_type") == "audit_case":
        # 감리지적사례 표시용 필드(임베딩 대상 여부와 무관하게 보존). title은 위 화이트리스트가
        # 이미 처리. facts/violation/basis는 embed_text 본문(검색결과 text)으로 노출되므로 메타
        # 중복 저장하지 않음. Chroma는 str/int/float/bool만 허용 → None/"" 제외, bool 명시 변환.
        for k in ("case_id", "standard", "source_url",
                  "audit_gap", "implication", "fiscal_year"):
            v = rec.get(k)
            if v not in (None, ""):
                md[k] = v
        if rec.get("standard_superseded") is not None:
            md["standard_superseded"] = bool(rec["standard_superseded"])
    return md


def iter_records(collections=None):
    """(collection, file, doc_idx, record) 스트림. doc_idx = **그 문서 안에서의** 순번.

    파일 전체 줄번호가 아니라 문서(doc_no) 내 순번을 주는 이유는 record_id 안정성 때문이다
    (기준서 1건 재수집으로 레코드 수가 바뀌어도 다른 문서의 id 가 밀리지 않게).

    collections 미지정 시 기본 COLLECTIONS. AUDIT_COLLECTIONS 등 다른 매핑을 넘겨 재사용 가능.
    """
    for coll, files in (collections or COLLECTIONS).items():
        for fn in files:
            path = PARSED / fn
            if not path.exists():
                continue
            seen = {}
            for line in path.open(encoding="utf-8"):
                rec = json.loads(line)
                key = _doc_key(fn, rec)
                idx = seen.get(key, 0)
                seen[key] = idx + 1
                yield coll, fn, idx, rec


def _doc_key(fn, rec):
    """레코드가 속한 '문서'의 안정 식별자.

    post_id(게시판-seq, 사이트가 보장하는 고유키) > doc_no > case_id > 파일명 순.
    질의회신의 doc_no 는 첨부 파일명에서 뽑은 공식번호라 파일명이 바뀌면 흔들릴 수 있어
    post_id 를 우선한다(기준서에는 post_id 가 없고 doc_no 가 곧 board-seq).
    """
    return (rec.get("post_id") or rec.get("doc_no") or rec.get("case_id")
            or fn.replace(".jsonl", ""))


def record_id(fn, rec, doc_idx=0):
    """Chroma 문서 id. '{문서키}#{문서 내 안정키}'.

    안정키는 ref_key(문단/용어) → section_key → 문서 내 순번(질의회신·감리사례처럼
    ref_key 가 없는 레코드) 순으로 고른다. 줄번호 기반이던 옛 체계와 달리, 어떤 문서를
    재수집해 레코드 수가 달라져도 **다른 문서의 id 는 변하지 않는다**.
    """
    stable = rec.get("ref_key") or rec.get("section_key")
    return "{}#{}".format(_doc_key(fn, rec),
                          stable if stable else doc_idx)


# ------------------------------------------------------------------ 모델

def pick_device():
    import torch
    return "mps" if torch.backends.mps.is_available() else "cpu"


def load_embedder(device=None):
    from sentence_transformers import SentenceTransformer
    dev = device or pick_device()
    m = SentenceTransformer(EMB_MODEL, device=dev)
    m.max_seq_length = MAX_SEQ
    if dev != "cpu":
        m.half()   # fp16: 긴 문서(8192) 어텐션 메모리 절반 + 속도 향상 (MPS OOM 방지)
    return m


def load_reranker(device=None):
    from sentence_transformers import CrossEncoder
    dev = device or pick_device()
    # max_length 512: 문서 대부분이 짧음(중앙 161자·90%tile 444자≈150토큰)이라 512로
    #   99%+ 온전 커버, 긴 문서만 절단. cross-encoder 비용은 길이에 민감 → 1024대비 2~4배 빠름.
    ce = CrossEncoder(RERANK_MODEL, device=dev, max_length=512)
    if dev != "cpu":
        try:
            ce.model.half()   # fp16: 임베더와 동일 정책. fp32 대비 ~2배 속도·메모리 절반
        except Exception:
            pass              # fp16 미지원/불안정 시 fp32 유지(안전)
    return ce


def ensure_corpus():
    """벡터DB가 없는 배포 환경(HF Spaces 등)에서 HF private 데이터셋에서 data/chroma를 내려받는다.

    로컬(이미 data/chroma 존재)에서는 아무 일도 하지 않는다. 데이터 저작권(KASB) 때문에
    벡터DB는 GitHub/Space git이 아닌 HF private 데이터셋에 두고, 앱 시작 시 토큰으로 로드한다.
      - KASB_DATA_REPO: 예) "sonsdf/kasb-rag-corpus" (미설정이면 다운로드 스킵 = 로컬 모드)
      - HF_TOKEN:       private 데이터셋 접근 토큰 (Space 시크릿)
    """
    if CHROMA_DIR.exists() and any(CHROMA_DIR.iterdir()):
        return
    repo = os.environ.get("KASB_DATA_REPO")
    if not repo:
        return   # 로컬: python3 -m rag.embed 로 직접 생성
    from huggingface_hub import snapshot_download
    snapshot_download(repo_id=repo, repo_type="dataset",
                      local_dir=str(ROOT / "data"),
                      token=os.environ.get("HF_TOKEN"),
                      allow_patterns=["chroma/**"])


def get_chroma():
    import chromadb
    ensure_corpus()
    CHROMA_DIR.mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(path=str(CHROMA_DIR))
