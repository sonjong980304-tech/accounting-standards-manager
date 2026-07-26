# -*- coding: utf-8 -*-
"""split_standard/split_kgaap_chapter의 문단 경계 lookahead 버퍼링 검증.

배경: 두 함수는 줄 단위 상태기계로 문단을 자르는데, "문단 시작"도 "섹션 헤딩"도 아닌
애매한 줄(예: 소제목 "재화의 판매")을 만나면 무조건 현재 열려있는 직전 문단에 붙여왔다.
그런데 실제 기준서 원문에서는 이 소제목이 바로 다음에 오는 새 번호 문단의 도입부인 경우가
있어, 엉뚱하게 이전 문단 끝에 잘못 붙는 버그가 있었다(전수 스캔: kgaap 2001건 중 67건,
kifrs 23811건 중 391건 확인). 아래 3케이스는 실제 데이터베이스에서 확인된 재현 사례다.

kifrs 케이스는 문단 번호를 1부터 순서대로 둔다 — split_standard의 순서 게이트(새 계열은
반드시 1부터 시작, 이후 단조 증가)를 만족시켜야 문단이 정상적으로 열리기 때문이다.
"""
from parsers.standard_split import split_kgaap_chapter, split_standard


def _by_para(records, para_no):
    return next(r for r in records if r["para_no"] == para_no)


def test_kgaap_orphan_heading_attaches_to_next_paragraph_not_previous():
    """제16장 실제 사례: "재화의 판매" 소제목이 16.9가 아니라 16.10에 붙어야 한다."""
    text = (
        "16.9 한 거래에서 판매자가 재화와 용역을 함께 제공하는 경우 다음과 같다.\n"
        "⑴ 재화판매거래로 분류한다.\n"
        "재화의 판매\n"
        "16.10 재화의 판매로 인한 수익은 다음 조건이 모두 충족될 때 인식한다.\n"
        "⑴ 위험과 보상이 이전된다.\n"
    )
    records = split_kgaap_chapter(text, "16")

    para_16_9 = _by_para(records, "16.9")
    assert not para_16_9["text"].rstrip().endswith("재화의 판매")

    para_16_10 = _by_para(records, "16.10")
    assert "재화의 판매로 인한 수익은" in para_16_10["text"]
    assert "재화의 판매" in para_16_10["text"]


def test_kifrs_orphan_heading_attaches_to_next_paragraph_not_previous():
    """제1116호 실제 사례("원가모형"이 29→30 사이에서 새는 것)를 1→2로 축소 재현."""
    text = (
        "1 리스이용자는 사용권자산에 원가모형을 선택할 수 있다.\n"
        "원가모형\n"
        "2 원가모형을 적용하는 경우 사용권자산을 원가에서 감가상각누계액을 차감한 금액으로 측정한다.\n"
    )
    records = split_standard(text, "1116")

    para_1 = _by_para(records, "1")
    assert not para_1["text"].rstrip().endswith("원가모형")

    para_2 = _by_para(records, "2")
    assert para_2["text"].startswith("원가모형")
    assert "원가모형을 적용하는 경우" in para_2["text"]


def test_kifrs_orphan_heading_after_subitem_attaches_to_next_paragraph():
    """제1116호 실제 사례(하위항목 50⑶ 뒤 "공시"가 51로 안 붙던 것)를 1→2로 축소 재현."""
    text = (
        "1 리스이용자는 다음을 공시한다.\n"
        "⑶ 사용권자산의 감가상각비\n"
        "공시\n"
        "2 공시의 목적은 재무제표이용자가 리스활동의 영향을 평가할 수 있는 정보를 제공하는 것이다.\n"
    )
    records = split_standard(text, "1116")

    para_1 = _by_para(records, "1")
    assert not para_1["text"].rstrip().endswith("공시")

    para_2 = _by_para(records, "2")
    assert para_2["text"].startswith("공시")
    assert "공시의 목적은" in para_2["text"]


def test_kifrs_genuine_multiline_body_still_merges_into_current_paragraph():
    """회귀: 진짜 여러 줄에 걸친 본문 연속(중간 줄이 문장종결형이 아니어도, 다음 줄이
    또 다른 연속 본문이면)은 그대로 이전 문단에 이어붙어야 한다.
    """
    text = (
        "1 리스이용자는 다음 사항을 인식한다.\n"
        "이는 사용권자산과 리스부채를 포함하며\n"
        "리스제공자가 기초자산의 통제를 이전하지 않는 계약은 제외한다.\n"
        "2 이 기준서는 모든 리스에 적용한다.\n"
    )
    records = split_standard(text, "1116")

    para_1 = _by_para(records, "1")
    assert "이는 사용권자산과 리스부채를 포함하며" in para_1["text"]
    assert "리스제공자가 기초자산의 통제를 이전하지 않는 계약은 제외한다" in para_1["text"]
    para_2 = _by_para(records, "2")
    assert para_2["text"].startswith("이 기준서는 모든 리스에 적용한다")


