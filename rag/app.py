# -*- coding: utf-8 -*-
"""KASB 회계기준 RAG — Streamlit UI.

기존 graph.py/search.py를 그대로 호출해 화면만 입힌다 (새 파이프라인 로직 없음).
- A방식: 단계 표시 + 근거 먼저 + 답변 토큰 스트리밍으로 retrieve 대기 체감 완화
- 3층 신뢰 UI: 답변(+인용) / 근거 원문 카드(verify DB조회, PDF 페이지 렌더) / 해설
실행:  streamlit run rag/app.py
"""
import os
import re
import sys
import time

# 배포 환경(HF Spaces 등)에서 `streamlit run rag/app.py`로 실행되면 sys.path에 repo 루트가
# 없어 `from rag import ...`가 깨진다. 실행 방식과 무관하게 repo 루트를 경로에 추가.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamlit as st

from rag import common as C
from rag import llm as L

st.set_page_config(page_title="KASB 회계기준 RAG", page_icon="📘", layout="wide")
CKPT = C.ROOT / "rag" / "checkpoints_ui.db"
COLL_LABEL = {"kifrs_standards": "K-IFRS 기준서", "kgaap_standards": "일반기업 기준서",
              "qa_kifrs": "K-IFRS 질의회신", "qa_kgaap": "일반기업 질의회신"}


# ---------------------------------------------------------------- 무거운 리소스
@st.cache_resource(show_spinner="인덱스 로드 중 (Chroma + BGE-M3 + 리랭커 + BM25)...")
def load_index():
    from rag.search import Index
    return Index()


def make_graph(api_key, local):
    from rag.graph import build_graph
    return build_graph(load_index(), checkpoint_path=CKPT, api_key=api_key, local=local)


# 기준서 게시판 목록 URL (상세 View는 POST 방식이라 직접 링크 불가 → 게시판으로 안내).
# ※ PDF 페이지 렌더 기능은 폐기: 맥미니에서 미작동 확인, KASB 링크로 통일 (CLAUDE.md 참조).
KASB_BOARD_URL = {
    "kifrs_standards": "https://www.kasb.or.kr/front/board/ingAccountingList.do",
    "kgaap_standards": "https://www.kasb.or.kr/front/board/List3003.do",
}


# ---------------------------------------------------------------- 근거 카드 (3층 중 2층)
def render_evidence(items, used_refs=frozenset(), anchors=None):
    """검색된 근거 원문 카드 (DB 원문 그대로, LLM 재생성 아님). 답변보다 먼저 렌더."""
    st.markdown(f"##### 📄 근거 원문 ({len(items)}건)")
    st.caption("검색된 원문 그대로입니다 (LLM 재생성 아님). ★ = 답변이 인용한 근거. "
               "답변의 인용 배지를 누르면 해당 카드로 이동합니다.")
    for it in items:
        ref = it["ref_key"] or it["doc_no"]
        coll = COLL_LABEL.get(it["collection"], it["collection"])
        star = "★ " if ref in used_refs else ""
        if anchors and ref in anchors:    # 배지 클릭 시 스크롤 목표
            st.markdown(f"<div id='{anchors[ref]}'></div>", unsafe_allow_html=True)
        with st.expander(f"{star}[{ref}] · {coll} · {it.get('source','')}",
                         expanded=bool(star)):
            st.markdown(md_escape(it["text"][:1500]))   # 원문 그대로(마크다운 오해석·취소선 방지)
            cols = st.columns([1, 1, 2])
            cols[0].caption(f"ref_key: {it.get('ref_key') or '—'}")
            cols[1].caption(f"doc_no: {it.get('doc_no') or '—'}")
            if it.get("url"):                      # 질의회신 → 직접 상세 링크(GET)
                cols[2].markdown(f"[↗ KASB 원문 보기]({it['url']})")
            else:                                  # 기준서/용어 → 게시판 링크 + 번호 안내
                burl = KASB_BOARD_URL.get(it["collection"])
                if burl:
                    cols[2].markdown(f"[↗ KASB 기준서 게시판]({burl})")
                    cols[2].caption(f"게시판에서 이 번호로 찾아보세요: **{ref}**")


# ---------------------------------------------------------------- 인용 하이라이트/앵커
import hashlib


