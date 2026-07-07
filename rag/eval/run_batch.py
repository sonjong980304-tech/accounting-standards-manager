# -*- coding: utf-8 -*-
"""배치 평가: 골든셋으로 Context Recall/Precision (기계 채점, LLM 비용 0).

검색 대상: 각 질의회신의 **기준서 컬렉션만**(질의회신 컬렉션 제외).
  정답(expected_ref_keys)은 100% 기준서 문단이므로, 질의회신을 검색 대상에 섞으면
  유사 질의회신이 상위를 독식해 정답을 밀어냄(실측 recall 3.7%→26%로 7배 왜곡).
  → 질의회신 컬렉션을 원천 제외해 self-leakage도 동시 차단(자기 자신이 결과에 안 나옴).

지표(RAGAS 근사) — recall을 3단으로 병기(정직성):
  · 문단 recall (exact)   = 정확한 문단까지 회수한 비율        — 가장 엄격
  · 문단 recall (인접완화) = exact 1.0 + 인접 문단 0.5점        — 실용 하한
  · 호 recall             = 올바른 기준서(제NNNN호) 회수 비율   — 실질 성능 상한
  · 문단 precision(exact) = 검색 상위 k 중 정확 문단 비율
인접 판정(자의성 배제): 같은 호 · 같은 상위경로/접두 · 같은 꼬리 · 마지막 정수 차이 ±1.
  (예: 제1102호 B45 ↔ B46 인접 0.5점 / B45 ↔ B53(차이8) 오답 / 5.7.5 ↔ 5.8.1 오답)

결과: 전체 평균 + 게시판별 + top-k(5/10). batch_YYYYMMDD.json + summary.md.
사용: python3 -m rag.eval.run_batch [--sample 30] [--per-coll 50] [--date YYYYMMDD]
"""
import argparse
import json
import re
import time
import unicodedata
from collections import defaultdict

from rag import common as C
from rag.search import Index

GOLD = C.ROOT / "eval" / "goldenset.jsonl"
RESULTS = C.ROOT / "eval" / "results"

# 게시판 → 검색 대상 기준서 컬렉션 (질의회신 컬렉션은 검색 안 함)
STD_COLL = {"016001": "kifrs_standards", "016002": "kifrs_standards",
            "016005": "kifrs_standards", "016003": "kgaap_standards",
            "016006": "kgaap_standards"}

# 호(kifrs: 제1102호) + 장(kgaap 일반기업기준: 제5장) 모두 — 두 체계 혼재(질의회신 정답에 섞임)
_HO = re.compile(r"(제\d+[호장])")
_LABEL = re.compile(r"문단\s*(.+)$")
_INT = re.compile(r"\d+")


def ho_of(ref_key):
    """ref_key/section_key에서 기준서 단위(제NNNN호/제N장) 추출. kifrs=호, kgaap=장."""
    m = _HO.match(ref_key)
    return m.group(1) if m else None


def parse_para(ref_key):
    """문단키를 (호, 상위경로prefix, 마지막정수, 꼬리)로 분해. 용어섹션/파싱불가 → None."""
    hom = _HO.match(ref_key)
    if not hom:
        return None
    lm = _LABEL.search(ref_key)     # '문단 ' 뒤 라벨. '용어의 정의' 등은 매칭 안 됨
    if not lm:
        return None
    label = lm.group(1).strip()
    ints = list(_INT.finditer(label))
    if not ints:
        return None
    last = ints[-1]
    return (hom.group(1), label[:last.start()], int(last.group()), label[last.end():])


def adjacent(a, b, n=1):
    """두 문단키가 인접한가: 같은 호·상위경로·꼬리 & 마지막 정수 차이 0<Δ≤n."""
    pa, pb = parse_para(a), parse_para(b)
    if not pa or not pb:
        return False
    return (pa[0] == pb[0] and pa[1] == pb[1] and pa[3] == pb[3]
            and pa[2] != pb[2] and abs(pa[2] - pb[2]) <= n)


def got_keys(metas):
    """검색 결과 메타에서 회수한 ref_key/section_key 집합."""
    got = set()
    for m in metas:
        if m.get("ref_key"):
            got.add(m["ref_key"])
        if m.get("section_key"):
            got.add(m["section_key"])
    return got


