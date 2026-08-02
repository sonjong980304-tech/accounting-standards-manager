# -*- coding: utf-8 -*-
"""용어 레코드만 재임베딩 (embed_text 용어명 포함 수정 반영). 같은 id로 upsert."""
from rag import common as C


def main():
    targets = []   # (coll, id, text, meta)
    for coll, fn, i, rec in C.iter_records():
        if rec.get("record_type") != "term":
            continue
        targets.append((coll, C.record_id(fn, rec, i), C.embed_text(rec),
                        C.to_metadata(rec, coll)))
    print(f"재임베딩 대상 용어 레코드: {len(targets)}건", flush=True)
    client = C.get_chroma()
    emb = C.load_embedder()
    by_coll = {}
    for coll, rid, text, meta in targets:
        by_coll.setdefault(coll, []).append((rid, text, meta))
    for coll_name, items in by_coll.items():
        col = client.get_collection(coll_name)
        vecs = emb.encode([t for _, t, _ in items], normalize_embeddings=True,
                          batch_size=16, show_progress_bar=False)
        col.upsert(ids=[i for i, _, _ in items],
                   embeddings=[v.tolist() for v in vecs],
                   documents=[t for _, t, _ in items],
                   metadatas=[m for _, _, m in items])
        print(f"  [{coll_name}] 용어 {len(items)}건 재임베딩 완료 (컬렉션 {col.count()}건)", flush=True)
    # 샘플 확인
    print("샘플 embed_text:", targets[0][2][:70], flush=True)


if __name__ == "__main__":
    main()
