# -*- coding: utf-8 -*-
"""GPT vs EXAONE 답변 품질 정량 비교 (동일 판사로 Faithfulness/Relevancy 채점).

- 같은 질문을 답변 모델만 바꿔 생성 → **하나의 판사**로 채점(공정 비교).
- 판사는 답변 모델 둘(GPT=OpenAI, EXAONE=로컬)과 **다른 벤더**여야 자기편향 없음
  → 판사 벤더는 Anthropic 또는 Google 권장(OpenAI 판사는 GPT 답변에 자기편향).
- 목적: EXAONE의 인용 준수·틀린전제 교정 약함(정성 관찰)을 정량 뒷받침.

사용(판사 키 확보 후):
  python3 -m rag.eval.compare_models --judge-vendor Anthropic --judge-key sk-ant-... \
          --openai-key sk-...
  (--openai-key 생략 시 .env의 OPENAI_API_KEY 사용)
"""
import argparse
from pathlib import Path

from rag import common as C
from rag.eval.judge import Judge
from rag.graph import build_graph
from rag.search import Index

# 표본 확대(2026-07-26): 4케이스 → 13케이스. 4케이스는 케이스별 실패 양상만 보여줄 수
# 있어(통계적 결론 불가) 주제·유형을 넓혔다. 유형 구성은 세 축을 고정 —
#   ① 정상(근거 있음) 9건: 리스·금융상품·무형자산·재고·충당부채·차입원가·유형자산·정부보조금
#   ② 틀린 전제 2건: 모델이 사용자의 오류를 근거로 교정하는지
#   ③ 근거 없음 2건: 국내 회계기준 코퍼스 밖 질문 → refusal 유지되는지(환각 방지)
CASES = [
    # ── ① 정상(근거 있음) ──
    ("단기리스", "1년 임차 후 1년 연장하면 단기리스 면제 되나?"),
    ("파생상품", "전환사채 풋옵션이 파생상품 정의를 충족하는지"),
    ("개발비", "내부적으로 창출한 개발비를 무형자산으로 인식하기 위한 요건은 무엇인가?"),
    ("상표권수수료", "회사가 상표권 출원·등록 관련 법률수수료를 지출한 경우 무형자산으로 인식할 수 있는가?"),
    ("재고자산측정", "재고자산은 취득원가와 순실현가능가치 중 어느 금액으로 측정하는가?"),
    ("충당부채", "충당부채를 인식하기 위한 요건은 무엇인가?"),
    ("차입원가", "적격자산 취득에 직접 관련된 차입원가는 자본화해야 하는가?"),
    ("감가상각변경", "유형자산의 감가상각방법을 변경하는 것은 회계정책의 변경인가 회계추정의 변경인가?"),
    ("정부보조금", "자산 관련 정부보조금을 자산의 장부금액에서 차감하는 방식으로 표시할 수 있는가?"),
    # ── ② 틀린 전제(교정 능력) ──
    ("틀린전제", "모든 리스는 리스기간이 12개월을 초과하면 반드시 금융리스로 분류되나?"),
    ("틀린전제2", "모든 무형자산은 내용연수를 20년 이내로 두고 반드시 상각해야 하나?"),
    # ── ③ 근거 없음(환각 방지) ──
    ("근거없음", "미국 세법상 감가상각 내용연수는 몇 년인가?"),
    ("근거없음2", "일본 소비세 신고 기한은 매년 언제까지인가?"),
]


def _env_key(name):
    p = C.ROOT / ".env"
    if not p.exists():
        return None
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k.strip() == name:
            return v.strip().strip('"').strip("'")
    return None


def generate(index, question, local, openai_key, tag):
    """한 모델로 답변 생성 → (answer, grounding, has_grounds)."""
    ckpt = C.ROOT / "rag" / f"checkpoints_cmp_{tag}.db"
    g = build_graph(index, checkpoint_path=ckpt, api_key=openai_key, local=local)
    st = g.invoke({"question": question}, {"configurable": {"thread_id": tag}})
    ans = st.get("answer", {})
    grounding = "\n\n".join(h.get("text", "") for h in st.get("retrieved", []))
    return (ans.get("answer", ""), grounding, ans.get("has_grounds", False),
            ans.get("used_refs", []))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--judge-vendor", required=True, help="Anthropic 또는 Google 권장")
    ap.add_argument("--judge-key", default=None,
                     help="생략 시 .env의 <VENDOR대문자>_API_KEY 사용(예: GOOGLE_API_KEY)")
    ap.add_argument("--openai-key", default=None)
    args = ap.parse_args()
    openai_key = args.openai_key or _env_key("OPENAI_API_KEY")
    assert openai_key, "OpenAI 키 필요(GPT 답변 생성용): --openai-key 또는 .env"
    judge_key = args.judge_key or _env_key(f"{args.judge_vendor.upper()}_API_KEY")
    assert judge_key, f"판사 키 필요: --judge-key 또는 .env의 {args.judge_vendor.upper()}_API_KEY"

    judge = Judge(args.judge_vendor, judge_key)
    print(f"판사: {args.judge_vendor}/{judge.model} (답변 모델 GPT·EXAONE와 다른 벤더)\n")
    print("인덱스 로드...", flush=True)
    index = Index()

    rows = []
    answers = []
    for i, (label, q) in enumerate(CASES):
        for model, local in (("GPT-5.5", False), ("EXAONE", True)):
            tag = f"{model}_{i}".replace(".", "")
            ans, gnd, hg, urefs = generate(index, q, local, openai_key, tag)
            answers.append((label, model, hg, urefs, ans))
            # 답변 모델 벤더: GPT=OpenAI, EXAONE=로컬(비-클라우드). 판사와 같으면 자기편향.
            av = "OpenAI" if not local else "로컬(EXAONE)"
            bias = "⚠ 자기편향(판사=답변 벤더)" if av == args.judge_vendor else "독립 평가"
            if not hg:
                rows.append((label, model, "refusal(채점 스킵)", "-", bias))
                continue
            r = judge.evaluate(q, ans, gnd)
            if not r:
                rows.append((label, model, "판사 실패", "-", bias))
                continue
            rows.append((label, model, f"{r['faithfulness']}", f"{r['answer_relevancy']}", bias))

    print("\n| 케이스 | 답변모델 | Faithfulness | Relevancy | 비고 |")
    print("|---|---|---|---|---|")
    for row in rows:
        print("| " + " | ".join(row) + " |")

    print("\n===== 답변 전문 비교 (육안 질적 차이 — 인용 준수·틀린전제 교정·길이) =====")
    for label, model, hg, urefs, ans in answers:
        print(f"\n[{label} · {model}] has_grounds={hg} · used_refs={urefs}")
        print(f"  {(ans or '(빈 답변)')[:500]}")
    print(f"\n판사 {args.judge_vendor}/{judge.model}. GPT 답변은 판사와 같은 OpenAI라 "
          "**자기편향 가능(참고용)**, EXAONE는 독립 평가.")


if __name__ == "__main__":
    main()