def anchor_id(ref):
    """ref_key(한글·특수문자 포함)를 URL-safe 앵커 id로."""
    return "ev-" + hashlib.md5(ref.encode("utf-8")).hexdigest()[:8]


def md_escape(text):
    """원문의 마크다운 특수문자를 이스케이프해 '그대로' 표시(원문 훼손 금지).

    예: 원문 '문단 97~106 참조 … 상각하지 아니한다(문단 107~110'의 두 ~가
    취소선(~...~)으로 오해석되던 버그 방지. 취소선은 근거 표시에 절대 안 씀.
    """
    return re.sub(r"([\\`*_{}\[\]()#+\-.!~|<>])", r"\\\1", text)


def highlight_citations(text, valid_refs, anchors=None):
    """인용 [ref]는 배지(클릭→앵커)로, 나머지 텍스트는 이스케이프(원문 ~,* 등 오해석 방지)."""
    out = []
    for part in re.split(r"(\[[^\[\]]+\])", text):
        m = re.fullmatch(r"\[([^\[\]]+)\]", part)
        if m and m.group(1) in valid_refs:
            r = m.group(1)
            badge = f"**`[{r}]`**"
            out.append(f"[{badge}](#{anchors[r]})" if anchors and r in anchors else badge)
        else:
            out.append(md_escape(part))   # 비인용 텍스트: 마크다운 특수문자 이스케이프
    return "".join(out)


# ---------------------------------------------------------------- 실시간 평가 (B트랙)
def render_eval(eval_cfg, question, ans, retrieved):
    """답변 후 판사 LLM으로 Faithfulness/Answer Relevancy 채점 + 표시.

    - 토글 off(eval_cfg['on']=False) 또는 키 없음 → 아무 호출도 안 함(조기 반환).
    - 근거 없는 답변(refusal, has_grounds=False)은 채점 스킵.
    - 판사=답변 벤더면 자기편향 경고. 판사 실패 시 답변엔 영향 없이 조용히 실패 표시.
    - 결과는 traces.jsonl의 해당 레코드에 병합(질문-답변-평가 한 레코드).
    """
    if not (eval_cfg.get("on") and eval_cfg.get("key")):
        return
    if not ans.get("has_grounds"):
        st.caption("🔍 품질 평가: 근거 없는 답변(refusal)은 채점하지 않습니다.")
        return
    grounding = "\n\n".join(h.get("text", "") for h in (retrieved or []))
    with st.spinner(f"품질 평가 중 (판사: {eval_cfg['vendor']})…"):
        try:
            from rag.eval.judge import Judge
            res = Judge(eval_cfg["vendor"], eval_cfg["key"]).evaluate(
                question, ans.get("answer", ""), grounding)
        except Exception:
            res = None
    if not res:
        st.caption("🔍 품질 평가: 판사 호출 실패 — 평가만 건너뜁니다(답변은 정상).")
        return

    faith = res.get("faithfulness")
    rel = res.get("answer_relevancy")
    def _fmt(x):
        return f"{x:.2f}" if isinstance(x, (int, float)) else "—"
    bias = " · ⚠ 자기편향 가능" if eval_cfg["vendor"] == eval_cfg.get("answer_vendor") else ""
    st.markdown(f"🔍 **품질 평가** (판사: {res.get('judge_vendor')}) — "
                f"근거 충실도 **{_fmt(faith)}** · 질문 관련성 **{_fmt(rel)}**{bias}")
    with st.expander("평가 상세"):
        st.write(f"- **근거 충실도(Faithfulness)**: {_fmt(faith)} "
                 "— 답변 주장이 검색 근거로 뒷받침되는 비율")
        unsup = res.get("unsupported") or []
        if unsup:
            st.write("  - 근거 없는 주장(환각 의심):")
            for s in unsup[:5]:
                st.write(f"    - {s}")
        st.write(f"- **질문 관련성(Answer Relevancy)**: {_fmt(rel)} "
                 f"— {res.get('relevancy_reason', '')}")
        if bias:
            st.caption("⚠ 판사와 답변 모델이 같은 벤더입니다. 다른 벤더 사용을 권장합니다.")
    # 트레이스에 평가 병합 (부가 기능 — 실패해도 조용히)
    try:
        from rag.graph import attach_eval
        attach_eval(question, res)
    except Exception:
        pass


