# -*- coding: utf-8 -*-
"""기준서 텍스트 → 문단 단위 분리 + ref_key 부여.

통짜 저장 금지: 문단 번호(7, 76A, B8, BC40, …) 기준으로 분리하고,
문단 안의 하위항목 ⑴~⑿ 은 개별 레코드(예: "제1116호 문단 7⑴")로도 저장한다.
ref_key 는 반드시 refs.make_ref_key() 로 생성 → 질의회신 standard_refs 와
완전일치 조인 보장. 텍스트는 refs.normalize_ref() 로 정규화 후 처리한다.

문단 시작 판정(노이즈 필터 2중 게이트):
  1) 한글 게이트: 번호 뒤 60자 안에 한글이 있거나 '['로 시작
     (저작권부 영문 주소 "7 Westferry Circus…" 등 배제)
  2) 순서 게이트: 계열(prefix)별로 번호가 오름차순이어야 하고
     새 계열은 반드시 1부터 시작 (본문 1…, B1…, C1…, IE1…, BC1…, DO1…, IN1…)
"""
import logging
import re

from refs import (
    make_kgaap_ref_key,
    make_ref_key,
    make_section_key,
    make_term_key,
    normalize_ref,
)

logger = logging.getLogger("kasb.parsers")

# 문단 시작 (공백형): 번호 + 공백 + 내용
# (예: "7 리스이용자가…", "BC40 …", 소수점 체계 "4.1.2A …", "B4.1.7 …")
RE_PARA_START = re.compile(
    r"^([A-Z]{1,2})?(\d{1,3}(?:\.\d{1,3})*)([A-Z]{0,2})\s+(\S.*)$")
# 문단 시작 (밀착형): 개정 삽입 문단은 번호와 본문이 붙어 나옴
# (예: "46A실무적 간편법으로…", "104리스이용자는…", "C20BA리스이용자는…")
RE_PARA_START_GLUED = re.compile(
    r"^([A-Z]{1,2})?(\d{1,3}(?:\.\d{1,3})*)([A-Z]{0,2})([가-힣].*)$")
# 밀착형 오탐 가드: "12개월…", "3년간…" 같은 수량 표현의 첫 글자
COUNTER_CHARS = set("개년월일원명번회차억만천퍼")
# 하위항목: ⑴~⒇ 로 시작하는 라인 (refs.PARA_PATTERN과 동일 범위 —
# 제1001호 문단 54가 ⒅까지 실사용해 ⑿→⒇로 확장됨, 2026-07-02)
RE_SUBITEM = re.compile(r"^([⑴-⒇])\s*(.*)$")
# 섹션 헤딩: 현재 문단을 닫는다 (부록A 용어정의 등 무번호 구간이 직전 문단에 붙는 것 방지)
RE_SECTION = re.compile(
    r"^(부록\s*[A-Z]|결론도출근거|적용사례|용어의 정의|한국채택국제회계기준|개정\s)"
)
RE_HANGUL = re.compile(r"[가-힣]")

# 소제목/헤딩처럼 보이는 애매한 줄 판정 (문단 경계 오귀속 방지, 2026-07-26)
# 실제 조문 본문은 거의 항상 문장종결형(다/음/함/임/됨/까)으로 끝나는데, 소제목은
# 명사구라 이런 종결형이 없다("재화의 판매", "원가모형", "공시" 등). 길이도 짧아야만
# 후보로 본다 — 길고 우연히 종결형이 없는 본문 줄까지 오탐하지 않기 위한 안전장치.
_SENTENCE_END_RE = re.compile(r"(다|음|함|임|됨|까)[.)]?$")
MAX_ORPHAN_HEADING_LEN = 30


def _looks_like_orphan_heading(line):
    """직전 문단 대신 다음 문단에 속했어야 할 소제목처럼 보이는 짧은 줄인지 판정."""
    return len(line) <= MAX_ORPHAN_HEADING_LEN and not _SENTENCE_END_RE.search(line)