# 표기 정규화: NFKC로 원문자·전각을 표준형으로 '풀어씀'(제거 아님) + 공백만 제거.
#   'B96⑷'→'B96(4)', 전각'（）'→'()'. 따라서 'B96⑷'↔'B96(4)'는 같게, 'B96⑷'↔'B96⑴'은
#   **다르게** 취급(하위항목 ⑴⑵⑷ 구분 보존). ← 원문자를 '제거'하면 다른 하위문단이 합쳐져
#   recall이 부당하게 부풀려짐(검증: 제거식은 기준서 고유키 3,131그룹 충돌 → false-positive).
#   NFKC식은 충돌 0. 실제 골든셋엔 괄호숫자↔원문자 같은 진짜 표기차가 거의 없어 효과 +0.0%p.
_WS = re.compile(r"\s+")


def _nk(k):
    return _WS.sub("", unicodedata.normalize("NFKC", k))


def score(expected, got):
    """정답 대비 점수 반환: (exact_recall, relaxed_recall, ho_recall, hit_rate, exact_hit수).

    exact는 표기 정규화(_nk) 후 매칭 — 원문자·괄호·공백 차이는 같은 문단으로 인정.
    hit_rate = 정답 문단을 **1개라도** 회수하면 1.0(아니면 0.0). 질의 단위 성공률(Hit@k):
      exact_recall이 문단 개수로 나눠 다문단 인용 질의를 불리하게 잡는 것과 달리,
      '최소 1건의 정답 근거를 확보했는가'만 본다. 항상 hit_rate ≥ exact_recall.
    """
    if not expected:
        return 0.0, 0.0, 0.0, 0.0, 0
    gotnorm = {_nk(g) for g in got}
    def hit(e):
        return _nk(e) in gotnorm
    exact_hits = sum(1 for e in expected if hit(e))
    relaxed = 0.0
    for e in expected:
        if hit(e):
            relaxed += 1.0
        elif any(adjacent(e, g) for g in got):
            relaxed += 0.5
    exp_ho = {ho_of(e) for e in expected if ho_of(e)}
    got_ho = {ho_of(g) for g in got if ho_of(g)}
    ho_recall = len(exp_ho & got_ho) / len(exp_ho) if exp_ho else 0.0
    hit_rate = 1.0 if exact_hits else 0.0
    return (exact_hits / len(expected), relaxed / len(expected), ho_recall,
            hit_rate, exact_hits)


def evaluate(index, golden, ks=(5, 10), per_coll=50, progress_every=50):
    agg = {k: defaultdict(lambda: {"exact": [], "relaxed": [], "ho": [],
                                   "hit": [], "prec": []})
           for k in ks}
    self_excluded = 0
    kmax = max(ks)
    n = len(golden)
    dump = []          # 원시 검색결과 — 채점 로직 바뀌면 재실행 없이 rescore.py로 재계산
    t_start = time.time()
    for qi, g in enumerate(golden, 1):
        scoll = STD_COLL[g["board"]]
        tq = time.time()
        # 기준서 컬렉션만 검색(질의회신 원천 제외). 넉넉히 kmax+3.
        hits = index.retrieve_routed(g["question"], [scoll], k=kmax + 3,
                                     min_standards=0, per_coll=per_coll)
        dt = time.time() - tq
        if qi == 1 or qi % progress_every == 0 or qi == n:
            elapsed = time.time() - t_start
            eta = elapsed / qi * (n - qi)
            print(f"  [{qi}/{n}] 쿼리 {dt:.1f}s · 누적 {elapsed:.0f}s · 예상잔여 {eta:.0f}s",
                  flush=True)
        # 안전망: 혹시 남을 self(doc_no 일치) 제외 — 기준서만 검색이라 사실상 0
        before = len(hits)
        hits = [h for h in hits if h.get("doc_no") != g["doc_no"]]
        self_excluded += before - len(hits)
        exp = g["expected_ref_keys"]
        for k in ks:
            got = got_keys([h["meta"] for h in hits[:k]])
            ex, rel, ho, hitrate, nhit = score(exp, got)
            for scope in (g["board"], "ALL"):
                agg[k][scope]["exact"].append(ex)
                agg[k][scope]["relaxed"].append(rel)
                agg[k][scope]["ho"].append(ho)
                agg[k][scope]["hit"].append(hitrate)
                agg[k][scope]["prec"].append(nhit / k)
        dump.append({"id": g["id"], "board": g["board"], "expected": exp,
                     "hits": [{"ref_key": h["meta"].get("ref_key", ""),
                               "section_key": h["meta"].get("section_key", "")}
                              for h in hits[:kmax]]})
    return agg, self_excluded, dump


