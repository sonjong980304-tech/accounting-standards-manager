# -*- coding: utf-8 -*-
"""기준서 개정 자동 감지·재수집 단위 테스트 (작업 B).

KASB 목록의 '구분' 칼럼은 전부 '시행 중'이라 개정 신호가 안 되지만, 첨부 파일명에는
개정연도·수정목록 번호가 들어 있다(…제1101호_…(2024_개정_…_26-1_…).hwp). 그래서 seq 별
파일명을 state 에 적어 두고 다음 실행에서 비교해 개정을 감지한다.

네트워크는 쓰지 않는다(KasbClient 를 monkeypatch). 실제 data/ 는 건드리지 않는다(tmp_path).
실행: python3 -m pytest tests/test_std_recollect.py -q
"""
import json

import pytest

from crawl import standards_crawler as SC


@pytest.fixture
def store(tmp_path, monkeypatch):
    """DATA 를 tmp_path 로 돌린 StdStore(3001)."""
    monkeypatch.setattr(SC, "DATA", tmp_path)
    return SC.StdStore("3001")


# ------------------------------------------------- state 스키마 + 하위호환
def test_state_is_dict_keyed_by_seq(store):
    store.mark_collected("2974", ["a.hwp", "a.pdf"])
    reloaded = json.loads(store.state_path.read_text(encoding="utf-8"))
    assert isinstance(reloaded["collected"], dict)
    assert reloaded["collected"]["2974"]["file_names"] == ["a.hwp", "a.pdf"]
    assert reloaded["collected"]["2974"]["collected_at"]


def test_legacy_list_state_loads_as_dict(tmp_path, monkeypatch):
    monkeypatch.setattr(SC, "DATA", tmp_path)
    sp = tmp_path / "state" / "3001.json"
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps({"collected": ["1", "10", "2974"],
                              "updated_at": "2026-07-03T00:00:00+09:00"}),
                  encoding="utf-8")
    st = SC.StdStore("3001")
    assert set(st.state["collected"]) == {"1", "10", "2974"}
    assert st.state["collected"]["2974"] == {"file_names": [], "collected_at": None}
    assert st.is_collected("2974") and not st.is_collected("99")


def test_legacy_state_does_not_trigger_recollect(tmp_path, monkeypatch):
    """파일명을 모르는 기존 state 는 첫 실행에서 재수집을 유발하면 안 된다."""
    monkeypatch.setattr(SC, "DATA", tmp_path)
    sp = tmp_path / "state" / "3001.json"
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps({"collected": ["2974"], "updated_at": None}),
                  encoding="utf-8")
    st = SC.StdStore("3001")
    assert st.needs_recollect("2974", ["무엇이든.hwp"]) is False


def test_new_state_roundtrip_survives_reload(store, tmp_path, monkeypatch):
    store.mark_collected("2974", ["x.hwp"])
    st2 = SC.StdStore("3001")
    assert st2.is_collected("2974")
    assert st2.needs_recollect("2974", ["x.hwp"]) is False


# ------------------------------------------------------------ needs_recollect
def test_needs_recollect_false_when_file_names_identical(store):
    store.mark_collected("2974", ["a.hwp", "a.pdf"])
    assert store.needs_recollect("2974", ["a.hwp", "a.pdf"]) is False


def test_needs_recollect_ignores_order(store):
    store.mark_collected("2974", ["a.hwp", "a.pdf"])
    assert store.needs_recollect("2974", ["a.pdf", "a.hwp"]) is False


def test_needs_recollect_true_when_file_name_changed(store):
    store.mark_collected("2974", ["제1101호_(2023_개정).hwp"])
    assert store.needs_recollect("2974", ["제1101호_(2024_개정).hwp"]) is True


def test_needs_recollect_true_when_attachment_added(store):
    store.mark_collected("2974", ["a.hwp"])
    assert store.needs_recollect("2974", ["a.hwp", "a.pdf"]) is True


def test_needs_recollect_false_for_unknown_seq(store):
    assert store.needs_recollect("9999", ["a.hwp"]) is False