MAX_NUM_JUMP = 50  # 계열 내 번호 점프 허용치 (삭제 문단 감안, 연도 등 오탐 차단)
# 주: 150으로 올리면 제1039호(IAS39) 등 삭제-갭 문단을 잡지만 IG/예시 번호를
# 문단으로 오인해 오탐 폭증(제1039호 +507). 삭제-갭 복구는 섹션인식 기반의
# 별도 targeted fix 필요 — 전역 cap 상향은 금지.

# 부록A 시작 헤딩 (예: "부록 A. 용어의 정의")
RE_APPENDIX_A = re.compile(r"^부록\s*A[.\s]*용어의 정의$")


def split_kgaap_chapter(text, chapter):
    """일반기업회계기준 장(章) 텍스트 → 문단 레코드 (예: "31.9 내용…").

    ref_key는 refs.make_kgaap_ref_key → 질의회신의 "제31장 문단 31.9"
    참조와 완전일치 조인. 하위항목 ⑴~⒇은 개별 레코드로도 저장.
    """
    text = normalize_ref(text)
    ch = str(int(chapter))
    re_para = re.compile(r"^(" + ch + r"\.\d+[A-Z]*)\s+(\S.*)$")
    records, cur = [], None
    section = ""
    pending = []  # "다음 문단 도입부일 수 있는" 보류 중인 애매한 줄들 (lookahead 대상,
                  # 소제목이 여러 줄에 걸치는 경우가 있어 누적한다 — 예: "재무상태표"
                  # 다음 줄에 "재무상태표의 목적"이 또 이어지고서야 새 문단이 시작됨)

    def close():
        if cur is None:
            return
        parts = [cur["pre"].strip()]
        parts += ["{} {}".format(m, t.strip()) for m, t in cur["subs"]]
        records.append({
            "ref_key": make_kgaap_ref_key(ch, para=cur["para_no"]),
            "para_no": cur["para_no"],
            "series": "본문",
            "section": cur["section"],
            "text": "\n".join(p for p in parts if p),
        })
        for mark, sub_text in cur["subs"]:
            records.append({
                "ref_key": make_kgaap_ref_key(ch, para=cur["para_no"] + mark),
                "para_no": cur["para_no"] + mark,
                "series": "본문",
                "section": cur["section"],
                "text": sub_text.strip(),
            })

    def _is_new_para(line):
        m = re_para.match(line)
        return m if (m and not m.group(2).lstrip().startswith("|")) else None

    def flush_pending():
        if not pending:
            return
        text = "\n".join(pending)
        pending.clear()
        if cur is not None:
            if cur["subs"]:
                cur["subs"][-1][1] += "\n" + text
            else:
                cur["pre"] += "\n" + text

    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line:
            continue

        is_section = RE_SECTION.match(line) and len(line) < 40
        m = None if is_section else _is_new_para(line)

        # 보류된 줄들이 있는데 이번 줄이 새 문단 시작이 아니면: 그 줄들이 또 다른
        # 소제목 후보(체인 계속)일 수도, 진짜 본문일 수도 있다. 아래에서 "소제목
        # 후보인가" 판정으로 계속 쌓을지/흘려보낼지 갈린다 — 여기서는 새 문단 시작인
        # 경우만 먼저 처리(체인 전체를 다음 문단 선두로 넘김).
        if is_section:
            flush_pending()
            close()
            cur = None
            section = line
            continue

        if m:
            close()
            lead = "\n".join(pending) if pending else None
            pending.clear()
            cur = {"para_no": m.group(1), "section": section,
                   "pre": (lead + "\n" + m.group(2)) if lead else m.group(2), "subs": []}
            continue

        sm = RE_SUBITEM.match(line)
        if sm and cur is not None:
            flush_pending()  # 하위항목 시작 → 보류분은 체인이 아니라 진짜 본문이었다
            mark = sm.group(1)
            if cur["subs"] and ord(mark) <= ord(cur["subs"][-1][0]):
                cur["subs"][-1][1] += "\n" + line  # 목록 재시작 → 연속 텍스트
            else:
                cur["subs"].append([mark, sm.group(2)])
            continue

        if cur is not None and _looks_like_orphan_heading(line):
            pending.append(line)  # 소제목 체인일 수 있으므로 누적(즉시 커밋 안 함)
            continue

        if cur is not None:
            flush_pending()  # 문장종결형 연속 본문 → 보류분은 진짜 본문이었다
            if cur["subs"]:
                cur["subs"][-1][1] += "\n" + line
            else:
                cur["pre"] += "\n" + line
    flush_pending()
    close()
    return records


