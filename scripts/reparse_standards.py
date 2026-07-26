# -*- coding: utf-8 -*-
"""기준서(3001/3003) 문단 레코드를 캐시된 원문에서 재파싱 (네트워크 재크롤링 없음).

배경: parsers/standard_split.py의 문단 경계 파싱 버그(2026-07-26 수정 — 소제목처럼
보이는 애매한 줄이 직전 문단 끝에 잘못 붙던 문제) 수정을 기존 data/parsed/*.jsonl에
반영하기 위함. 용어 레코드(record_type=="term")는 이 버그와 무관하므로 그대로 보존하고,
문단 레코드(record_type=="paragraph")만 data/raw/<board>/text/<seq>.txt 캐시 원문 +
수정된 split_standard/split_kgaap_chapter로 다시 만든다.

사용법:
    python3 scripts/reparse_standards.py 3001 3003
    python3 scripts/reparse_standards.py 3001 3003 --dry-run   # 파일 안 건드리고 통계만
"""
import argparse
import json
from pathlib import Path

from rag import common as C
from parsers.standard_split import split_kgaap_chapter, split_standard

ROOT = C.ROOT
STD_BOARDS = {
    "3001": {"kind": "kifrs"},
    "3003": {"kind": "kgaap"},
}


def _leak_count(records):
    """검증용: 문단 텍스트의 마지막 줄이 아직도 '소제목처럼' 보이는(=결함) 건수."""
    from parsers.standard_split import _looks_like_orphan_heading
    import re
    subitem_mark_re = re.compile(r"^[⑴-⒇㈀-㈩\(][^ ]{0,3}\s")
    n = 0
    for r in records:
        t = r["text"]
        last_line = t.rsplit("\n", 1)[-1].strip()
        if not last_line or last_line == t or subitem_mark_re.match(last_line):
            continue
        if _looks_like_orphan_heading(last_line):
            n += 1
    return n


def reparse_board(board_id, dry_run=False):
    kind = STD_BOARDS[board_id]["kind"]
    path = ROOT / "data" / "parsed" / (board_id + ".jsonl")
    old_records = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]

    old_paragraphs = [r for r in old_records if r.get("record_type") == "paragraph"]
    kept_terms = [r for r in old_records if r.get("record_type") != "paragraph"]

    # doc_no별 공통 메타데이터(문단 전문 재생성에 필요한 것만) 복원
    common_by_doc = {}
    for r in old_paragraphs:
        common_by_doc.setdefault(r["doc_no"], {
            "source": r["source"], "board_id": r["board_id"], "doc_no": r["doc_no"],
            "standard_title": r["standard_title"], "src_file": r["src_file"],
            "crawled_at": r["crawled_at"], "standard_no": r["standard_no"],
            "standard_name": r["standard_name"],
        })

    text_dir = ROOT / "data" / "raw" / board_id / "text"
    new_paragraphs = []
    missing_cache = []
    for doc_no, common in common_by_doc.items():
        seq = doc_no.split("-")[-1]
        text_path = text_dir / (seq + ".txt")
        if not text_path.exists():
            missing_cache.append(doc_no)
            continue
        text = text_path.read_text(encoding="utf-8")
        if kind == "kgaap":
            chapter = common["standard_no"].replace("제", "").replace("장", "")
            paras = split_kgaap_chapter(text, chapter)
        else:
            std_no = common["standard_no"]
            paras = split_standard(text, std_no)
        for p in paras:
            rec = dict(common, record_type="paragraph")
            rec.update(p)
            new_paragraphs.append(rec)

    print(f"[{board_id}] 원본 문단 {len(old_paragraphs)}건 → 재생성 {len(new_paragraphs)}건 "
          f"(용어 등 비문단 레코드 {len(kept_terms)}건 그대로 보존)")
    if missing_cache:
        print(f"[{board_id}] 캐시 원문 없음(스킵, 원본 유지 필요): {len(missing_cache)}건 — {missing_cache[:5]}")

    old_leaks = _leak_count(old_paragraphs)
    new_leaks = _leak_count(new_paragraphs)
    print(f"[{board_id}] 정밀 결함 건수: 수정 전 {old_leaks} → 수정 후 {new_leaks}")

    if dry_run:
        print(f"[{board_id}] --dry-run: 파일 변경 없음")
        return

    # 캐시가 없어 재생성 못 한 문서는 기존 레코드를 그대로 보존(유실 방지)
    missing_doc_nos = set(missing_cache)
    fallback_old = [r for r in old_paragraphs if r["doc_no"] in missing_doc_nos]

    out_records = kept_terms + new_paragraphs + fallback_old
    bak_path = path.with_suffix(".jsonl.bak")
    if not bak_path.exists():
        bak_path.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"[{board_id}] 백업: {bak_path}")

    with path.open("w", encoding="utf-8") as f:
        for r in out_records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"[{board_id}] 저장 완료: {path} (총 {len(out_records)}건)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("boards", nargs="+", choices=list(STD_BOARDS))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    for b in args.boards:
        reparse_board(b, dry_run=args.dry_run)
    print("ALL_DONE", flush=True)


if __name__ == "__main__":
    main()