# ---------------------------------------------------------------- 사이드바
def sidebar():
    st.sidebar.title("⚙️ 설정")
    st.sidebar.markdown("**API 키**")
    key_in = st.sidebar.text_input("OpenAI API 키", type="password",
                                   help="세션에만 보관, 파일·로그 저장 안 함",
                                   value=st.session_state.get("api_key", ""))
    if key_in:
        st.session_state.api_key = key_in   # session_state만 (파일 저장 금지)
    import os
    env_key = bool(os.environ.get("OPENAI_API_KEY"))
    # 우선순위: 입력 > env > .env
    eff_key = st.session_state.get("api_key") or None
    if eff_key:
        st.sidebar.success("입력 키 사용 중 (세션 메모리)")
    elif env_key:
        st.sidebar.info("환경변수/.env 키 사용")
    else:
        st.sidebar.warning("키 없음 — 입력하거나 GPT 대신 로컬 모델을 쓰세요.")

    st.sidebar.markdown("**모델**")
    # 배포 환경(KASB_CLOUD)은 GPT만 지원 — EXAONE는 Ollama 로컬 구동이라 관리형 호스팅 불가.
    if os.environ.get("KASB_CLOUD"):
        st.sidebar.radio("모델 선택", ["GPT (기본)"], captions=["gpt-4o-mini + gpt-5.5"])
        st.sidebar.caption("☁️ 배포 환경에서는 GPT만 지원합니다 "
                           "(EXAONE는 로컬 전용 — 저장소를 클론해 로컬에서 사용).")
        local = False
    else:
        mode = st.sidebar.radio("모델 선택", ["GPT (기본)", "로컬 EXAONE"],
                                captions=["gpt-4o-mini + gpt-5.5", "EXAONE 3.5 (Ollama)"])
        local = mode.startswith("로컬")
        if local:
            try:
                L.check_ollama_model(L.LOCAL_MODEL)
                st.sidebar.success(f"로컬 모델 준비됨: {L.LOCAL_MODEL}")
            except L.LLMError as e:
                st.sidebar.error(str(e))
                local = "unavailable"

    # 답변 품질 평가 (B트랙, 기본 off — 체크 안 하면 어떤 평가 호출도 없음)
    st.sidebar.divider()
    eval_on = st.sidebar.checkbox("🔍 답변 품질 평가", value=False,
                                  help="체크 시에만 판사 LLM이 호출됩니다 (속도·비용 영향)")
    eval_cfg = {"on": eval_on}
    # 답변 모델 벤더: GPT=OpenAI, 로컬 EXAONE는 클라우드 벤더 아님 → 자기편향 대상 아님
    answer_vendor = None if local else "OpenAI"
    if eval_on:
        from rag.eval.judge import VENDORS
        vendor = st.sidebar.selectbox("판사 벤더", list(VENDORS),
                                      help="답변 모델과 다른 벤더를 권장합니다 (자기편향 방지)")
        jkey = st.sidebar.text_input(f"{vendor} API 키", type="password",
                                     value=st.session_state.get("judge_key_" + vendor, ""))
        if jkey:
            st.session_state["judge_key_" + vendor] = jkey   # session_state만
        st.sidebar.caption("답변 모델과 다른 벤더 권장 (자기편향 방지)")
        if vendor == answer_vendor:
            st.sidebar.warning("⚠ 판사와 답변 모델이 같은 벤더 — 자기편향 가능")
        eval_cfg.update({"vendor": vendor, "key": jkey or None,
                         "answer_vendor": answer_vendor})

    if st.sidebar.button("🔄 대화 초기화 (새 thread)"):
        st.session_state.thread = "ui-" + str(int(time.time()))
        st.session_state.messages = []
        st.rerun()
    st.sidebar.caption(f"thread: {st.session_state.get('thread','')}")
    st.sidebar.divider()
    st.sidebar.caption("made by gyuyeong")
    return eff_key, local, eval_cfg


