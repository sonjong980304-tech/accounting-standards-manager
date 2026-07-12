# -*- coding: utf-8 -*-
"""하이브리드 검색 CLI: BM25 + dense → RRF 병합 → bge-reranker 재정렬.

상시 실행용 (BGE-M3 + 리랭커 둘 다 로드). 임베딩(embed.py)과 분리.

사용법:
    python3 -m rag.search "1년 임차 후 1년 연장하면 단기리스 면제 되나?"
    python3 -m rag.search --collections qa_kifrs,kifrs_standards "..."
    python3 -m rag.search --interactive
"""
import argparse
import re
import threading

from rag import common as C

RRF_K = 60
POOL_PER = 50      # 각 검색기(BM25/dense) 상위 N
RERANK_N = 30      # 리랭킹 후보 수
TOP = 5


def tokenize(text, tokenizer):
    """BM25용 토큰화: BGE-M3 서브워드 (한국어 recall 확보)."""
    return tokenizer.tokenize(text.lower())


def _rrf_merge(rank_lists, k=RRF_K):
    """순위 리스트 여러 개(각각 id 시퀀스, 앞이 상위)를 RRF로 병합.

    반환: [(id, score), ...] 점수 내림차순. 이종 검색기(BM25/dense) 점수
    스케일이 달라도 순위만 쓰므로 비교 가능.
    """
    scores = {}
    for lst in rank_lists:
        for rank, _id in enumerate(lst):
            scores[_id] = scores.get(_id, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda x: -x[1])


def _bm25_candidates_for_collections(ids, doc_coll, scores, colls, per_coll):
    """전체 코퍼스 BM25 점수에서 라우팅된 컬렉션(colls) 소속만 걸러 상위 id 반환.

    self.bm25는 전 컬렉션 통합 인덱스이므로, retrieve_routed처럼 컬렉션이
    한정된 호출에서는 대상 밖 문서를 먼저 제외한 뒤 순위를 매겨야 한다.
    """
    coll_set = set(colls)
    pool = [(i, s) for i, c, s in zip(ids, doc_coll, scores) if c in coll_set]
    pool.sort(key=lambda x: -x[1])
    return [i for i, _ in pool[:per_coll * len(colls)]]


