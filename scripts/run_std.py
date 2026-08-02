# -*- coding: utf-8 -*-
"""기준서 게시판 전체 수집 드라이버 (warning 로그 캡처 포함).

사용법: python3 -u -m scripts.run_std 3001
- crawl_standards로 잔여 전체 수집 (state/ 재개, raw/ 보관)
- 문단 체계 warning(kasb.parsers)을 data/std_warnings_<board>.log + stdout에 기록
"""
import logging
import sys

from crawl.crawler import DATA
from crawl.standards_crawler import STD_BOARDS, crawl_standards

board = sys.argv[1] if len(sys.argv) > 1 else "3001"

logger = logging.getLogger("kasb.parsers")
logger.setLevel(logging.WARNING)
_fh = logging.FileHandler(DATA / ("std_warnings_" + board + ".log"), encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
logger.addHandler(_fh)
_sh = logging.StreamHandler(sys.stdout)   # 모니터가 [WARN] 라인을 잡도록 stdout
_sh.setFormatter(logging.Formatter("[WARN] %(message)s"))
logger.addHandler(_sh)

print("########## {} ({}) 수집 시작 ##########".format(board, STD_BOARDS[board]["source"]),
      flush=True)
saved, skipped, recollected = crawl_standards(board)
print("\n########## {} 완료: 신규 {}건, 개정 재수집 {}건, 스킵 {}건 ##########".format(
    board, saved, recollected, skipped), flush=True)
