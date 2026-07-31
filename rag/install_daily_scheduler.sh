#!/usr/bin/env bash
# KASB 매일 자동 갱신 스케줄러 설치기 (질의회신+기준서 증분 크롤링 → 임베딩, 매일 06:00).
#
# rag.daily_update (crawler.py/standards_crawler.py 증분 크롤링 → rag.embed 증분 임베딩) 를
# cron 에 등록. 감리지적사례(kasb-audit-sync, 분기별 04:00)와는 별개 스케줄 — 서로 건드리지 않음.
#
# 사용:
#   install_daily_scheduler.sh            # (기본) 설치될 crontab 내용만 출력 — 시스템 변경 없음
#   install_daily_scheduler.sh --print    # 위와 동일
#   install_daily_scheduler.sh --install  # 실제 crontab 등록(멱등: 기존 kasb-daily 항목 교체)
#   install_daily_scheduler.sh --uninstall# kasb-daily 항목만 제거
#
# ⚠️ 실제 crontab 변경(--install/--uninstall)은 시스템 전역 상태를 바꾼다. 미리보기는 --print.
# 파이썬은 기본적으로 이 프로젝트 전용 venv(.venv) 사용 — cron은 PATH가 최소라 시스템
# python3로 잘못 걸리면 chromadb 버전 충돌(공유 venv 이슈 재발) 위험. PYTHON 환경변수로 덮어쓸 수 있음.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="$(cd "$SCRIPT_DIR/.." && pwd)"          # rag/ 의 부모 = kasb-crawler 루트
PYTHON="${PYTHON:-$PROJECT/.venv/bin/python}"
LOG="$PROJECT/data/daily_update.log"
TAG="# kasb-daily-update scheduler"

if [ ! -x "$PYTHON" ]; then
  echo "오류: $PYTHON 을 찾을 수 없습니다. PYTHON=<경로> 로 지정하세요." >&2
  exit 1
fi

# cron 라인 (TAG 주석으로 멱등 교체·제거 대상 식별). 매일 06:00.
# PATH에 pyenv shims 추가: hwp5html/hwp5txt(pyhwp)가 거기 있는데 cron 기본 PATH엔
# 없어서(shutil.which 탐색 실패) 새 기준서 HWP를 못 찾아 PDF 폴백만 타는 걸 방지.
CRON_PATH="$HOME/.pyenv/shims:/usr/local/bin:/usr/bin:/bin"
SCHEDULED="0 6 * * * cd $PROJECT && PATH=$CRON_PATH $PYTHON -m rag.daily_update >> $LOG 2>&1 $TAG"

print_block() {
  echo "# ── kasb 매일 증분 갱신 스케줄 (매일 06:00, 질의회신+기준서 → 임베딩) ──"
  echo "$SCHEDULED"
}

case "${1:---print}" in
  --print)
    echo "다음 1줄이 crontab 에 등록됩니다 (실제 등록: --install):"
    echo
    print_block
    echo
    echo "파이썬: $PYTHON"
    echo "프로젝트: $PROJECT"
    echo "로그:   $LOG"
    echo
    echo "기존 kasb-audit-sync(분기별 04:00) 항목은 건드리지 않습니다."
    ;;
  --install)
    # 기존 kasb-daily 항목은 지우고(멱등) 새로 추가. kasb-audit-sync 등 다른 항목은 보존.
    { crontab -l 2>/dev/null | grep -vF "$TAG" || true; echo "$SCHEDULED"; } | crontab -
    echo "설치 완료. 현재 kasb-daily 항목:"
    crontab -l 2>/dev/null | grep -F "$TAG" || true
    ;;
  --uninstall)
    { crontab -l 2>/dev/null | grep -vF "$TAG" || true; } | crontab -
    echo "kasb-daily 스케줄 항목을 제거했습니다."
    ;;
  *)
    echo "알 수 없는 옵션: $1" >&2
    echo "사용: install_daily_scheduler.sh [--print|--install|--uninstall]" >&2
    exit 2
    ;;
esac