def test_needs_recollect_false_when_current_names_empty(store):
    # 목록에서 첨부를 못 읽은 경우(파싱 실패 등)를 개정으로 오인하면 안 됨
    store.mark_collected("2974", ["a.hwp"])
    assert store.needs_recollect("2974", []) is False


# ------------------------------------------------- jsonl 에서 문서 레코드 제거
def _write_jsonl(path, docs):
    with path.open("w", encoding="utf-8") as f:
        for doc_no, n in docs:
            for i in range(n):
                f.write(json.dumps({"doc_no": doc_no, "ref_key": "k%d" % i},
                                   ensure_ascii=False) + "\n")


def _docs_of(path):
    return [json.loads(l)["doc_no"] for l in path.open(encoding="utf-8") if l.strip()]


def test_remove_doc_records_drops_only_target(tmp_path):
    p = tmp_path / "3001.jsonl"
    _write_jsonl(p, [("3001-1", 2), ("3001-2", 3), ("3001-3", 1)])
    removed = SC.remove_doc_records(p, "3001-2")
    assert removed == 3
    assert _docs_of(p) == ["3001-1", "3001-1", "3001-3"]


def test_remove_doc_records_preserves_order_and_content(tmp_path):
    p = tmp_path / "3001.jsonl"
    _write_jsonl(p, [("3001-1", 2), ("3001-2", 1)])
    before = [l for l in p.open(encoding="utf-8") if '"3001-1"' in l]
    SC.remove_doc_records(p, "3001-2")
    assert [l for l in p.open(encoding="utf-8")] == before


def test_remove_doc_records_no_match_is_noop(tmp_path):
    p = tmp_path / "3001.jsonl"
    _write_jsonl(p, [("3001-1", 2)])
    assert SC.remove_doc_records(p, "3001-9") == 0
    assert _docs_of(p) == ["3001-1", "3001-1"]


def test_remove_doc_records_missing_file_is_noop(tmp_path):
    assert SC.remove_doc_records(tmp_path / "없음.jsonl", "3001-1") == 0


def test_remove_doc_records_removing_all_leaves_empty_file(tmp_path):
    p = tmp_path / "3001.jsonl"
    _write_jsonl(p, [("3001-1", 2)])
    assert SC.remove_doc_records(p, "3001-1") == 2
    assert p.exists() and p.read_text(encoding="utf-8") == ""


# --------------------------------------------- crawl_standards 재수집 통합(mock)
LIST_HTML_OLD = """
<table><tr>
  <td><a onclick="fn_Detail('3001','2974')">제1101호 한국채택국제회계기준의 최초채택</a></td>
  <td><a onclick="fileDownload('111','1')">제1101호_(2023_개정).pdf</a>
      <a onclick="fileDownload('111','2')">제1101호_(2023_개정).hwp</a></td>
</tr></table>"""
LIST_HTML_NEW = LIST_HTML_OLD.replace("2023_개정", "2024_개정")


class FakeClient:
    """네트워크 대역. 목록 HTML 은 주입, 첨부는 '현재 개정판' 내용을 담은 더미 바이트."""

    def __init__(self, state):
        self.state = state
        self.downloads = []

    def get_html(self, path, params=None):
        return self.state["html"]

    def download_file(self, file_no, file_seq):
        ext = "pdf" if file_seq == "1" else "hwp"
        name = "제1101호_%s.%s" % (self.state["edition"], ext)
        self.downloads.append(name)
        # 파일 내용 = 그 시점 개정판 본문 (옛 파일을 재사용하면 옛 내용이 나온다)
        return self.state["text"].encode("utf-8"), name


