# -*- coding: utf-8 -*-
"""임베딩 + ChromaDB 적재 (일괄 작업, BGE-M3만 로드).

- 청크 = 레코드 1개 (추가 분할 없음). 8192 초과는 모델이 자동 절단.
- 질의회신은 question+answer 합쳐 하나의 임베딩 (common.embed_text).
- 배치 처리 + 진행률. 재개: 이미 적재된 id는 스킵(재인코딩 안 함).

사용법: python3 -m rag.embed            (전체)
        python3 -m rag.embed --batch 32
"""
import argparse
import sys
import time
from collections import defaultdict

from rag import common as C

# 배치 어텐션 메모리 ∝ batch × maxlen². 토큰 예산으로 배치 크기를 적응시켜
# 긴 문서(8192)는 배치 1, 짧은 문서는 배치 32까지 → MPS OOM 방지.
AREA_BUDGET = 8192   # batch × approx_tokens 상한
MAX_BATCH = 32


def approx_tokens(text):
    # 한국어 서브워드 ~1.5~2자/토큰. 안전하게 과대추정(작은 배치) + 8192 상한.
    return min(max(len(text) // 2, 1), C.MAX_SEQ)


def adaptive_batches(items):
    """items(id,text,meta)를 길이 내림차순 정렬 후 토큰예산 기반 가변배치로 yield."""
    ordered = sorted(items, key=lambda x: -approx_tokens(x[1]))
    i = 0
    while i < len(ordered):
        maxtok = approx_tokens(ordered[i][1])
        bs = max(1, min(MAX_BATCH, AREA_BUDGET // maxtok))
        yield ordered[i:i + bs]
        i += bs


def existing_ids(coll):
    try:
        return set(coll.get(include=[])["ids"])
    except Exception:  # noqa: BLE001
        return set()


def run(collections=None):
    """collections(기본 COLLECTIONS) 매핑을 임베딩·적재. AUDIT_COLLECTIONS 등도 재사용 가능."""
    # 컬렉션별 레코드 수집
    buckets = defaultdict(list)   # coll -> [(id, text, metadata)]
    for coll, fn, i, rec in C.iter_records(collections):
        text = C.embed_text(rec)
        if not text.strip():
            continue
        buckets[coll].append((C.record_id(fn, rec, i), text, C.to_metadata(rec, coll)))

    print("적재 대상:", {k: len(v) for k, v in buckets.items()}, flush=True)
    client = C.get_chroma()
    print("BGE-M3 로드 중...", flush=True)
    emb = C.load_embedder()
    print("device =", emb.device, flush=True)

    grand_done = grand_total = 0
    t0 = time.time()
    for coll_name, items in buckets.items():
        col = client.get_or_create_collection(
            coll_name, metadata={"hnsw:space": "cosine"})
        done = existing_ids(col)
        todo = [x for x in items if x[0] not in done]
        grand_total += len(items)
        grand_done += len(items) - len(todo)
        print(f"\n[{coll_name}] 전체 {len(items)} / 기적재 {len(items)-len(todo)} / 신규 {len(todo)}",
              flush=True)

        try:
            import torch
            mps_clear = torch.mps.empty_cache if torch.backends.mps.is_available() else None
        except Exception:  # noqa: BLE001
            mps_clear = None

        for chunk in adaptive_batches(todo):
            ids = [c[0] for c in chunk]
            docs = [c[1] for c in chunk]
            metas = [c[2] for c in chunk]
            vecs = emb.encode(docs, normalize_embeddings=True,
                              batch_size=len(chunk), show_progress_bar=False)
            col.upsert(ids=ids, embeddings=[v.tolist() for v in vecs],
                       documents=docs, metadatas=metas)
            if mps_clear:
                mps_clear()      # 배치 간 MPS 메모리 반환 (긴 문서 후 누적 방지)
            grand_done += len(chunk)
            pct = 100 * grand_done / grand_total
            rate = grand_done / max(time.time() - t0, 1e-6)
            sys.stdout.write(
                f"\r  진행 {grand_done}/{grand_total} ({pct:.1f}%) "
                f"{rate:.0f} rec/s   ")
            sys.stdout.flush()
        print(f"\n[{coll_name}] 완료: {col.count()}건", flush=True)

    print(f"\n전체 적재 완료: {grand_done}건, {time.time()-t0:.0f}s", flush=True)
    print("컬렉션 현황:", {c.name: c.count() for c in client.list_collections()},
          flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=32)
    ap.parse_args()
    run()


if __name__ == "__main__":
    main()
