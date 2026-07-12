# -*- coding: utf-8 -*-
"""LangGraph 파이프라인: [rewrite] → route → retrieve → [rewrite 재시도] → answer → verify.

- rewrite는 조건부: 히스토리(후속질문) 있으면 route 전에 무조건 실행, 없으면 생략하고
  retrieve 먼저 시도 → top 리랭커 점수 <0.6(RETRY_SCORE_THRESHOLD)이면 그때만 rewrite 후
  재검색(1회 한정, 무한루프 방지). 분기 판정은 _entry_node/_after_rewrite_node/
  _need_rewrite_retry 순수함수가 전담(tests/test_graph_retry_routing.py).

- 체크포인터: SQLite(thread_id) — 대화기억 지속
- 각 노드가 trace 로그(질문/재작성/라우팅/검색 ref_key/답변/사용 ref_key/지연) 남김
  → 평가(rag/eval)가 이 trace를 소비 (RAGAS 지표)
- answer는 검색 근거만 사용, 근거 없으면 "근거를 찾지 못함" (환각 방지)
- verify는 answer의 ref_key로 원문 레코드를 DB조회해 반환 (LLM 재생성 금지)
"""
import json
import operator
import re
import time
from pathlib import Path
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph

from rag import common as C
from rag import llm as L
from rag.search import Index

TRACE_LOG = C.ROOT / "data" / "traces.jsonl"
ALL_COLLS = list(C.COLLECTIONS.keys())   # 라우터 후보 — AUDIT_COLLECTIONS는 절대 섞지 않음

# 감리지적사례 사이드카 표시 임계값(리랭커 점수 컷오프). 미달 후보는 자동 숨김.
#   실측 튜닝(2026-07-09): audit_smoke_test 자가회수 63/65(96.9%) 점수 하한 0.859 +
#   audit_sample_review 15표본의 실제 관련 매치 분포(약한 매치 0.57~0.66대, 강한 매치 0.8+)를
#   근거로 0.5→0.6 상향. 가장 약한 주변부 매치를 걸러내되 실제 관련 매치는 대부분 보존.
#   ⚠️ 알려진 한계: 점수 기반 필터라 "고점수 오탐"은 못 거름(표본 관찰: 서로 다른 기준서인데
#   0.94로 매치된 사례 1건 — 리랭커가 '숫자조정류' 표면 유사성에 흔들린 것으로 추정, 임계값을
#   더 올려도 해결 안 됨). 데이터가 늘거나 재관찰되면 재튜닝 필요.
AUDIT_CASE_SCORE_THRESHOLD = 0.6

# rewrite 재시도 트리거 임계값(1차 검색 top 리랭커 점수). e5f6601 관찰(정답 0.608 vs 탈락 0.012) 근거.
RETRY_SCORE_THRESHOLD = 0.6


def _need_rewrite_retry(retrieved, already_retried):
    if already_retried:
        return False
    top = retrieved[0]["score"] if retrieved else 0.0
    return top < RETRY_SCORE_THRESHOLD


def _entry_node(history):
    return "rewrite" if history else "route"


def _after_rewrite_node(route_already_set):
    return "retrieve" if route_already_set else "route"


# ---- 그래프 조건부 엣지에 연결하는 얇은 state 어댑터 (분기 판정 자체는 위 순수함수가 전담) ----
def _entry_edge(state):
    return _entry_node(state.get("history", []))


def _after_rewrite_edge(state):
    return _after_rewrite_node(route_already_set=bool(state.get("route")))


def _after_retrieve_edge(state):
    already_retried = bool(state.get("rewritten"))
    need_retry = _need_rewrite_retry(state.get("retrieved", []), already_retried)
    return "rewrite" if need_retry else "answer"


class State(TypedDict, total=False):
    question: str
    rewritten: str
    route: dict
    retrieved: list
    audit_cases: list       # 감리지적사례 사이드카(참고용) — answer는 절대 참조하지 않음
    answer: dict
    verified: list
    history: Annotated[list, operator.add]   # 대화기억 (턴 누적)
    trace: Annotated[list, operator.add]
    # 주: api_key/local은 State가 아닌 Pipeline 인스턴스에 둔다 (체크포인터에 키 저장 방지)


