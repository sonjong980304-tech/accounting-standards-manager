# -*- coding: utf-8 -*-
"""매일 자동 갱신: 질의회신 5개 게시판 + 기준서 2개 게시판을 증분 크롤링한 뒤,
새로 파싱된 레코드만 임베딩(rag.embed.run, 기존 id는 자동 스킵).

크롤러(crawl_board/crawl_standards)와 임베딩(rag.embed) 모두 이미 "이미 있는 건 스킵"
로직을 갖고 있어(data/state/*.json, Chroma existing_ids), 매일 재실행해도 새로 올라온
글만 효율적으로 처리된다(전체 재수집·재임베딩 없음). 한 게시판이 실패해도(네트워크 등)
나머지 게시판은 계속 진행 — 실패는 failures.log(log_failure)에 남는다.

사용법:
    python3 -m rag.daily_update              # 전 게시판 증분 크롤링 + 임베딩
    python3 -m rag.daily_update --no-embed   # 크롤링만, 임베딩 스킵

크론 등록(매일)은 별도 스크립트: rag/install_daily_scheduler.sh (--print 로 미리보기).
"""
import argparse
import time

from crawl.crawler import BOARDS as QA_BOARDS
from crawl.crawler import crawl_board
from crawl.standards_crawler import STD_BOARDS, crawl_standards

# 안정된 순서(게시판별 부하 분산 목적 없음, 로그 가독성용) — board→컬렉션 매핑은 common.COLLECTIONS 참조.
QA_BOARD_ORDER = ("016001", "016002", "016005", "016003", "016006")
STD_BOARD_ORDER = ("3001", "3003")


def run_qa():
    """질의회신 게시판별 증분 크롤링(전체 페이지, 이미 수집한 seq는 state로 스킵)."""
    results = {}
    for board_id in QA_BOARD_ORDER:
        print(f"\n=== 질의회신 [{board_id}] {QA_BOARDS[board_id]['source']} ===", flush=True)
        try:
            results[board_id] = crawl_board(board_id, max_pages=None)
            print(f"[{board_id}] 신규 {results[board_id][0]}건, "
                  f"기수집 {results[board_id][1]}건 스킵", flush=True)
        except Exception as e:  # noqa: BLE001 — 한 게시판 실패가 나머지를 막지 않게
            print(f"[{board_id}] 실패: {e!r}", flush=True)
            results[board_id] = None
    return results


def run_standards():
    """기준서 게시판별 증분 크롤링(단일 목록 페이지, 이미 수집한 seq는 state로 스킵)."""
    results = {}
    for board_id in STD_BOARD_ORDER:
        print(f"\n=== 기준서 [{board_id}] {STD_BOARDS[board_id]['source']} ===", flush=True)
        try:
            results[board_id] = crawl_standards(board_id)
            print(f"[{board_id}] 신규 {results[board_id][0]}건, "
                  f"개정 재수집 {results[board_id][2]}건, "
                  f"기수집 {results[board_id][1]}건 스킵", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[{board_id}] 실패: {e!r}", flush=True)
            results[board_id] = None
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-embed", action="store_true", help="크롤링만, 임베딩 스킵")
    args = ap.parse_args()
    t0 = time.time()

    results = {**run_qa(), **run_standards()}
    new_total = sum(v[0] for v in results.values() if v)
    # 기준서 개정 재수집분(3-튜플의 3번째)도 임베딩 대상 — 신규 글이 0건이어도 문단이 바뀜
    recollected = sum(v[2] for v in results.values() if v and len(v) > 2)
    failed = [b for b, v in results.items() if v is None]
    print(f"\n크롤링 완료: 신규 {new_total}건, 개정 재수집 {recollected}건"
          f" (소요 {time.time() - t0:.0f}s)"
          + (f" · 실패 게시판: {failed}" if failed else ""), flush=True)

    if args.no_embed:
        print("임베딩 스킵(--no-embed).", flush=True)
        return
    if new_total == 0 and recollected == 0:
        print("신규·개정 문서 없음 — 임베딩 스킵.", flush=True)
        return

    from rag import embed
    embed.run()
    print(f"전체 완료 (총 소요 {time.time() - t0:.0f}s)", flush=True)


if __name__ == "__main__":
    main()
