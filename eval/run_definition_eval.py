# -*- coding: utf-8 -*-
"""정의조회 보너스 골든셋(30건) 평가 — 메인 배치와 별개, 검증된 score()/retrieve_routed() 재사용.

메인 골든셋(eval/goldenset.jsonl)과 절대 안 섞임. 결과도 별도 파일로 저장.
사용: python3 -m eval.run_definition_eval [--per-coll 12] [--bm25]
  기본값(per_coll=50, bm25=False)은 기존 dense-only 회귀 보존. 실제 프로덕션
  (rag/graph.py retrieve())과 동일 조건으로 재려면 --per-coll 12 --bm25.
"""
import argparse
import json

from rag.eval.run_batch import RESULTS, got_keys, score
from rag.search import Index

GOLD = __import__("rag.common", fromlist=["ROOT"]).ROOT / "eval" / "goldenset_definition.jsonl"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-coll", type=int, default=50)
    ap.add_argument("--bm25", action="store_true",
                    help="retrieve_routed에서 BM25+dense RRF 하이브리드 사용")
    args = ap.parse_args()

    golden = [json.loads(l) for l in GOLD.open(encoding="utf-8")]
    print(f"정의조회 평가 {len(golden)}건 · per_coll={args.per_coll} bm25={args.bm25} · Index 로드...",
          flush=True)
    idx = Index()
    ks = (5, 10)
    per_q = []
    agg = {k: {"exact": [], "relaxed": [], "ho": [], "hit": [], "prec": []} for k in ks}
    for g in golden:
        hits = idx.retrieve_routed(g["question"], g["expected_collections"],
                                   k=max(ks) + 3, min_standards=0, per_coll=args.per_coll,
                                   use_bm25=args.bm25)
        exp = g["expected_ref_keys"]
        row = {"id": g["id"], "question": g["question"], "expected": exp}
        for k in ks:
            got = got_keys([h["meta"] for h in hits[:k]])
            ex, rel, ho, hitrate, nhit = score(exp, got)
            agg[k]["exact"].append(ex)
            agg[k]["relaxed"].append(rel)
            agg[k]["ho"].append(ho)
            agg[k]["hit"].append(hitrate)
            agg[k]["prec"].append(nhit / k)
            row[f"exact@{k}"] = ex
        per_q.append(row)
        print(f"  [{g['id']}] {g['question']} exact@5={row['exact@5']:.0f} exact@10={row['exact@10']:.0f}")

    def avg(x):
        return round(sum(x) / len(x), 4) if x else 0.0
    summary = {k: {"exact": avg(v["exact"]), "relaxed": avg(v["relaxed"]), "ho": avg(v["ho"]),
                   "hit": avg(v["hit"]), "precision": avg(v["prec"])}
               for k, v in agg.items()}
    print("\n=== 정의조회 30건 결과 ===")
    for k in ks:
        s = summary[k]
        print(f"  top-{k}: exact={s['exact']:.3f} 인접완화={s['relaxed']:.3f} "
              f"호recall={s['ho']:.3f} hit={s['hit']:.3f} precision={s['precision']:.3f}")

    RESULTS.mkdir(parents=True, exist_ok=True)
    meta = {"n": len(golden), "per_coll": args.per_coll, "bm25": args.bm25}
    (RESULTS / "definition_lookup.json").write_text(
        json.dumps({"meta": meta, "summary": summary, "per_question": per_q},
                   ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(f"저장: {RESULTS / 'definition_lookup.json'}")


if __name__ == "__main__":
    main()
