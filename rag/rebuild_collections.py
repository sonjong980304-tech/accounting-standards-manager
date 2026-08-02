# -*- coding: utf-8 -*-
"""컬렉션 clean rebuild — 기존 컬렉션을 지우고 현재 JSONL 로 처음부터 재임베딩.

**언제 필요한가**: `common.record_id` 가 줄번호 기반(`3001:42`)에서 문서 안정키 기반
(`3001-2974#제1116호 문단 7`)으로 바뀌었다. 옛 id 로 적재된 레코드는 새 id 와 겹치지 않아
upsert 로 갱신되지 않고 **좀비로 남는다**(같은 내용이 옛/새 id 두 벌로 검색에 노출).
그래서 id 체계 변경 후 **한 번은** 해당 컬렉션을 통째로 지우고 다시 넣어야 한다.

한 번 정리하고 나면 이후 기준서 개정 재수집(crawl.standards_crawler)은 rebuild 없이
`python3 -m rag.embed` 만으로 반영된다 — 바뀐 문단만 새 id 로 추가되고 다른 문서 id 는
그대로이기 때문이다.

사용법:
    python3 -m rag.rebuild_collections --dry-run                 # 계획만 출력
    python3 -m rag.rebuild_collections --yes                     # 기준서 2개 컬렉션 재구축
    python3 -m rag.rebuild_collections --collections qa_kifrs qa_kgaap --yes

주의: 되돌릴 수 없다. 먼저 data/chroma 를 백업할 것.
"""
import argparse

from rag import common as C

# 기준서 컬렉션이 기본 대상 — 개정 재수집으로 레코드 수가 변하는 쪽이라 id 안정성이 가장 중요.
DEFAULT_TARGETS = ["kifrs_standards", "kgaap_standards"]
ALL_MAPPINGS = dict(C.COLLECTIONS, **C.AUDIT_COLLECTIONS)


def resolve_targets(names):
    """대상 컬렉션명 검증. 미지정 시 기준서 2개."""
    if not names:
        return list(DEFAULT_TARGETS)
    unknown = [n for n in names if n not in ALL_MAPPINGS]
    if unknown:
        raise SystemExit("알 수 없는 컬렉션: {} (가능: {})".format(
            ", ".join(unknown), ", ".join(sorted(ALL_MAPPINGS))))
    return list(names)


def drop_collections(client, names):
    for name in names:
        try:
            client.delete_collection(name)
            print("컬렉션 제거: {}".format(name), flush=True)
        except Exception:   # noqa: BLE001 — 없으면 이미 목표 상태
            print("컬렉션 없음(스킵): {}".format(name), flush=True)


def rebuild(names):
    """대상 컬렉션을 지우고 해당 JSONL 만 재임베딩."""
    from rag import embed
    targets = resolve_targets(names)
    drop_collections(C.get_chroma(), targets)
    embed.run({n: ALL_MAPPINGS[n] for n in targets})


def main():
    ap = argparse.ArgumentParser(description="Chroma 컬렉션 clean rebuild")
    ap.add_argument("--collections", nargs="+", default=None,
                    help="대상 컬렉션명 (기본: {})".format(" ".join(DEFAULT_TARGETS)))
    ap.add_argument("--dry-run", action="store_true", help="계획만 출력")
    ap.add_argument("--yes", action="store_true", help="확인 프롬프트 생략")
    args = ap.parse_args()

    targets = resolve_targets(args.collections)
    print("대상 컬렉션: {}".format(", ".join(targets)))
    print("소스 JSONL: {}".format(
        {n: ALL_MAPPINGS[n] for n in targets}))
    print("Chroma 경로: {}".format(C.CHROMA_DIR))
    if args.dry_run:
        print("--dry-run — 아무것도 변경하지 않았습니다.")
        return
    if not args.yes:
        ans = input("위 컬렉션을 삭제하고 재임베딩합니다. 되돌릴 수 없습니다. 진행할까요? [y/N] ")
        if ans.strip().lower() not in ("y", "yes"):
            print("취소했습니다.")
            return
    rebuild(targets)


if __name__ == "__main__":
    main()