RE_DEFLIST_START = re.compile(r"용어의 (?:정의|뜻)[^\n]{0,15}다음")


def _valid_term(term, definition):
    """용어/정의 유효성 필터 (부록A 표·콜론 리스트 공용, 3001 오추출 방지)."""
    if not term or not definition:
        return False
    if term in ("용어", "정의", "계", "합계", "소계", "구분", "금액") or len(term) < 2:
        return False
    if len(term) > 30 or not re.search(r"[가-힣]", term):
        return False
    if not re.search(r"[가-힣A-Za-z]", definition):   # 정의가 숫자·기호뿐 = 예시표 셀
        return False
    if re.match(r"^\d{4}\.\s*\d{1,2}\.", term):         # 개정이력 행
        return False
    return True


def extract_colon_terms(text, std_no, src_file=None):
    """부록A 표가 없고 정의가 본문 콜론 리스트로 있는 기준서용 용어 추출.

    예: 제1012호 "5 …용어의 정의는 다음과 같다." → "회계이익: 법인세비용 차감 전 …"
    ⑴⑵/㈎㈏ 하위목록이 딸린 복합 정의는 직전 용어 정의에 이어붙인다.
    다음 번호 문단("12 …")이 나오면 리스트 종료.
    """
    text = normalize_ref(text)
    lines = text.split("\n")
    start = next((i for i, l in enumerate(lines)
                  if RE_DEFLIST_START.search(l.strip())), None)
    if start is None:
        return []
    records, seen, cur = [], set(), None

    def flush():
        if not cur:
            return
        term, deflines = cur[0], " ".join(cur[1]).strip()
        if _valid_term(term, deflines):
            key = make_term_key(std_no, term)
            if key not in seen:
                seen.add(key)
                records.append({
                    "ref_key": key, "section_key": make_section_key(std_no),
                    "term": term, "text": deflines,
                    "standard": "제{}호".format(std_no),
                    "page_no": None, "src_file": src_file,
                })

    for l in lines[start + 1:]:
        s = l.strip()
        if not s:
            continue
        if re.match(r"^\d+\s+[가-힣]", s):    # 다음 번호 문단 → 리스트 끝
            break
        m = re.match(r"^([^:：]{1,30})[:：]\s*(.+)$", s)
        if m and re.search(r"[가-힣]", m.group(1)):
            flush()
            cur = [m.group(1).strip(), [m.group(2).strip()]]
        elif cur:
            cur[1].append(s)               # 연속(⑴⑵ 등)
    flush()
    return records