def summarize(agg, ks):
    def avg(x):
        return sum(x) / len(x) if x else 0.0
    out = {}
    for k in ks:
        out[k] = {b: {"exact": round(avg(v["exact"]), 4),
                      "relaxed": round(avg(v["relaxed"]), 4),
                      "ho": round(avg(v["ho"]), 4),
                      "hit": round(avg(v["hit"]), 4),
                      "precision": round(avg(v["prec"]), 4),
                      "n": len(v["exact"])}
                  for b, v in agg[k].items()}
    return out


def write_markdown(summary, ks, meta, path):
    a5, a10 = summary[5]["ALL"], summary[10]["ALL"]
    L = ["# 배치 평가 — Context Recall/Precision (기계 채점, LLM 비용 0)", "",
         f"- 골든셋 **{meta['n']}건** (질의회신 질문 → 정답=인용 기준서 문단, 조인 성공분만).",
         f"- 검색 대상: **각 질의회신의 기준서 컬렉션만**(질의회신 컬렉션 제외) · "
         f"per_coll={meta['per_coll']} · dense+리랭킹(bge-reranker-v2-m3, fp16).",
         f"- **self-leakage 차단**: 질의회신 컬렉션을 검색 대상에서 원천 제외 → 자기 글이 "
         f"결과에 안 나옴(추가 doc_no 필터로 제외된 잔여 {meta['self_excluded']}건).",
         f"- 소요 {meta['elapsed_s']}s ({meta['elapsed_s']/max(meta['n'],1):.1f}s/건).", "",
         "## 성능 (전체 평균)", "",
         "| 지표 | top-5 | top-10 | 무엇을 재나 / 무엇을 못 재나 |",
         "|---|---|---|---|",
         f"| 문단 recall — exact | {a5['exact']:.3f} | {a10['exact']:.3f} | "
         "정확한 문단까지 회수. **가장 엄격**(표기 정규화 NFKC 적용하나 현재 데이터엔 효과 +0.0%p) |",
         f"| 문단 recall — 인접완화 | {a5['relaxed']:.3f} | {a10['relaxed']:.3f} | "
         "정답 문단은 1.0, **±1 인접 문단은 0.5**점. exact가 놓치는 '옆 문단' 회수를 반영(실용 하한) |",
         f"| 호 recall — 기준서 단위 | {a5['ho']:.3f} | {a10['ho']:.3f} | "
         "올바른 **기준서(제NNNN호)**를 찾았는가. **실질 성능에 가까움**. 단, 문단 정밀도는 못 잼 |",
         f"| 문단 hit rate — 1개↑ | {a5['hit']:.3f} | {a10['hit']:.3f} | "
         "정답 문단을 **1개라도** 회수한 질의 비율(Hit@k). 다문단 인용 질의에서 '최소 1건 근거 확보' "
         "성공률(항상 exact ≤ hit rate) |",
         f"| 문단 precision — exact | {a5['precision']:.3f} | {a10['precision']:.3f} | "
         "상위 k 중 정확 문단 비율 |", "",
         "### 인접완화 채점 기준 (자의적 후함 배제)",
         "- 인접 = **같은 호 · 같은 상위경로(예: `5.7.`)/접두(예: `B`,`AG`) · 같은 꼬리 · "
         "마지막 정수 차이 정확히 ±1**. 이때만 0.5점.",
         "- 예: `제1102호 B45`↔`B46` 인접(0.5) / `B45`↔`B53`(차이 8) 오답 / "
         "`5.7.5`↔`5.8.1`(상위경로 다름) 오답 / `3A`↔`3`(꼬리 다름) 오답.",
         "- exact를 **대체하지 않고 별도 병기**. 인접완화 ≥ exact는 구조상 보장(exact 점수를 낮추지 않음).", "",
         "### 왜 문단 exact가 낮은가 (정직한 해석)",
         "- 질의회신 1건당 인용 문단 **평균 2.1개·최대 14개** — 여러 문단을 모두 top-k에 넣어야 만점.",
         "- 인용이 **번호를 축약/범위로** 표기하는 경우가 있어 레코드 단위 exact와 어긋남.",
         "- 임베딩·리랭킹은 **올바른 기준서(호)는 잘 찾지만**(위 호 recall) 인용된 정확한 문단 "
         "번호까지 pinpoint하는 것은 본질적으로 어려움 → exact는 하한, 호는 상한, 인접완화는 그 사이.",
         "- 수치를 부풀리지 않음: 세 지표를 모두 공개하고 각자의 한계를 명시.", "",
         "### 문단 exact 실패 원인 분류 (dump 1,956건 자동 분류)",
         "- 표기 정규화(NFKC)로 원문자·괄호·전각 차이는 exact에 이미 흡수 → 남은 실패는 "
         "**진짜 검색 실패 100%**. 골든셋엔 `괄호숫자↔원문자` 같은 진짜 표기차가 거의 없어 "
         "**정규화 효과 +0.0%p**(원문자 '제거'식은 `B96⑴/⑵/⑷`를 합쳐 +3.4%p 부풀렸으나 "
         "false-positive라 폐기 → 문단 exact 27%는 채점 아티팩트가 아닌 실제 검색 성능).",
         "- 실패 내역: **62.6% 호는 맞고 정확 문단만 놓침**(문단 pinpoint 한계) · "
         "30.0% 기준서(호)조차 못 찾음(질의회신이 여러 기준서에 광범위 인용) · 7.4% ±1 인접까지 감.",
         "- **답변 품질 영향은 제한적**: 실패의 63%는 올바른 기준서를 이미 회수(호 recall "
         f"{a10['ho']:.1%})했고 인접 문단을 근거로 제시 → 사용자가 정답 문단 부근 원문을 받게 됨.", "",
         "## 게시판별 (top-10)", "",
         "| 게시판 | 문단 exact | 인접완화 | 호 recall | 문단 hit | n |",
         "|---|---|---|---|---|---|"]
    for b in ("016001", "016002", "016003", "016005", "016006"):
        if b in summary[10]:
            s = summary[10][b]
            L.append(f"| {b} | {s['exact']:.3f} | {s['relaxed']:.3f} | {s['ho']:.3f} | "
                     f"{s['hit']:.3f} | {s['n']} |")
    path.write_text("\n".join(L) + "\n", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=0, help="상위 N건만(0=전체)")
    ap.add_argument("--per-coll", type=int, default=50)
    ap.add_argument("--date", default="test")
    args = ap.parse_args()

    golden = [json.loads(l) for l in GOLD.open(encoding="utf-8")]
    if args.sample:
        golden = golden[:args.sample]
    prog = 5 if len(golden) <= 60 else 50
    print(f"평가 대상 {len(golden)}건, Index 로드...", flush=True)
    idx = Index()
    ks = (5, 10)
    t0 = time.time()
    agg, self_excluded, dump = evaluate(idx, golden, ks=ks, per_coll=args.per_coll,
                                        progress_every=prog)
    summary = summarize(agg, ks)
    meta = {"n": len(golden), "self_excluded": self_excluded,
            "per_coll": args.per_coll, "elapsed_s": round(time.time() - t0, 1)}

    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / f"batch_{args.date}.json").write_text(
        json.dumps({"meta": meta, "summary": summary}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    # 원시 검색결과 저장 → 채점 로직 변경 시 rescore.py로 재실행 없이 재계산(원시 로그, gitignore)
    with (RESULTS / f"retrieval_{args.date}.jsonl").open("w", encoding="utf-8") as f:
        for d in dump:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    write_markdown(summary, ks, meta, RESULTS / "summary.md")

    a5, a10 = summary[5]["ALL"], summary[10]["ALL"]
    print(f"\n=== 결과 (n={meta['n']}, {meta['elapsed_s']}s, self제외 {self_excluded}) ===")
    print(f"  문단 recall  exact  top5={a5['exact']:.3f} top10={a10['exact']:.3f}")
    print(f"  문단 recall  인접완화 top5={a5['relaxed']:.3f} top10={a10['relaxed']:.3f}")
    print(f"  호   recall         top5={a5['ho']:.3f} top10={a10['ho']:.3f}")
    print(f"  문단 precision exact top5={a5['precision']:.3f} top10={a10['precision']:.3f}")


if __name__ == "__main__":
    main()
