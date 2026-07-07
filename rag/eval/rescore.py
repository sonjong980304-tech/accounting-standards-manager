# -*- coding: utf-8 -*-
"""재채점: run_batch가 남긴 원시 검색결과(retrieval_*.jsonl)로 지표만 다시 계산.

- 검색(리랭킹)은 다시 안 함 → 인덱스 로드·GPU 불필요, 수 초 내 완료.
- 채점 로직(score/인접 판정/호 추출)을 바꾼 뒤 이걸 돌리면 2시간 재실행이 불필요.
사용: python3 -m rag.eval.rescore [--date 20260704]
"""
import argparse
import json
from collections import defaultdict

from rag.eval.run_batch import RESULTS, score, summarize, write_markdown


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="20260704")
    args = ap.parse_args()

    dump = [json.loads(l) for l in
            (RESULTS / f"retrieval_{args.date}.jsonl").open(encoding="utf-8")]
    ks = (5, 10)
    agg = {k: defaultdict(lambda: {"exact": [], "relaxed": [], "ho": [],
                                   "hit": [], "prec": []})
           for k in ks}
    for d in dump:
        exp = d["expected"]
        for k in ks:
            got = set()
            for h in d["hits"][:k]:
                if h.get("ref_key"):
                    got.add(h["ref_key"])
                if h.get("section_key"):
                    got.add(h["section_key"])
            ex, rel, ho, hitrate, nhit = score(exp, got)
            for scope in (d["board"], "ALL"):
                agg[k][scope]["exact"].append(ex)
                agg[k][scope]["relaxed"].append(rel)
                agg[k][scope]["ho"].append(ho)
                agg[k][scope]["hit"].append(hitrate)
                agg[k][scope]["prec"].append(nhit / k)

    summary = summarize(agg, ks)
    # 기존 batch json의 meta 재사용(있으면), 없으면 최소 meta
    bp = RESULTS / f"batch_{args.date}.json"
    meta = json.loads(bp.read_text(encoding="utf-8"))["meta"] if bp.exists() else \
        {"n": len(dump), "self_excluded": 0, "per_coll": "?", "elapsed_s": 0}
    meta["n"] = len(dump)
    bp.write_text(json.dumps({"meta": meta, "summary": summary},
                             ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(summary, ks, meta, RESULTS / "summary.md")

    a5, a10 = summary[5]["ALL"], summary[10]["ALL"]
    print(f"재채점 완료 (n={len(dump)}, 검색 재실행 없음)")
    print(f"  문단 recall exact  top5={a5['exact']:.3f} top10={a10['exact']:.3f}")
    print(f"  문단 recall 인접완화 top5={a5['relaxed']:.3f} top10={a10['relaxed']:.3f}")
    print(f"  호   recall        top5={a5['ho']:.3f} top10={a10['ho']:.3f}")
    print(f"  문단 hit rate(1개↑) top5={a5['hit']:.3f} top10={a10['hit']:.3f}")
    print("  게시판별 호(top10):",
          {b: summary[10][b]["ho"] for b in summary[10] if b != "ALL"})


if __name__ == "__main__":
    main()