@pytest.fixture
def std_env(tmp_path, monkeypatch):
    """네트워크·문서추출을 대역으로 바꾼 3001 크롤 환경.

    문서 추출은 **실제로 건네받은 파일을 읽는다** — 옛 첨부를 재사용하면 옛 본문이 나오므로
    '개정본을 새로 받아왔는지'가 테스트로 드러난다.
    """
    monkeypatch.setattr(SC, "DATA", tmp_path)
    state = {"html": LIST_HTML_OLD, "edition": "2023_개정",
             "text": "1 옛 문단.\n2 옛 문단 둘.", "parsed_calls": [], "clients": []}

    def make_client(list_path):
        c = FakeClient(state)
        state["clients"].append(c)
        return c

    monkeypatch.setattr(SC, "KasbClient", make_client)
    monkeypatch.setattr(SC, "extract_document_text",
                        lambda hwp, pdf: ((hwp or pdf).read_text(encoding="utf-8"), "hwp"))
    monkeypatch.setattr(SC, "extract_term_records", lambda *a, **k: [])

    def fake_split(text, std_no):
        state["parsed_calls"].append(text)
        return [{"ref_key": "제%s호 문단 %d" % (std_no, i + 1), "para_no": str(i + 1),
                 "text": ln} for i, ln in enumerate(text.splitlines()) if ln.strip()]

    monkeypatch.setattr(SC, "split_standard", fake_split)
    return state


def _records(tmp_path):
    p = tmp_path / "parsed" / "3001.jsonl"
    return [json.loads(l) for l in p.open(encoding="utf-8") if l.strip()]


def test_first_run_collects_and_records_file_names(std_env, tmp_path):
    saved, skipped, recollected = SC.crawl_standards("3001")
    assert (saved, skipped, recollected) == (1, 0, 0)
    st = json.loads((tmp_path / "state" / "3001.json").read_text(encoding="utf-8"))
    assert st["collected"]["2974"]["file_names"] == \
        ["제1101호_(2023_개정).hwp", "제1101호_(2023_개정).pdf"]
    assert len(_records(tmp_path)) == 2


def test_second_run_same_file_names_skips(std_env, tmp_path):
    SC.crawl_standards("3001")
    saved, skipped, recollected = SC.crawl_standards("3001")
    assert (saved, skipped, recollected) == (0, 1, 0)
    assert len(_records(tmp_path)) == 2       # 중복 적재 없음


def test_changed_file_name_triggers_recollect(std_env, tmp_path):
    SC.crawl_standards("3001")
    std_env["html"] = LIST_HTML_NEW
    std_env["edition"] = "2024_개정"           # 개정 → 첨부 파일명 변경
    std_env["text"] = "1 새 문단.\n2 새 문단 둘.\n3 새로 추가된 문단."
    saved, skipped, recollected = SC.crawl_standards("3001")

    assert recollected == 1 and skipped == 0
    recs = _records(tmp_path)
    assert len(recs) == 3                     # 옛 2건 제거 + 새 3건
    assert all(r["doc_no"] == "3001-2974" for r in recs)
    assert "새 문단" in recs[0]["text"]


def test_recollect_downloads_revised_attachment(std_env, tmp_path):
    """재수집 시 개정본을 실제로 새로 내려받아야 한다.

    첨부 캐시 판정이 '확장자별 glob 첫 파일'이라, 옛 HWP 가 남아 있으면 새 개정본을
    받지 않고 옛 파일을 그대로 재파싱해 버린다(= 개정이 반영 안 됨).
    """
    SC.crawl_standards("3001")
    std_env["html"] = LIST_HTML_NEW
    std_env["edition"] = "2024_개정"
    std_env["text"] = "1 새 문단."
    SC.crawl_standards("3001")

    downloaded = std_env["clients"][-1].downloads
    assert "제1101호_2024_개정.hwp" in downloaded     # 개정본을 실제로 요청
    files = {p.name for p in (tmp_path / "raw" / "3001" / "files").iterdir()}
    assert "2974_제1101호_2024_개정.hwp" in files      # 새 이름으로 저장
    assert "2974_제1101호_2023_개정.hwp" in files      # 옛 원본 보존
    assert [r["text"] for r in _records(tmp_path)] == ["1 새 문단."]