def test_kifrs_long_non_terminal_line_immediately_before_new_paragraph_stays_put():
    """안전장치 회귀: 문장종결형이 아니어도 충분히 긴 줄(소제목이라기엔 너무 김)은,
    바로 다음이 새 문단이더라도 orphan heading으로 오판해 옮겨붙이면 안 된다.
    """
    long_line = "이 조건은 상당히 길게 서술되어 있어 소제목으로 보기 어려운 일반 본문 연속"
    assert len(long_line) > 30
    text = "1 어떤 조건을 설명한다.\n{}\n2 다음 문단이다.\n".format(long_line)
    records = split_standard(text, "1116")

    para_1 = _by_para(records, "1")
    assert long_line in para_1["text"]
    para_2 = _by_para(records, "2")
    assert long_line not in para_2["text"]
    assert para_2["text"] == "다음 문단이다."


def test_kgaap_multi_line_orphan_heading_chain_attaches_to_next_paragraph():
    """실제 제2장 사례: 소제목이 한 줄이 아니라 "재무상태표"(빈 줄)"재무상태표의 목적"처럼
    여러 줄에 걸쳐 이어진 뒤에야 새 문단(2.17)이 시작된다. 체인 전체가 2.16이 아니라
    2.17에 붙어야 한다.
    """
    text = (
        "2.16 다음의 사항을 각 재무제표의 명칭과 함께 기재한다.\n"
        "⑶ 보고통화 및 금액단위\n"
        "재무상태표\n"
        "\n"
        "재무상태표의 목적\n"
        "\n"
        "2.17 재무상태표는 일정시점의 재무상태에 관한 정보를 제공한다.\n"
    )
    records = split_kgaap_chapter(text, "2")

    para_2_16_3 = _by_para(records, "2.16⑶")
    assert "재무상태표" not in para_2_16_3["text"]

    para_2_17 = _by_para(records, "2.17")
    assert para_2_17["text"].startswith("재무상태표\n재무상태표의 목적")
    assert "재무상태표는 일정시점의" in para_2_17["text"]


def test_kifrs_subitem_list_still_parsed_after_orphan_heading_fix():
    """회귀: 하위항목 ⑴⑵ 목록 파싱은 그대로 동작해야 한다."""
    text = (
        "1 다음 조건을 모두 충족해야 한다.\n"
        "⑴ 첫 번째 조건이다.\n"
        "⑵ 두 번째 조건이다.\n"
        "2 다음 문단이다.\n"
    )
    records = split_standard(text, "1116")

    para_1 = _by_para(records, "1")
    assert "⑴ 첫 번째 조건이다." in para_1["text"]
    assert "⑵ 두 번째 조건이다." in para_1["text"]
    assert _by_para(records, "1⑴")["text"] == "첫 번째 조건이다."
    assert _by_para(records, "1⑵")["text"] == "두 번째 조건이다."


def test_kifrs_section_heading_still_closes_current_paragraph():
    """회귀: RE_SECTION에 매치하는 정식 헤딩("부록A" 등)은 여전히 현재 문단을 닫아야
    한다 — orphan heading과 혼동해 다음 숫자문단에 붙이면 안 된다.
    """
    text = (
        "1 다음과 같다.\n"
        "부록A 용어의 정의\n"
        "2 다음 문단이다.\n"
    )
    records = split_standard(text, "1116")

    para_1 = _by_para(records, "1")
    assert "부록A" not in para_1["text"]
    para_2 = _by_para(records, "2")
    assert "부록A" not in para_2["text"]
    assert para_2["text"] == "다음 문단이다."


def test_kifrs_glued_paragraph_start_still_recognized():
    """회귀: 번호와 본문이 붙어 나오는 개정 삽입 문단("1A실무적...")은 여전히
    새 문단으로 인식돼야 한다.
    """
    text = (
        "1 원래 문단이다.\n"
        "1A실무적 간편법으로 다음을 적용할 수 있다.\n"
    )
    records = split_standard(text, "1116")

    para_1a = _by_para(records, "1A")
    assert para_1a["text"].startswith("실무적 간편법으로")


def test_kgaap_orphan_heading_at_end_of_text_falls_back_to_current_paragraph():
    """엣지케이스: 애매한 줄 뒤에 더 이상 줄이 없으면(파일 끝) 새 문단이 열리지
    않으므로 현재 문단에 이어붙여야 한다(유실 방지).
    """
    text = (
        "16.9 한 거래에서 판매자가 재화와 용역을 함께 제공하는 경우 다음과 같다.\n"
        "결론적 소결\n"
    )
    records = split_kgaap_chapter(text, "16")

    para_16_9 = _by_para(records, "16.9")
    assert "결론적 소결" in para_16_9["text"]
