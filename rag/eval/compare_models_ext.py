# -*- coding: utf-8 -*-
"""compare_models.py(13케이스, Gemini 판사) 확장 — 새 답변모델 변형 추가 평가.

기존 GPT-5.5/EXAONE(원본 프롬프트) 두 변형은 이미 측정돼 있으므로 다시 돌리지 않고,
아래 두 변형만 추가로 같은 13케이스·같은 판사(Google Gemini)로 평가한다:
  - gpt54mini: 답변 모델을 gpt-5.5 대신 gpt-5.4-mini로 교체
  - exaone_prompt: EXAONE에 "원문 먼저 인용" 프롬프트 한 줄을 추가(quote-first,
    kasb-exaone-qlora-finetune 저장소에서 이미 검증된 문구 그대로 재사용)

두 변형을 한 프로세스에서 동시에(스레드로) 돌리면 L.MODELS/Pipeline._answer_system_prompt
전역 몽키패치가 서로 덮어써 충돌하므로, 변형마다 별도 프로세스로 실행한다(--variant로 구분).
"""
import argparse
import json

from rag import common as C
from rag import llm as L
from rag.eval.compare_models import CASES, _env_key, generate
from rag.eval.judge import Judge
from rag.graph import Pipeline

QUOTE_FIRST_ADDITION = (
    " 핵심 결론을 말하기 전에, 그 결론의 근거가 되는 근거 원문 문장을 [식별자] 인용과 "
    "함께 그대로 한 번 옮겨 적은 뒤 결론을 서술하라(원문을 바꿔 쓰지 말 것)."
)

RESULTS_DIR = C.ROOT / "eval" / "results"

VARIANTS = {
    "gpt54mini": {"label": "GPT-5.4-mini", "local": False,
                  "model_override": "gpt-5.4-mini", "prompt_patch": False},
    "exaone_prompt": {"label": "EXAONE+프롬프트", "local": True,
                       "model_override": None, "prompt_patch": True},
    "gpt54mini_prompt": {"label": "GPT-5.4-mini+프롬프트", "local": False,
                          "model_override": "gpt-5.4-mini", "prompt_patch": True},
}


def run_variant(key, judge_vendor, judge_key, openai_key):
    cfg = VARIANTS[key]

    if cfg["model_override"]:
        L.MODELS["answer"] = cfg["model_override"]
    orig_prompt = Pipeline._answer_system_prompt
    if cfg["prompt_patch"]:
        def _patched(self):
            return orig_prompt(self) + QUOTE_FIRST_ADDITION
        Pipeline._answer_system_prompt = _patched

    from rag.search import Index

    judge = Judge(judge_vendor, judge_key)
    print(f"[{cfg['label']}] 판사: {judge_vendor}/{judge.model}", flush=True)
    print("인덱스 로드...", flush=True)
    index = Index()

    rows = []
    for i, (label, q) in enumerate(CASES):
        tag = f"{key}_{i}"
        ans, gnd, hg, urefs = generate(index, q, cfg["local"], openai_key, tag)
        if not hg:
            rows.append({"case": label, "faithfulness": None, "relevancy": None,
                         "note": "refusal", "answer": ""})
            print(f"  [{i+1}/{len(CASES)}] {label}: refusal(채점 스킵)", flush=True)
            continue
        r = judge.evaluate(q, ans, gnd)
        if not r:
            rows.append({"case": label, "faithfulness": None, "relevancy": None,
                         "note": "판사실패", "answer": ans[:300]})
            print(f"  [{i+1}/{len(CASES)}] {label}: 판사 실패", flush=True)
            continue
        rows.append({"case": label, "faithfulness": r["faithfulness"],
                     "relevancy": r["answer_relevancy"], "note": "", "answer": ans[:300]})
        print(f"  [{i+1}/{len(CASES)}] {label}: F={r['faithfulness']} R={r['answer_relevancy']}",
              flush=True)

    if cfg["model_override"]:
        L.MODELS["answer"] = "gpt-5.5"
    Pipeline._answer_system_prompt = orig_prompt

    out_path = RESULTS_DIR / f"compare_ext_{key}.json"
    out_path.write_text(
        json.dumps({"variant": key, "label": cfg["label"], "rows": rows},
                   ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(f"[저장] {out_path}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", required=True, choices=list(VARIANTS))
    ap.add_argument("--judge-vendor", default="Google")
    ap.add_argument("--judge-key", default=None)
    ap.add_argument("--openai-key", default=None)
    args = ap.parse_args()
    openai_key = args.openai_key or _env_key("OPENAI_API_KEY")
    assert openai_key, "OpenAI 키 필요(GPT 답변 생성용): --openai-key 또는 .env"
    judge_key = args.judge_key or _env_key(f"{args.judge_vendor.upper()}_API_KEY")
    assert judge_key, f"판사 키 필요: --judge-key 또는 .env의 {args.judge_vendor.upper()}_API_KEY"
    run_variant(args.variant, args.judge_vendor, judge_key, openai_key)
    print("ALL_DONE", flush=True)


if __name__ == "__main__":
    main()