def extract_term_records(text, std_no, src_file=None):
    """부록A '용어의 정의' 표에서 용어 하나당 레코드 하나 추출 (2단 키).

    질의회신이 "(제1109호 용어의 정의)"처럼 용어명 없이 섹션 수준으로
    인용하므로 section_key로 거칠게 조인하고 term 매칭으로 좁힌다.
    page_no: HWP→XHTML 흐름에는 페이지 정보가 없어 None
    (PDF 폴백 파싱을 쓰는 경우에만 채울 수 있음).
    """
    text = normalize_ref(text)
    lines = text.split("\n")

    def region_after(i0):
        """i0 다음부터 다음 섹션 헤딩 전까지의 (start, end)."""
        for j in range(i0 + 1, len(lines)):
            if RE_SECTION.match(lines[j].strip()) and len(lines[j].strip()) < 40:
                return i0 + 1, j
        return i0 + 1, len(lines)

    # 부록A 후보 중 '목차 항목'(다음 섹션이 코앞) 건너뛰고 실제 내용 있는 것 선택
    start = end = None
    for i, ln in enumerate(lines):
        s = ln.strip()
        if RE_APPENDIX_A.match(s) or s == "용어의 정의":
            st, en = region_after(i)
            content = [l for l in lines[st:en] if l.strip()]
            if len(content) >= 3:          # 목차(코앞에 부록B)면 content<3 → 스킵
                start, end = st, en
                break
    if start is None:
        # 부록A 표가 없으면 본문 콜론 정의 리스트로 폴백 (제1012·1007·1032호 등)
        return extract_colon_terms(text, std_no, src_file=src_file)

    records, seen = [], set()
    for ln in lines[start:end]:
        if " | " not in ln:
            continue  # 용어 표가 아닌 안내문 등
        cells = [c.strip() for c in ln.split(" | ")]
        term, definition = cells[0], " ".join(c for c in cells[1:] if c).strip()
        if not _valid_term(term, definition):
            continue
        key = make_term_key(std_no, term)
        if key in seen:
            continue
        seen.add(key)
        records.append({
            "ref_key": key,
            "section_key": make_section_key(std_no),
            "term": term,
            "text": definition,
            "standard": "제{}호".format(std_no),
            "page_no": None,
            "src_file": src_file,
        })
    # 부록A 표를 찾았으나 유효 용어가 0이면(예시표만 있던 경우) 콜론 폴백
    if not records:
        return extract_colon_terms(text, std_no, src_file=src_file)
    return records


def _suffix_key(suffix):
    return suffix or ""


def _match_para_start(line, last_in_series, seen_in_series):
    """줄이 유효한 새 문단 시작인지 판정만 하고 상태(last_in_series/seen_in_series)는
    바꾸지 않는다(lookahead에서 안전하게 미리보기용으로 재사용하기 위함).

    반환: (prefix, num, num_s, suffix, rest) 또는 유효하지 않으면 None.
    """
    m = RE_PARA_START.match(line)
    if not m:
        gm = RE_PARA_START_GLUED.match(line)
        if gm and gm.group(4)[0] not in COUNTER_CHARS:
            m = gm
    if not m:
        return None
    prefix, num_s, suffix, rest = m.group(1) or "", m.group(2), m.group(3), m.group(4)
    num = tuple(int(x) for x in num_s.split("."))
    hangul_ok = bool(RE_HANGUL.search(rest[:60])) or rest.startswith("[")
    if rest.lstrip().startswith("|"):
        hangul_ok = False
    if not hangul_ok:
        return None
    last = last_in_series.get(prefix)
    seen = seen_in_series.get(prefix, set())
    if last is None:
        lead_max = 1 if not prefix else 5
        order_ok = (num[0] <= lead_max and not suffix)
    else:
        order_ok = (
            (num > last and num[0] - last[0] <= MAX_NUM_JUMP)
            or (num == last and (num, _suffix_key(suffix)) not in seen)
        )
    if not order_ok:
        return None
    return prefix, num, num_s, suffix, rest