class Index:
    """Chroma(dense) + rank_bm25(sparse)를 함께 여는 검색 인덱스."""

    def __init__(self, collections=None):
        self.client = C.get_chroma()
        names = [c.name for c in self.client.list_collections()]
        self.colls = [n for n in (collections or names) if n in names]
        self.emb = C.load_embedder()
        self.reranker = C.load_reranker()
        # graph.py의 retrieve/audit_lookup 노드가 이 Index를 병렬로 호출한다(같은 route
        # 뒤에서 fan-out). HuggingFace fast tokenizer는 스레드 세이프하지 않아(내부
        # Rust RefCell) 동시 encode/predict 호출 시 "RuntimeError: Already borrowed"로
        # 죽는다 → 모델 호출 구간만 락으로 직렬화.
        self._model_lock = threading.Lock()
        # 전 컬렉션 문서를 메모리에 적재 (BM25 union + 결과 렌더용)
        self.docs, self.ids, self.metas, self.doc_coll = [], [], [], []
        for cn in self.colls:
            col = self.client.get_collection(cn)
            got = col.get(include=["documents", "metadatas"])
            for i, d, m in zip(got["ids"], got["documents"], got["metadatas"]):
                self.ids.append(i); self.docs.append(d); self.metas.append(m)
                self.doc_coll.append(cn)
        self.pos = {i: k for k, i in enumerate(self.ids)}
        from rank_bm25 import BM25Okapi
        self.bm25 = BM25Okapi([tokenize(d, self.emb.tokenizer) for d in self.docs])

    def _dense(self, query):
        qv = self.emb.encode([query], normalize_embeddings=True)[0].tolist()
        ranked = []
        for cn in self.colls:
            col = self.client.get_collection(cn)
            r = col.query(query_embeddings=[qv], n_results=POOL_PER,
                          include=["distances"])
            for _id, dist in zip(r["ids"][0], r["distances"][0]):
                ranked.append((_id, 1.0 - dist))   # cosine distance → 유사도
        ranked.sort(key=lambda x: -x[1])
        return [i for i, _ in ranked[:POOL_PER * len(self.colls)]]

    def _bm25(self, query):
        scores = self.bm25.get_scores(tokenize(query, self.emb.tokenizer))
        order = sorted(range(len(scores)), key=lambda k: -scores[k])
        return [self.ids[k] for k in order[:POOL_PER * len(self.colls)]]

    def search(self, query):
        dense_ids = self._dense(query)
        bm25_ids = self._bm25(query)
        fused = _rrf_merge([dense_ids, bm25_ids])
        rrf = dict(fused)
        pre = [i for i, _ in fused[:RERANK_N]]
        # 리랭킹
        pairs = [(query, self.docs[self.pos[i]]) for i in pre]
        rr = self.reranker.predict(pairs)
        reranked = sorted(zip(pre, rr), key=lambda x: -x[1])
        return {
            "pre": [(i, rrf[i]) for i in pre[:TOP]],
            "post": reranked[:TOP],
        }

    def retrieve_routed(self, query, collections, k=6, min_standards=1, per_coll=20,
                        use_bm25=False):
        """라우팅된 컬렉션만 대상으로 검색+리랭킹, 컬렉션 쿼터로 기준서 근거 보장.

        use_bm25=False(기본): 기존과 동일한 dense-only 후보 선정(회귀 보존).
        use_bm25=True: 같은 컬렉션 범위 안에서 BM25 후보도 함께 뽑아 RRF로
          dense와 병합 후 리랭킹(문단 번호·전문용어 등 lexical 신호 보강).

        반환: [{ref_key, doc_no, collection, text, score}] (리랭킹 점수 내림차순),
        단 *_standards 컬렉션이 라우팅에 있으면 기준서 근거를 최소 min_standards개 포함.
        """
        colls = [c for c in collections if c in self.colls] or self.colls
        with self._model_lock:
            qv = self.emb.encode([query], normalize_embeddings=True)[0].tolist()
        cand = []   # (id, coll)
        dense_ids = []
        for cn in colls:
            r = self.client.get_collection(cn).query(
                query_embeddings=[qv], n_results=per_coll, include=["distances"])
            for _id in r["ids"][0]:
                cand.append((_id, cn))
                dense_ids.append(_id)
        if not cand:
            return []
        if use_bm25:
            with self._model_lock:
                bm25_scores = self.bm25.get_scores(tokenize(query, self.emb.tokenizer))
            bm25_ids = _bm25_candidates_for_collections(
                self.ids, self.doc_coll, bm25_scores, colls, per_coll)
            fused = _rrf_merge([dense_ids, bm25_ids])
            dense_coll = dict(cand)   # id → coll (dense 후보분)
            cand = [(i, dense_coll.get(i) or self.doc_coll[self.pos[i]])
                    for i, _ in fused[:per_coll * len(colls)]]
        pairs = [(query, self.docs[self.pos[i]]) for i, _ in cand]
        with self._model_lock:
            scores = self.reranker.predict(pairs)
        ranked = sorted(zip(cand, scores), key=lambda x: -x[1])   # ((id,coll),score)

        def item(entry):
            (i, cn), s = entry
            m = self.metas[self.pos[i]]
            return {"ref_key": m.get("ref_key", ""), "doc_no": m.get("doc_no", ""),
                    "collection": cn, "text": self.docs[self.pos[i]],
                    "score": float(s), "meta": m}

        top = [item(e) for e in ranked[:k]]
        # 기준서 근거 최소 보장: 라우팅된 **각 standards 컬렉션에서 최소 1개씩**(컬렉션 독식 방지).
        #   리랭커가 질의회신을 기준서 문단보다 크게 선호(0.9 vs 0.1)해 기준서가 top에서 밀리고,
        #   kifrs·kgaap standards가 함께 라우팅되면 한쪽(kifrs)이 유일 슬롯을 독식해 다른쪽(kgaap
        #   제11장 등) 정답이 누락됨(개발비 케이스 실측). → 컬렉션별로 각각 상위 1개를 보장.
        #   min_standards=0(배치평가: 단일 standards 컬렉션)이면 스킵.
        std_colls = [c for c in colls if c.endswith("standards")]
        if min_standards and std_colls:
            present = {t["collection"] for t in top if t["collection"].endswith("standards")}
            for sc in std_colls:
                if sc in present:
                    continue
                cand = next((e for e in ranked[k:] if e[0][1] == sc), None)
                if not cand:
                    continue
                for j in range(len(top) - 1, -1, -1):    # non-standards 최하위 슬롯을 교체
                    if not top[j]["collection"].endswith("standards"):
                        top[j] = item(cand)
                        present.add(sc)
                        break
        return top

    def render(self, _id, score):
        k = self.pos[_id]
        m = self.metas[k]
        rk = m.get("ref_key") or m.get("doc_no") or _id
        txt = self.docs[k].replace("\n", " ")[:100]
        return "{:>8.4f}  [{}] {}\n            {}".format(
            score, self.doc_coll[k], rk, txt)


def run(index, query):
    res = index.search(query)
    print(f"\n{'='*72}\n질의: {query}\n{'='*72}")
    print("\n── RRF 병합 (리랭킹 前) 상위 5 ──")
    for rank, (i, s) in enumerate(res["pre"], 1):
        print(f"{rank}. {index.render(i, s)}")
    print("\n── bge-reranker 재정렬 (리랭킹 後) 상위 5 ──")
    for rank, (i, s) in enumerate(res["post"], 1):
        print(f"{rank}. {index.render(i, float(s))}")
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="?", default=None)
    ap.add_argument("--collections", default=None,
                    help="쉼표구분 (기본: 전체)")
    ap.add_argument("--interactive", action="store_true")
    args = ap.parse_args()
    colls = args.collections.split(",") if args.collections else None
    print("인덱스 로드 중 (Chroma + BGE-M3 + 리랭커 + BM25)...", flush=True)
    index = Index(colls)
    print(f"준비 완료: 문서 {len(index.docs)}건, 컬렉션 {index.colls}")
    if args.interactive:
        while True:
            try:
                q = input("\n검색> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if q:
                run(index, q)
    elif args.query:
        run(index, args.query)
    else:
        ap.error("query 또는 --interactive 필요")


if __name__ == "__main__":
    main()