def test_recollect_updates_state_file_names(std_env, tmp_path):
    SC.crawl_standards("3001")
    std_env["html"] = LIST_HTML_NEW
    std_env["edition"] = "2024_개정"
    SC.crawl_standards("3001")
    st = json.loads((tmp_path / "state" / "3001.json").read_text(encoding="utf-8"))
    assert st["collected"]["2974"]["file_names"] == \
        ["제1101호_(2024_개정).hwp", "제1101호_(2024_개정).pdf"]


def test_recollect_invalidates_text_cache(std_env, tmp_path):
    SC.crawl_standards("3001")
    std_env["html"] = LIST_HTML_NEW
    std_env["edition"] = "2024_개정"
    std_env["text"] = "1 새 문단."
    std_env["parsed_calls"].clear()
    SC.crawl_standards("3001")
    # 캐시(text/2974.txt)를 지우고 다시 추출해야 개정 내용이 반영됨
    assert std_env["parsed_calls"] == ["1 새 문단."]
    assert (tmp_path / "raw" / "3001" / "text" / "2974.txt").read_text(
        encoding="utf-8") == "1 새 문단."


def test_recollect_keeps_original_attachments(std_env, tmp_path):
    """원본 HWP/PDF 는 재파싱용으로 보존 — 삭제하지 않는다(CLAUDE.md 절대 규칙)."""
    SC.crawl_standards("3001")
    files_dir = tmp_path / "raw" / "3001" / "files"
    before = sorted(p.name for p in files_dir.iterdir())
    std_env["html"] = LIST_HTML_NEW
    std_env["edition"] = "2024_개정"
    _, _, recollected = SC.crawl_standards("3001")
    after = sorted(p.name for p in files_dir.iterdir())
    assert recollected == 1
    assert set(before).issubset(set(after))   # 옛 파일 그대로 남아 있음


def test_recollect_does_not_touch_other_documents(std_env, tmp_path):
    SC.crawl_standards("3001")
    # 다른 기준서 레코드를 뒤에 끼워 넣고, 재수집이 이를 건드리지 않는지 확인
    other = {"doc_no": "3001-9999", "ref_key": "제1116호 문단 7", "text": "타 기준서"}
    with (tmp_path / "parsed" / "3001.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(other, ensure_ascii=False) + "\n")
    std_env["html"] = LIST_HTML_NEW
    std_env["edition"] = "2024_개정"
    std_env["text"] = "1 새 문단."
    _, _, recollected = SC.crawl_standards("3001")
    recs = _records(tmp_path)
    assert recollected == 1
    assert sum(1 for r in recs if r["doc_no"] == "3001-9999") == 1
    assert [r for r in recs if r["doc_no"] == "3001-9999"][0] == other
    assert sum(1 for r in recs if r["doc_no"] == "3001-2974") == 1   # 옛 2건 대체


# --------------------------------------------- daily_update: 개정분도 임베딩 대상
def _patch_daily(monkeypatch, qa, std):
    from rag import daily_update as D
    monkeypatch.setattr(D, "run_qa", lambda: qa)
    monkeypatch.setattr(D, "run_standards", lambda: std)
    calls = []
    monkeypatch.setattr("rag.embed.run", lambda *a, **k: calls.append(1))
    monkeypatch.setattr("sys.argv", ["daily_update"])
    return D, calls


def test_daily_update_embeds_when_only_recollected(monkeypatch):
    """신규 글이 0건이어도 기준서 개정 재수집이 있으면 임베딩을 돌려야 한다."""
    D, calls = _patch_daily(monkeypatch, {"016001": (0, 5)}, {"3001": (0, 60, 1)})
    D.main()
    assert calls == [1]


def test_daily_update_skips_embed_when_nothing_changed(monkeypatch):
    D, calls = _patch_daily(monkeypatch, {"016001": (0, 5)}, {"3001": (0, 60, 0)})
    D.main()
    assert calls == []


def test_daily_update_tolerates_failed_board(monkeypatch):
    D, calls = _patch_daily(monkeypatch, {"016001": None}, {"3001": (0, 60, 1)})
    D.main()
    assert calls == [1]
