# -*- coding: utf-8 -*-
"""보너스 골든셋: "정의조회형" 질문 (메인 goldenset.jsonl과 별개, 안 섞임).

목적: 메인 골든셋(실무 시나리오 질문)의 낮은 exact recall(26~34%)이 검색기 결함이
아니라 "질문↔조문 표면 격차" 때문이라는 가설을 검증하기 위한 대조군. 질문을 조문과
가까운 정의조회 형태로 만들면 exact가 얼마나 오르는지 측정한다.

편향 방지: 사람이 케이스별로 "쉬운 용어"를 골라 다듬지 않는다.
- 표본 추출: 3001.jsonl(kifrs)의 **공식 용어정의 레코드 393개 전부** 사용(추출 샘플링 없음 —
  일부만 뽑으면 표본 선택 자체가 편향 요인이 될 수 있어, 존재하는 용어 전체를 대상으로 한다).
- 질문 생성: 3개 고정 템플릿을 순환 배정(용어 선택과 무관하게 기계적).
- kgaap(3003.jsonl)은 부록A 용어정의 형식이 없어(장 체계) 후보 0건 — 전부 kifrs.

정답은 그 용어 레코드 자신의 ref_key 1개뿐이라 메인 골든셋과 달리 다중인용 모호성이 없다.
"""
import json

from rag import common as C

TEMPLATES = [
    "{term}의 정의는 무엇인가?",
    "{term}이란 무엇을 의미하는가?",
    "{term}에 대해 설명해줘.",
]

OUT = C.ROOT / "eval" / "goldenset_definition.jsonl"


def load_terms():
    terms = []
    for line in (C.PARSED / "3001.jsonl").open(encoding="utf-8"):
        r = json.loads(line)
        rk = r.get("ref_key", "")
        if r.get("section_key") and ":" in rk:
            term = rk.split(":", 1)[1]
            if len(term) >= 2:
                terms.append(r)
    terms.sort(key=lambda r: r["ref_key"])
    return terms


def main():
    terms = load_terms()
    print(f"kifrs 용어정의 후보 {len(terms)}건 — 전부 사용(샘플링 없음)")

    rows = []
    for i, r in enumerate(terms):
        term = r["ref_key"].split(":", 1)[1]
        q = TEMPLATES[i % len(TEMPLATES)].format(term=term)
        rows.append({
            "id": f"definition-{i:03d}",
            "question": q,
            "term": term,
            "expected_collections": ["kifrs_standards"],
            "expected_ref_keys": [r["ref_key"]],
            "board": "definition_lookup",
            "doc_no": r.get("doc_no", ""),
        })

    with OUT.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"정의조회 골든셋 {len(rows)}건 → {OUT}")
    for row in rows[:5]:
        print(f"  [{row['id']}] {row['question']} → {row['expected_ref_keys'][0]}")


if __name__ == "__main__":
    main()
