# -*- coding: utf-8 -*-
"""UI 헤드리스 검증: 앱이 쓰는 스트리밍·근거데이터·PDF렌더를 브라우저 없이 확인."""
import time

from rag import common as C
from rag.graph import build_graph
from rag.search import Index
from rag.app import KASB_BOARD_URL

CKPT = C.ROOT / "rag" / "checkpoints_uicheck.db"


def stream_case(graph, q, thread):
    """앱과 동일하게 stream_mode=[updates,messages] 소비."""
    steps, tokens, final = [], [], {}
    t0 = time.time()
    first_token_at = None
    for mode, chunk in graph.stream({"question": q},
                                    {"configurable": {"thread_id": thread}},
                                    stream_mode=["updates", "messages"]):
        if mode == "updates":
            for node, delta in chunk.items():
                steps.append(node)
                final.update(delta)
        else:
            tok = getattr(chunk[0], "content", "")
            if tok:
                if first_token_at is None:
                    first_token_at = time.time() - t0
                tokens.append(tok)
    return steps, tokens, final, first_token_at


def main():
    print("인덱스 로드...", flush=True)
    graph = build_graph(Index(), checkpoint_path=CKPT)  # 키는 env/.env

    # 케이스 1: 스트리밍 단계·토큰·근거
    steps, tokens, final, first_at = stream_case(
        graph, "1년 임차 후 1년 연장하면 단기리스 면제 되나?", "u1")
    print("\n[케이스1] 단계 이벤트 순서:", steps)
    print(f"  answer 토큰 수: {len(tokens)} (스트리밍 {'OK' if len(tokens) > 5 else '✗'}), "
          f"첫 토큰 {first_at:.1f}s")
    print(f"  답변: {''.join(tokens)[:90]!r}")
    ver = final.get("verified", [])
    print(f"  근거 원문(verify): {[v['ref'] for v in ver]}")
    # rewrite는 조건부(히스토리 있으면 route 전, 없으면 검색 신뢰도 미달 시 retrieve 후 재시도)
    # → 이번 첫 턴(히스토리 없음)은 rewrite가 아예 안 나타날 수도 있어 고정 순서 대신
    # 상대 순서만 확인한다(rag/graph.py의 조건부 재시도 설계 참조).
    assert {"retrieve", "answer", "verify"} <= set(steps), steps
    assert steps.index("retrieve") < steps.index("answer") < steps.index("verify"), steps
    if "rewrite" in steps:
        assert steps.index("rewrite") > steps.index("route"), steps   # 없으면 재시도 경로 위반
    assert len(tokens) > 5, "answer 토큰 스트리밍 안 됨"
    assert ver, "verify 근거 없음"
    # 근거를 답변 전에 렌더하려면 retrieved에 원문·링크 메타가 있어야 함
    rt = final.get("retrieved", [])
    has_meta = any(("url" in h) and ("src_file" in h) and ("page_no" in h) for h in rt)
    print(f"  retrieved 메타(url/src_file/page_no) 포함: {'OK' if has_meta else '✗'}")
    assert has_meta, "retrieved에 카드 렌더용 메타 없음"

    # 케이스 4: 환각 방지
    _, tok4, final4, _ = stream_case(graph, "미국 세법상 감가상각 내용연수는?", "u4")
    ans4 = "".join(tok4) or final4.get("answer", {}).get("answer", "")
    print(f"\n[케이스4] 근거없음 답변: {ans4[:40]!r} → 환각방지 "
          f"{'OK' if '근거를 찾지 못' in ans4 else '✗'}")

    # 기준서 카드 KASB 링크 (PDF 렌더 폐기 → 게시판 링크로 통일)
    print("\n[기준서 KASB 링크]")
    std_hits = [h for h in final.get("retrieved", [])
                if h["collection"].endswith("standards")]
    if std_hits:
        h = std_hits[0]
        burl = KASB_BOARD_URL.get(h["collection"])
        print(f"  {h['ref_key']} ({h['collection']}) → {burl}")
        assert burl and burl.startswith("http"), "KASB 게시판 링크 없음"
    # 질의회신 카드는 직접 상세 링크(url)
    qa_hits = [h for h in final.get("retrieved", []) if h.get("url")]
    if qa_hits:
        print(f"  질의회신 직접링크: {qa_hits[0]['url'][:55]}...")
    print("\n===== UI 데이터 경로 검증 통과 =====")


if __name__ == "__main__":
    main()