def _audit_filter(hits, threshold=AUDIT_CASE_SCORE_THRESHOLD):
    """리랭커 점수 임계값 미만 후보 제외(사람·LLM 개입 없는 자동 표시/숨김 판정)."""
    return [h for h in hits if h.get("score", 0.0) >= threshold]


def _audit_card(hit):
    """검색 히트 → UI 표시용 감리사례 카드. facts/violation/basis는 hit['text']에 라벨로 실려 있음.
    (answer 노드는 이 카드/audit_cases 값을 절대 참조하지 않는다 — 프롬프트·인용 게이트 격리.)"""
    m = hit.get("meta", {})
    return {"case_id": m.get("case_id", ""), "title": m.get("title", ""),
            "standard": m.get("standard", ""), "source_url": m.get("source_url", ""),
            "standard_superseded": bool(m.get("standard_superseded", False)),
            "fiscal_year": m.get("fiscal_year", ""),
            "text": hit.get("text", ""), "score": float(hit.get("score", 0.0))}


def _clip(s, n=400):
    return (s or "")[:n]


def _extract_json(text):
    """모델 출력에서 JSON 추출 (코드펜스/잡텍스트 방어)."""
    import re
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


_REWRITE_SYS = (
    "너는 한국 회계기준 검색 질의 재작성기다. 사용자 질문을 벡터·리랭커 검색에 "
    "적합한, 그 자체로 완결된 검색 질의로 다시 쓴다. 규칙: "
    "① 구어체 어미·군더더기(알려줘·설명해줘·좀·~해줘·~인가요 등)를 제거한다. "
    "② 붙여 쓴 복합어는 검색 친화적으로 띄어쓴다(예: '자산인식요건'→'자산 인식 요건'). "
    "③ 핵심 회계 용어와 질문의 의미는 절대 바꾸거나 지어내거나 삭제하지 않는다. "
    "④ 이전 대화가 있으면, 현재 질문이 그 대화의 '후속질문'인지 '새 주제'인지 먼저 판단한다. "
    "· 후속질문(지시대명사·생략이 있거나 '그럼/그 경우/~는?'처럼 앞 맥락 없이는 "
    "불완전한 질문)이면, 맥락을 참고해 지시대명사·생략을 구체화한 독립 질문으로 확장한다. "
    "· 새 주제(앞 대화와 다른 회계 주제로 전환된 질문. 예: 수익인식 대화 후 파생상품)면 "
    "이전 대화를 완전히 무시하고 현재 질문만으로 재작성한다(이전 주제어를 절대 붙이지 말 것). "
    "· 애매하면 현재 질문을 우선한다(맥락을 억지로 붙이지 말 것 — 오염이 맥락 누락보다 위험). "
    "재작성한 검색 질의 한 문장만 출력한다.")


def _rewrite_prompt(q, history=None):
    if history:
        convo = "\n".join("{}: {}".format(m["role"], m["content"]) for m in history[-6:])
        usr = "이전 대화:\n{}\n\n질문: {}\n\n검색 질의:".format(convo, q)
    else:
        usr = "질문: {}\n\n검색 질의:".format(q)
    return _REWRITE_SYS, usr


def rewrite_query(llm, q, history=None):
    """질의 재작성 단일 호출(Pipeline 밖에서도 재사용 — 배치 평가 rewrite 트랙용).

    Pipeline.rewrite와 동일한 프롬프트·규칙을 공유해 실시간 앱과 평가가 어긋나지 않게 한다.
    """
    sys, usr = _rewrite_prompt(q, history)
    return llm.complete(sys, usr).strip() or q


