# -*- coding: utf-8 -*-
"""감리지적사례 동기화: audit-sentinel cases.jsonl → KASB audit_cases 컬렉션 재임베딩.

audit-sentinel(별도 레포)의 원본을 매 실행마다 직접 읽지 않고, KASB `data/parsed/` 안으로
`record_type="audit_case"` 형식의 JSONL로 **복사·변환**해 둔 뒤 임베딩한다(두 레포 결합도↓).
임베딩은 rag.embed.run(AUDIT_COLLECTIONS)로 위임(기존 흐름 재사용). 재동기화 시 내용 변경분을
확실히 반영하려고 audit_cases 컬렉션을 먼저 비우고(clean rebuild) 65건을 다시 적재한다.

`standard_superseded`(인용 기준서가 폐지·개정됐다는 경고)는 audit-sentinel이 넘겨준 값을 쓰지
않는다 — 그쪽은 수작업 목록 기반이라 기준서가 새로 개정돼도 갱신되지 않는다. 대신 매 동기화마다
data/parsed/3001.jsonl(이 프로젝트가 실제로 수집한 현행 기준서)과 대조해 재계산한다.

사용법:
    python3 -m rag.sync_audit_cases                # 변환 + 재임베딩(65건 clean rebuild)
    python3 -m rag.sync_audit_cases --no-embed     # JSONL 변환만(임베딩 스킵)
    python3 -m rag.sync_audit_cases --source <path># 원본 cases.jsonl 경로 지정

크론 등록(분기별)은 별도 스크립트: rag/install_audit_scheduler.sh (--print 로 미리보기).
"""
import argparse
import functools
import json
import re
from pathlib import Path

from rag import common as C

# "제1039호" / "제 1027 호" — 일반기업 '제N장'은 대상 아님(호수 체계가 아님)
RE_STD_NO = re.compile(r"제\s*(\d{3,4})\s*호")

# audit-sentinel 원본(65건). 필드: case_id/title/facts/violation/basis/audit_gap/implication/
#   standard/source_url/standard_superseded/fiscal_year (+ 미사용 issue_area/decision_year 등).
DEFAULT_SOURCE = Path("/Users/gyuyeong/projects/audit-sentinel/src/data/cases.jsonl")
# KASB 변환 산출 경로 = AUDIT_COLLECTIONS["audit_cases"] 파일명과 일치해야 함.
DEST = C.PARSED / C.AUDIT_COLLECTIONS["audit_cases"][0]

# KASB audit_case 레코드로 옮길 필드(임베딩 본문 3종 + 표시용 메타). 나머지 원본 필드는 버림.
CARRY_FIELDS = ("case_id", "title", "facts", "violation", "basis",
                "audit_gap", "implication", "standard", "source_url",
                "standard_superseded", "fiscal_year")


def extract_standard_nos(text):
    """문자열에서 언급된 기준서 호수를 등장 순서대로(중복 제거) 추출. 예: ['1039','1109']."""
    out = []
    for m in RE_STD_NO.finditer(text or ""):
        if m.group(1) not in out:
            out.append(m.group(1))
    return out


@functools.lru_cache(maxsize=4)
def current_standard_nos(path=None):
    """현행 K-IFRS 기준서 호수 집합. 기본 소스 = data/parsed/3001.jsonl 의 standard_no.

    23,811건을 케이스마다 다시 읽지 않도록 경로별로 캐시한다(테스트는 cache_clear 로 초기화).
    """
    p = Path(path) if path else C.PARSED / "3001.jsonl"
    if not p.exists():
        return frozenset()
    nos = set()
    with p.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                no = json.loads(line).get("standard_no")
                if no:
                    nos.add(str(no))
    return frozenset(nos)


def is_superseded(standard_text, path=None):
    """감리사례가 인용한 기준서가 현행이 아닌지 판정.

    audit-sentinel이 넘겨준 standard_superseded(수작업 목록 기반, 최신성 없음) 대신
    이 프로젝트가 실제로 보유한 3001.jsonl 목록과 대조해 매 동기화마다 재계산한다.
    호수를 하나도 못 뽑으면(제N장 체계·참조 없음) 판단 근거가 없으므로 경고하지 않는다.
    """
    text = standard_text or ""
    if "舊" in text:
        return True
    nos = extract_standard_nos(text)
    current = current_standard_nos(path)
    return any(no not in current for no in nos)


def convert(rec, std_path=None):
    """audit-sentinel 레코드 → KASB record_type='audit_case' 레코드.

    standard_superseded 는 원본 값을 그대로 옮기지 않고 현행 기준서 목록과 대조해 덮어쓴다.
    """
    out = {"record_type": "audit_case"}
    for f in CARRY_FIELDS:
        if f in rec:
            out[f] = rec[f]
    out["standard_superseded"] = is_superseded(rec.get("standard"), std_path)
    return out


def sync_jsonl(source: Path, std_path=None) -> int:
    """source cases.jsonl 을 읽어 DEST 에 audit_case JSONL 로 변환·기록. 반환: 건수."""
    if not source.exists():
        raise SystemExit(f"원본을 찾을 수 없습니다: {source}")
    records = [convert(json.loads(line), std_path)
               for line in source.open(encoding="utf-8") if line.strip()]
    DEST.parent.mkdir(parents=True, exist_ok=True)
    with DEST.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"변환 완료: {source} → {DEST} ({len(records)}건)", flush=True)
    return len(records)


def reembed():
    """audit_cases 컬렉션을 비우고(clean rebuild) AUDIT_COLLECTIONS만 재임베딩."""
    from rag import embed
    client = C.get_chroma()
    for name in C.AUDIT_COLLECTIONS:
        try:
            client.delete_collection(name)   # 내용 변경분 확실 반영 위해 기존 컬렉션 제거
            print(f"기존 컬렉션 제거: {name}", flush=True)
        except Exception:                    # 없으면 무시(최초 실행)
            pass
    embed.run(C.AUDIT_COLLECTIONS)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", type=Path, default=DEFAULT_SOURCE,
                    help=f"audit-sentinel cases.jsonl 경로 (기본: {DEFAULT_SOURCE})")
    ap.add_argument("--no-embed", action="store_true", help="JSONL 변환만, 임베딩 스킵")
    args = ap.parse_args()
    n = sync_jsonl(args.source)
    if args.no_embed:
        print("임베딩 스킵(--no-embed). 적재는 `python3 -m rag.sync_audit_cases`로.", flush=True)
        return
    reembed()
    print(f"동기화 완료: audit_cases {n}건 재임베딩.", flush=True)


if __name__ == "__main__":
    main()