# ---------------------------------------------------------------- 메인
def main():
    st.title("📘 회계기준 Manager")
    if "thread" not in st.session_state:
        st.session_state.thread = "ui-" + str(int(time.time()))
        st.session_state.messages = []

    api_key, local, eval_cfg = sidebar()

    # 지난 대화 렌더
    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    q = st.chat_input("질문을 입력하세요")
    if not q:
        return
    with st.chat_message("user"):
        st.markdown(q)
    st.session_state.messages.append({"role": "user", "content": q})

    # 모델 준비 점검
    if local == "unavailable":
        with st.chat_message("assistant"):
            st.error("로컬 모델이 준비되지 않았습니다. 사이드바 안내를 확인하세요.")
        return
    try:
        L.get_llm("route", local=bool(local), api_key=api_key)
    except L.LLMError as e:
        with st.chat_message("assistant"):
            st.error(str(e))
        return

    graph = make_graph(api_key, bool(local))
    cfg = {"configurable": {"thread_id": st.session_state.thread}}

    with st.chat_message("assistant"):
        status = st.status("질문 처리 중…", expanded=True)
        ev_box = st.empty()          # 근거 카드 (답변보다 먼저). st.empty=교체형(중복 방지)
        ans_box = st.empty()         # 답변 스트리밍
        final = {}
        answer_buf = []
        # A방식: updates(단계) + messages(answer 토큰) 동시 스트림
        for mode, chunk in graph.stream({"question": q}, cfg,
                                        stream_mode=["updates", "messages"]):
            if mode == "updates":
                for node, delta in chunk.items():
                    final.update(delta)
                    if node == "rewrite":
                        status.update(label="🔎 검색 범위 결정 중…")
                        rw = delta.get("rewritten", "")
                        if rw and rw != q:
                            status.write(f"질문 재작성: *{rw}*")
                    elif node == "route":
                        r = delta.get("route", {})
                        labels = [COLL_LABEL.get(c, c) for c in r.get("collections", [])]
                        status.write(f"라우팅: **{r.get('qtype')}** → {', '.join(labels)}")
                        status.update(label="📚 근거 검색 중… (BM25+dense+리랭킹)")
                    elif node == "retrieve":
                        status.update(label="✍️ 답변 생성 중…")
                        retrieved = delta.get("retrieved", [])
                        status.write(f"근거 {len(retrieved)}건 확보 — 답변보다 먼저 표시합니다")
                        # A방식 핵심: 근거 카드를 답변 스트리밍 전에 렌더 (retrieve 직후)
                        with ev_box.container():     # st.empty.container()=교체 렌더
                            render_evidence(retrieved)
                            st.markdown("##### 💬 답변")
            else:  # messages: answer 토큰 스트리밍
                tok = getattr(chunk[0], "content", "")
                if tok:
                    answer_buf.append(tok)
                    ans_box.markdown("".join(answer_buf) + "▌")

        status.update(label="✅ 완료", state="complete", expanded=False)
        ans = final.get("answer", {})
        valid = {h["ref_key"] or h["doc_no"] for h in final.get("retrieved", [])}
        used = set(ans.get("used_refs", []))
        anchors = {r: anchor_id(r) for r in used}   # 인용 배지 → 근거 카드 스크롤
        # 3층-2: 근거 카드 — 인용된 것 ★+앵커로 '교체' 렌더 (중복 없이 이전 것을 대체)
        if final.get("retrieved") and used:
            with ev_box.container():
                render_evidence(final["retrieved"], used_refs=used, anchors=anchors)
                st.markdown("##### 💬 답변")
        # 3층-1: 답변 (인용 배지 클릭 → 카드로 스크롤)
        ans_box.markdown(highlight_citations(ans.get("answer", ""), valid, anchors))
        # 3층-3: 해설 (원문과 시각 구분)
        with st.container():
            st.divider()
            if ans.get("has_grounds"):
                st.caption(f"🔗 사용 근거: {', '.join(ans.get('used_refs', [])) or '—'}  ·  "
                           f"이 답변은 위 근거 원문에서 생성됐습니다.")
            else:
                st.caption("⚠️ 근거를 찾지 못해 답변하지 않았습니다 (환각 방지).")

        # 3층-4(선택): 실시간 품질 평가 — 토글 on일 때만 판사 호출
        render_eval(eval_cfg, q, ans, final.get("retrieved", []))

    st.session_state.messages.append(
        {"role": "assistant", "content": highlight_citations(ans.get("answer", ""), valid)})


if __name__ == "__main__":
    main()