def split_standard(text, std_no):
    """기준서 전문 텍스트를 문단 레코드 목록으로 분리.

    반환: [{"ref_key", "para_no", "series", "section", "text"}, ...]
      - 문단 레코드: text = 서문 + 하위항목 전부 (자체 완결 문맥)
      - 하위항목 레코드: ⑴~⑿ 각각 개별 (ref_key 예: "제1116호 문단 7⑴")
    """
    text = normalize_ref(text)
    records = []
    last_in_series = {}   # prefix → num 튜플 (숫자부 최대값)
    seen_in_series = {}   # prefix → {(num, suffix)} — 동일 번호 재등장 차단
    section = ""
    cur = None            # 진행 중 문단: {"para_no","series","section","pre","subs"}
    pending = []          # "다음 문단 도입부일 수 있는" 보류 중인 애매한 줄들 (누적 —
                          # 소제목이 여러 줄에 걸치는 경우가 있어 한 줄만으론 부족함)

    def flush_pending():
        if not pending:
            return
        text = "\n".join(pending)
        pending.clear()
        if cur is not None:
            if cur["subs"]:
                cur["subs"][-1][1] += "\n" + text
            else:
                cur["pre"] += "\n" + text

    def close_current():
        if cur is None:
            return
        # 부모 문단 레코드 (서문 + 하위항목 포함 전문)
        parts = [cur["pre"].strip()]
        for mark, sub_text in cur["subs"]:
            parts.append("{} {}".format(mark, sub_text.strip()))
        full = "\n".join(p for p in parts if p)
        records.append({
            "ref_key": make_ref_key(std_no, para=cur["para_no"]),
            "para_no": cur["para_no"],
            "series": cur["series"],
            "section": cur["section"],
            "text": full,
        })
        # 하위항목 개별 레코드
        for mark, sub_text in cur["subs"]:
            records.append({
                "ref_key": make_ref_key(std_no, para=cur["para_no"] + mark),
                "para_no": cur["para_no"] + mark,
                "series": cur["series"],
                "section": cur["section"],
                "text": sub_text.strip(),
            })

    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line:
            continue

        is_section = RE_SECTION.match(line) and len(line) < 40
        matched = None if is_section else _match_para_start(line, last_in_series, seen_in_series)

        if is_section:
            flush_pending()
            close_current()
            cur = None
            section = line
            continue

        if matched:
            prefix, num, num_s, suffix, rest = matched
            close_current()
            last = last_in_series.get(prefix)
            last_in_series[prefix] = max(last, num) if last else num
            seen_in_series.setdefault(prefix, set()).add((num, _suffix_key(suffix)))
            lead = "\n".join(pending) if pending else None
            pending.clear()
            cur = {
                "para_no": prefix + num_s + suffix,
                "series": prefix or "본문",
                "section": section,
                "pre": (lead + "\n" + rest) if lead else rest,
                "subs": [],
            }
            continue

        sm = RE_SUBITEM.match(line)
        if sm and cur is not None:
            flush_pending()  # 하위항목 시작 → 보류분은 체인이 아니라 진짜 본문이었다
            mark = sm.group(1)
            if cur["subs"] and ord(mark) <= ord(cur["subs"][-1][0]):
                # 같은 문단 안에서 ⑴⑵… 목록이 재시작 (예: 제1001호 문단 7
                # '용어의 정의'의 용어별 하위목록) → ref_key 중복 방지를 위해
                # 첫 목록만 개별 레코드로 하고 이후 목록은 연속 텍스트로 취급
                cur["subs"][-1][1] += "\n" + line
            else:
                cur["subs"].append([mark, sm.group(2)])
            continue

        if cur is not None and _looks_like_orphan_heading(line):
            pending.append(line)  # 소제목 체인일 수 있으므로 누적(즉시 커밋 안 함)
            continue

        # 연속 라인: 열린 하위항목 > 열린 문단 순으로 덧붙임. 문단 밖(표지 등)은 버림
        if cur is not None:
            flush_pending()  # 문장종결형 연속 본문 → 보류분은 진짜 본문이었다
            if cur["subs"]:
                cur["subs"][-1][1] += "\n" + line
            else:
                cur["pre"] += "\n" + line

    flush_pending()
    close_current()
    return records