class Pipeline:
    """Index(무거운 모델)와 LLM 게터를 보유하고 그래프를 구성."""

    def __init__(self, index: Index, api_key=None, local=False):
        self.index = index
        self.api_key = api_key   # 메모리에만 보관, State/체크포인터에 넣지 않음
        self.local = local

    def _llm(self, node):
        return L.get_llm(node, local=self.local, api_key=self.api_key)

    # ---------------------------------------------------------- 노드
    def rewrite(self, state: State):
        """검색 친화적 질의 정규화 + 후속질문일 때만 맥락 확장(주제전환 오염 방지).

        BGE-M3 임베더·리랭커가 질의 표면형에 민감해, 구어체·군더더기·붙여쓴 복합어가
        있으면 정답 기준서 문단의 리랭커 점수가 무너져 검색에서 누락됨(개발비 케이스:
        '개발비의 자산인식요건 알려줘'는 제11장 11.20 점수 0.012 vs '개발비 자산 인식
        요건' 0.608). 단, 이 문제는 대부분 정제된 문어체 질문에는 거의 없고 구어체
        질문에서만 발생하므로, 매 질의 무조건이 아니라 **조건부**로 호출된다(그래프
        진입/재시도 분기는 build_graph 참조): ①히스토리(후속질문)가 있으면 맥락 해소가
        검색 점수와 무관하게 항상 필요해 무조건 실행 ②히스토리가 없으면 1차 검색이
        부실할 때만(top 리랭커 점수 <0.6) 재시도로 실행.
        히스토리를 무조건 붙이면 주제전환 시 이전 주제가 오염됨(수익인식 대화 후
        파생상품 질문이 '수익인식 기준에서 파생상품~'으로 잘못 확장) → 먼저 후속/새주제를
        판단해, 후속일 때만 맥락 확장하고 새주제·애매하면 현재 질문만으로 재작성한다.
        """
        t0 = time.time()
        q = state["question"]
        history = state.get("history", [])
        rewritten = rewrite_query(self._llm("rewrite"), q, history)
        return {"rewritten": rewritten,
                "trace": [{"node": "rewrite", "before": q, "after": rewritten,
                           "latency_ms": int((time.time() - t0) * 1000)}]}

    def route(self, state: State):
        t0 = time.time()
        q = state.get("rewritten") or state["question"]
        sys = ("너는 회계기준 질의 라우터다. 아래 JSON만 출력한다.\n"
               "{\"collections\": [...], \"qtype\": \"정의조회|사례시나리오|일반\"}\n"
               "컬렉션 후보: kifrs_standards(K-IFRS 기준서 문단·용어), "
               "kgaap_standards(일반기업회계기준 장문단), qa_kifrs(K-IFRS 질의회신), "
               "qa_kgaap(일반기업 질의회신).\n"
               "규칙: 중소기업·일반기업 관련이면 kgaap 계열, 그 외 상장·K-IFRS는 kifrs 계열. "
               "'정의조회'(용어 뜻)면 해당 standards 컬렉션을 반드시 포함. "
               "'사례시나리오'(구체적 거래 회계처리)면 qa 컬렉션을 앞에 둔다.")
        usr = "질문: {}".format(q)
        raw = self._llm("route").complete(sys, usr, json_mode=True)
        parsed = _extract_json(raw) or {}
        colls = [c for c in parsed.get("collections", []) if c in ALL_COLLS]
        qtype = parsed.get("qtype", "일반")
        json_ok = bool(_extract_json(raw))
        colls = self._framework_guard(q, colls)   # 프레임워크 과잉확정 보정
        if not colls:
            colls = ALL_COLLS
        if qtype == "정의조회" and not any(c.endswith("standards") for c in colls):
            colls.append("kifrs_standards")
        if qtype == "사례시나리오":
            colls = sorted(colls, key=lambda c: (not c.startswith("qa_")))
        route = {"collections": colls, "qtype": qtype}
        return {"route": route,
                "trace": [{"node": "route", "route": route, "json_ok": json_ok,
                           "raw": _clip(raw, 200),
                           "latency_ms": int((time.time() - t0) * 1000)}]}

    @staticmethod
    def _framework_guard(question, colls):
        """프레임워크 신호가 없는데 한쪽만 고른 경우 양쪽 다 포함(과잉확정 방지).

        예: '단기리스 면제'는 명시 신호가 없어 K-IFRS(제1116호)로 가야 하는데
        라우터가 KGAAP만 고르는 오류 → 신호 없으면 kifrs·kgaap QA 모두 검색.
        """
        import re
        kgaap = bool(re.search(r"중소기업|일반기업|비상장|중견기업", question))
        kifrs = bool(re.search(r"상장|연결재무제표|K-?IFRS|국제회계|지배기업", question))
        if kgaap and not kifrs:
            return [c for c in colls if "kgaap" in c] or ["qa_kgaap", "kgaap_standards"]
        if kifrs and not kgaap:
            return [c for c in colls if "kifrs" in c] or ["qa_kifrs", "kifrs_standards"]
        # 모호(명시 신호 없음): 양쪽 QA + 양쪽 standards 모두 보장.
        #   예: '개발비 자산인식요건'은 무형자산으로 kgaap(제11장 11.20)·kifrs(제1038호) 양쪽에
        #   존재. 라우터 LLM이 비결정적으로 한쪽 standards만 고르면 정답 문단을 놓쳐 GPT가
        #   근거부실 refusal(EXAONE는 자기지식으로 환각 답변) → 모호할 땐 4컬렉션 모두 검색해
        #   리랭커가 최적 근거를 고르게 한다. (신호가 명확하면 위에서 이미 한쪽으로 좁혀짐)
        for c in ("qa_kifrs", "qa_kgaap", "kifrs_standards", "kgaap_standards"):
            if c not in colls:
                colls.append(c)
        return colls

    def retrieve(self, state: State):
        t0 = time.time()
        colls = state["route"]["collections"]
        q = state.get("rewritten") or state["question"]
        hits = self.index.retrieve_routed(q, colls, k=8,
                                          min_standards=1, per_coll=12)
        # 답변 전에 근거 카드를 렌더할 수 있게 원문·링크 메타를 함께 실음 (LLM 재생성 아님)
        slim = [{"ref_key": h["ref_key"], "doc_no": h["doc_no"],
                 "collection": h["collection"], "score": h["score"],
                 "text": h["text"],
                 "url": h["meta"].get("url", ""), "src_file": h["meta"].get("src_file", ""),
                 "page_no": h["meta"].get("page_no"), "source": h["meta"].get("source", "")}
                for h in hits]
        return {"retrieved": slim,
                "trace": [{"node": "retrieve", "collections": colls,
                           "ref_keys": [h["ref_key"] or h["doc_no"] for h in slim],
                           "latency_ms": int((time.time() - t0) * 1000)}]}

    def audit_lookup(self, state: State):
        """감리지적사례 사이드카 조회 — 답변 근거와 **완전히 분리된** 참고용(접근법 B: 병렬 노드).

        - 매 질문마다 무조건 실행(트리거 조건 없음). rewritten 질의로 audit_cases 컬렉션만
          dense+리랭킹 top-3 → 리랭커 점수 임계값 미달 후보 제외(자동 표시/숨김).
        - 조회 실패(컬렉션 부재·예외)는 verify의 관용 패턴처럼 조용히 빈 결과로 처리(답변 흐름
          에 영향 없음). audit_cases 미로드 시 retrieve_routed의 빈-필터 폴백(전 컬렉션 검색)이
          비감리 결과를 반환하지 않도록 컬렉션 존재를 먼저 확인한다.
        - ⚠️ 반환 audit_cases는 answer 노드가 절대 참조하지 않는다(프롬프트·has_grounds 게이트 격리).
        """
        t0 = time.time()
        cases = []
        if "audit_cases" in self.index.colls:
            try:
                # per_coll=20(코드베이스 관행값): 65건 전체(구버전 per_coll=65)를 매 질문마다
                # 리랭킹하면 12초대 지연 유발(실측). dense 사전필터 20위 안에 진짜 관련
                # 사례가 안정적으로 들어옴(스모크테스트 자기회수 점수 0.86~1.0로 확인) →
                # 후보를 줄여도 recall 손실 거의 없이 리랭킹 비용만 3배 이상 절감.
                q = state.get("rewritten") or state["question"]
                hits = self.index.retrieve_routed(q, ["audit_cases"],
                                                  k=3, min_standards=0, per_coll=20)
                cases = [_audit_card(h) for h in _audit_filter(hits)]
            except Exception:   # noqa: BLE001 — 사이드카 실패는 답변에 영향 없이 조용히 무시
                cases = []
        return {"audit_cases": cases,
                "trace": [{"node": "audit_lookup",
                           "matched": [{"case_id": c["case_id"], "score": c["score"]}
                                       for c in cases],
                           "latency_ms": int((time.time() - t0) * 1000)}]}

    REFUSAL = "근거를 찾지 못했습니다."

    def answer(self, state: State):
        """근거만 사용, 평문 답변 + 인용은 [ref_key] 인라인. LangChain 모델이라
        graph.stream(stream_mode='messages')로 토큰이 UI에 스트리밍된다."""
        t0 = time.time()
        q, hits = state["question"], state.get("retrieved", [])
        if not hits:
            return self._finish_answer(state, q, self.REFUSAL, [], False, t0)
        ctx = "\n\n".join(
            "[{}] ({}) {}".format(h["ref_key"] or h["doc_no"], h["collection"],
                                  _clip(h["text"], 700)) for h in hits)
        sys = ("너는 한국 회계기준 답변가다. 아래 '근거'만 사용해 한국어로 답한다. "
               "근거에 없는 내용은 지어내지 말 것. 인용은 반드시 근거의 대괄호 식별자를 "
               "그대로 [식별자] 형태로 문장에 넣는다(예: [제1116호 문단 7]). "
               "근거만으로 답할 수 없으면 다른 말 없이 정확히 '{}'만 출력한다."
               .format(self.REFUSAL))
        usr = "질문: {}\n\n근거:\n{}".format(q, ctx)
        model = L.answer_chat_model(local=self.local, api_key=self.api_key)
        mname = L.LOCAL_MODEL if self.local else L.MODELS["answer"]
        resp = model.invoke(
            [("system", sys), ("human", usr)],
            config={"run_name": "answer", "tags": ["node:answer", "model:" + mname],
                    "metadata": {"node": "answer", "model": mname}})
        text = (resp.content or "").strip()
        # 인용 추출: 검색된 유효 ref만 used_refs로 (환각 인용 방지)
        valid = {h["ref_key"] or h["doc_no"] for h in hits}
        cited = list(dict.fromkeys(c for c in re.findall(r"\[([^\[\]]+)\]", text)
                                   if c in valid))
        refused = self.REFUSAL[:8] in text or len(text) < 15
        if self.local:
            # 로컬(EXAONE): 긴 컨텍스트에서 [ref_key] 인용 준수가 약함 → 인용 형식은
            # 완화하고 '실질 답변(모델이 refusal 안 함)'이면 채택. 근거가 정말 없어
            # 모델이 refusal하면(미국세법 등) 그대로 유지(환각방지 목적 보존).
            # used_refs: EXAONE 인용 중 검색된 것 우선, 없으면 top 근거로 best-effort.
            has = not refused
            used = cited or ([hits[0]["ref_key"] or hits[0]["doc_no"]] if has else [])
        else:
            # GPT: 엄격 유지 (유효 인용 필수) — 손대지 않음 (5/5 회귀 방지)
            has = bool(cited) and not refused
            used = cited if has else []
        ans_text = text if has else self.REFUSAL
        return self._finish_answer(state, q, ans_text, used, has, t0)

    def _finish_answer(self, state, q, ans_text, used_refs, has_grounds, t0):
        ans = {"answer": ans_text, "used_refs": used_refs, "has_grounds": has_grounds}
        return {"answer": ans,
                "history": [{"role": "user", "content": q},
                            {"role": "assistant", "content": ans_text}],
                "trace": [{"node": "answer", "answer": _clip(ans_text, 300),
                           "used_refs": used_refs, "has_grounds": has_grounds,
                           "latency_ms": int((time.time() - t0) * 1000)}]}

    def verify(self, state: State):
        """answer의 used_refs를 DB에서 원문 조회해 반환 (LLM 재생성 없음)."""
        t0 = time.time()
        records = []
        for ref in state["answer"].get("used_refs", []):
            rec = self._lookup(ref)
            if rec:
                records.append(rec)
        out = {"verified": records,
               "trace": [{"node": "verify", "resolved": len(records),
                          "latency_ms": int((time.time() - t0) * 1000)}]}
        self._write_trace(state, out["trace"])
        return out

    def _lookup(self, ref):
        """ref_key 또는 doc_no로 Chroma에서 원문 레코드 1건 조회."""
        for cn in self.index.colls:
            col = self.index.client.get_collection(cn)
            for field in ("ref_key", "doc_no"):
                got = col.get(where={field: ref}, limit=1,
                              include=["documents", "metadatas"])
                if got["ids"]:
                    m = got["metadatas"][0]
                    return {"ref": ref, "collection": cn, "metadata": m,
                            "text": got["documents"][0]}
        return None

    def _write_trace(self, state, verify_trace):
        TRACE_LOG.parent.mkdir(parents=True, exist_ok=True)
        entry = {"question": state.get("question"),
                 "rewritten": state.get("rewritten"),
                 "route": state.get("route"),
                 "retrieved_refs": [h["ref_key"] or h["doc_no"]
                                    for h in state.get("retrieved", [])],
                 "audit_cases": [{"case_id": c.get("case_id"), "score": c.get("score")}
                                 for c in state.get("audit_cases", [])],
                 "answer": _clip(state.get("answer", {}).get("answer"), 300),
                 "used_refs": state.get("answer", {}).get("used_refs", []),
                 "trace": state.get("trace", []) + verify_trace}
        with TRACE_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def attach_eval(question, eval_obj):
    """B트랙 실시간 평가 결과를 최근 트레이스 레코드에 병합(질문-답변-평가 한 레코드).

    - 답변 후 UI에서 판사 채점이 끝나면 호출. 파일 끝에서 질문이 일치하고 아직
      eval이 없는 마지막 레코드에 eval 필드를 붙여 재기록.
    - 평가는 부가 기능 → 어떤 예외도 조용히 무시(답변 흐름에 영향 없음).
    """
    try:
        if not TRACE_LOG.exists() or not eval_obj:
            return
        lines = TRACE_LOG.read_text(encoding="utf-8").splitlines()
        for i in range(len(lines) - 1, -1, -1):
            try:
                rec = json.loads(lines[i])
            except json.JSONDecodeError:
                continue
            if rec.get("question") == question and "eval" not in rec:
                rec["eval"] = eval_obj
                lines[i] = json.dumps(rec, ensure_ascii=False)
                TRACE_LOG.write_text("\n".join(lines) + "\n", encoding="utf-8")
                return
    except Exception:
        return


def build_graph(index, checkpoint_path=None, api_key=None, local=False):
    L.configure_langsmith()   # 트레이싱 활성/조용히 비활성 결정 (키 없으면 off)
    p = Pipeline(index, api_key=api_key, local=local)
    g = StateGraph(State)
    g.add_node("rewrite", p.rewrite)
    g.add_node("route", p.route)
    g.add_node("retrieve", p.retrieve)
    g.add_node("audit_lookup", p.audit_lookup)   # 접근법 B: retrieve와 병렬 사이드카
    g.add_node("answer", p.answer)
    g.add_node("verify", p.verify)
    # 진입: 히스토리(후속질문 맥락 해소)가 있으면 rewrite 먼저, 없으면 route부터(비용 절감).
    g.add_conditional_edges(START, _entry_edge, {"rewrite": "rewrite", "route": "route"})
    # rewrite는 두 경로에서 온다: ①진입(맥락 해소, route 전) ②retrieve 재시도(route 후).
    # route가 이미 끝났으면 재시도이므로 route를 다시 안 돌리고 retrieve로 바로 간다.
    g.add_conditional_edges("rewrite", _after_rewrite_edge, {"route": "route", "retrieve": "retrieve"})
    # route 이후 retrieve(답변 근거)와 audit_lookup(참고용 사이드카)로 fan-out, 둘 다 answer로 합류.
    g.add_edge("route", "retrieve")
    g.add_edge("route", "audit_lookup")
    # retrieve 후 1차 검색 결과가 부실하면(top 리랭커 점수 <0.6) rewrite로 재시도(1회 한정),
    # 아니면 바로 answer로.
    g.add_conditional_edges("retrieve", _after_retrieve_edge, {"rewrite": "rewrite", "answer": "answer"})
    g.add_edge("audit_lookup", "answer")   # answer는 두 노드 완료 후 1회 실행(fan-in)
    g.add_edge("answer", "verify")
    g.add_edge("verify", END)

    saver = None
    if checkpoint_path:
        import sqlite3
        from langgraph.checkpoint.sqlite import SqliteSaver
        conn = sqlite3.connect(str(checkpoint_path), check_same_thread=False)
        saver = SqliteSaver(conn)
    return g.compile(checkpointer=saver)
