# KASB 회계기준 크롤러

## 목표
한국회계기준원(www.kasb.or.kr)에서 회계기준서 + 질의회신을 수집해
RAG용 코퍼스를 만든다. 최종 소비처는 Streamlit + Ollama 기반 RAG 앱.

## 수집 대상 (각각 별도 컬렉션으로 분리)
- K-IFRS 기준서: /front/board/ingAccountingList.do
- 일반기업회계기준: /front/board/List3003.do
- 질의회신 K-IFRS: /front/board/List016001.do
- 질의회신 IFRS 해석위원회: /front/board/List016002.do
- 질의회신 일반기업: /front/board/List016003.do
- 질의회신 K-IFRS 신속처리: /front/board/List016005.do
- 질의회신 일반기업 신속처리: /front/board/List016006.do

## 기술 제약
- 목록/다운로드가 javascript:void(0) POST 방식 → 요청 파라미터 역분석 필요
- requests로 목록/상세/다운로드가 재현 안 되는 경우(JS 렌더링, 세션 토큰 등)
  Playwright로 전환. 단, 먼저 requests로 시도하고 실패 원인을 보고한 뒤 전환할 것
- 응답 인코딩 확인 필수 (EUC-KR일 수 있음). 한글 깨지면 response.encoding 명시
- robots.txt 없음 확인됨. 단, 요청 간 1~2초 딜레이 필수 (time.sleep + jitter)
- User-Agent는 일반 브라우저 문자열로 정직하게 설정
- 첨부: PDF, HWP 혼재 + 첨부 없이 본문에 질의/회신이 있는 글도 있음
- HWP는 hwp5txt(pyhwp)로 텍스트 추출, 실패 시 해당 파일 스킵하고 로그에 기록

## 저장 구조
data/
  raw/<board_id>/          # 원본 첨부파일 그대로 보관 (재파싱 대비)
  parsed/<board_id>.jsonl  # 파싱 결과
  state/<board_id>.json    # 수집 완료된 글 ID 목록 (중단 후 재개용)

## 출력 형식 (JSONL, 1줄 = 1문서)
{ "source": "K-IFRS신속처리", "doc_no": "2025-I-KQA006",
  "title": "...", "reply_date": "...", "question": "...",
  "answer": "...", "standard_refs": ["제1023호 문단12"],
  "attachments": ["원본파일명"], "url": "...", "crawled_at": "ISO8601" }
- question/answer 분리가 안 되는 문서는 "body" 필드에 전문 저장
- doc_no 기준 중복 수집 방지

## 작업 순서
1. List016005 게시판 하나로 요청 구조 역분석 ✅ 완료 (아래 역분석 결과 참조)
2. 단일 게시판 크롤러 완성 → 3건만 수집해서 JSONL 품질 검증 ← 진행 중
3. 파서(본문 HTML / PDF / HWP)를 별도 모듈로 분리
4. 나머지 게시판으로 확장 (게시판별 구조 차이 확인하며)
5. 전체 수집 실행

## 하지 말 것
- 한 번에 전체 긁기 금지. 반드시 3건 테스트 → 1페이지 → 전체 순서
- 딜레이 없는 연속 요청 금지
- 파싱 실패를 조용히 넘기지 말 것. 실패 목록을 failures.log에 남길 것

---

## 역분석 결과 (2026-07-02, List016005 기준 — requests로 100% 재현 가능, Playwright 불필요)

- **인코딩**: UTF-8 (EUC-KR 아님). `response.encoding = "utf-8"` 명시.
- **세션**: 첫 GET에서 `JSESSIONID` 쿠키 발급 → `requests.Session()`으로 유지.
- **목록**: `POST /front/board/List016005.do`
  `data = {siteCd: "002000000000000", seq: "", ctgCd: "", replySummary: "N", searchfield: "ALL", searchword: "", s_date_start: "", s_date_end: "", page: N}`
  - 총 59페이지, 페이지당 실제 글 10건. JS 함수 `G_MovePage(pageNo)` 재현.
  - 목록에 공지(ctgCd=016009, 상단 고정)와 실제 글(ctgCd=016005)이 섞임 → **ctgCd 필터 필수**.
  - 행 추출: `onclick="fn_Detail('<seq>','<ctgCd>')"` 정규식.
  - 목록 컬럼: 번호/제목/첨부/회신일/공개일.
- **상세**: `GET /front/board/View{ctgCd}.do?siteCd=...&seq=<seq>&ctgCd=<ctgCd>&replySummary=N&searchfield=ALL...`
  - 메타 테이블: 분류 / 관련기준서(기준서 구분·기준서 명) / 회신일자 / 공개일 / 첨부파일.
  - 본문: `div.board_view_cont` 안에 `[질의]` / `[회신]` / `[관련 회계기준]` 텍스트 마커.
  - 본문 말미에 면책 문구("'신속처리질의'는 …") 존재 → 잘라낼 것.
  - 첨부: `div.board_view_file a` 의 `onclick="fileDownload('<fileNo>','<fileSeq>')"`.
  - 첨부 없는 글: "등록된 파일이 없습니다." 텍스트.
- **첨부 다운로드**: `POST /commonFile/fileDownload.do`, `data = {fileNo, fileSeq}`
  - 파일명은 `Content-Disposition`에 percent-encoded → `urllib.parse.unquote()`.
  - 시그니처: `PK`=xlsx/zip, `\xd0\xcf\x11\xe0`=HWP(CFBF), `%PDF`=PDF.
  - `fn_fileViewer`(synap)는 미리보기 전용 → 무시.
- **doc_no**: 사이트에 공식 문서번호(2025-I-KQA006 형식)가 노출되지 않음 →
  `<board_id>-<seq>` 형식으로 대체 (seq가 안정적 고유 ID). 중복 수집 방지 키 = seq.

## 2단계: 임베딩 + 벡터DB (2026-07-03 완료)
- **모델**: BGE-M3 dense (sentence_transformers, MPS fp16, max_seq 8192), 리랭커 bge-reranker-v2-m3.
  메모리 peak ~3.1GB(RSS; MPS unified 감안 최악 ~6GB) / 16GB. 임베딩(BGE-M3만)·검색(둘다) 분리.
- **ChromaDB 4컬렉션**(라우팅 단위, cosine): kifrs_standards 23,811 / kgaap_standards 2,001 /
  qa_kifrs 862 / qa_kgaap 574 = 27,248건. `data/chroma/` (257MB).
- **청크=레코드 1개**. 질의회신은 question+answer 합쳐 1임베딩. 용어는 "용어명: 정의"로 임베딩
  (용어명이 정의문에 없어 매칭 실패하던 버그 수정). 8192 초과 13건(전부 제1039호 등 병합
  아티팩트)은 모델 자동 절단, 전문은 메타에 보존.
- **메타데이터**: ref_key/doc_no/source/standard_no·장/record_type/page_no/src_file/url/attachments.
- **검색**(rag/search.py): BM25(BGE 서브워드) + dense 각 top50 → RRF(k=60) → bge-reranker top5.
  실행: `python3 -m rag.embed` (적재) / `python3 -m rag.search "질의"` (검색, --collections 라우팅).
- **검증 3케이스**: QA 정답이 리랭킹 후 전부 1위(40677/40670/016006-40625), 리랭커가 점수를
  0.03(RRF 평탄) → 0.99로 뚜렷이 분리. KGAAP 경로도 제30장 문단 30.10 상위 노출.
  - 관찰(LangGraph 단계 튜닝거리, 블로커 아님): ① 전역 flat 검색은 질의가 QA 질문과 유사해
    top5를 QA가 독식 → 기준서 문단은 컬렉션 스코핑/라우팅 시 노출(제1116호 문단7 dense 3위).
    ② 하위항목(⑴) 조각 레코드가 짧은 질의에서 노이즈 → 부모문단 우선 가중 검토.
    ③ 시나리오형 질의는 추상적 정의문보다 사례 문단에 쏠림 → 정의 조회는 라우팅으로 분리.

## 3단계: LangGraph 파이프라인 (2026-07-03)
- **그래프**(rag/graph.py): rewrite→route→retrieve→answer→verify. SQLite 체크포인터
  (rag/checkpoints.db, thread_id)로 대화기억 지속.
- **모델 추상화**(rag/llm.py): 노드별 교체 쉬움. 기본 rewrite/route=gpt-4o-mini,
  answer=gpt-5.5. `--local`시 전 노드 EXAONE 3.5 7.8B(Ollama, 미설치면 pull 안내).
  키 우선순위: 입력 인자 > env OPENAI_API_KEY > .env. **입력 키는 메모리에만, 저장 금지.**
- **노드**: ① rewrite(히스토리로 후속질문 독립화) ② route(컬렉션+유형[정의조회/사례시나리오/
  일반], 정의조회→standards 포함, 시나리오→qa 우선) ③ retrieve(retrieve_routed, 컬렉션쿼터로
  기준서근거 최소1 보장) ④ answer(근거만 사용, 없으면 "근거 못 찾음", JSON{answer,used_refs,
  has_grounds}) ⑤ verify(used_refs를 DB조회해 원문 반환, LLM 재생성 금지).
- **CLI**: `python3 -m rag.chat "질문" [--thread t --local --interactive]`.
- **평가는 RAGAS 지표(context recall/precision, faithfulness, answer relevancy)로 나중에
  구현. trace 로그(data/traces.jsonl)가 입력.** 스텁: rag/eval/{retrieval,generation}.py
  (시그니처+TODO만). 골든셋: eval/goldenset.jsonl(질문/기대컬렉션/기대ref_key/기대답변요지),
  검증 3+1케이스를 시드로.
## 4단계: Streamlit UI (2026-07-03)
- **rag/app.py** — 기존 graph.py/search.py를 그대로 호출(새 파이프라인 로직 없음), 화면만 입힘.
- **실행**: `python3 -m streamlit run rag/app.py` (주의: `streamlit` CLI는 pyenv 3.11에만 있어
  활성 3.9에선 `python3 -m streamlit` 필수). 기동 HTTP 200 확인.
- **A방식(체감속도)**: `graph.stream(stream_mode=["updates","messages"])` — updates로 단계 표시
  (질문이해→검색범위→근거검색), retrieve 직후 **근거 카드를 답변보다 먼저 렌더**, answer는
  **토큰 스트리밍**(199토큰 실측). retrieve 대기(~9s)를 진행표시+근거로 채움. B(속도)는
  retrieve_routed가 라우팅된 컬렉션만 검색하는 선까지만(per_coll 추가 축소 안 함).
- **answer 노드 스트리밍화**: JSON→평문+인라인 인용[ref_key]. used_refs는 인용에서 정규식 추출
  (환각 인용 방지). CLI 5케이스 회귀 재검증 5/5 통과, JSON 깨짐 0%.
- **3층 신뢰 UI**: ① 답변+인용 하이라이트(배지 클릭→근거카드 앵커 스크롤) ② 근거 원문 카드
  (expander): 질의회신=KASB 상세 url 직접링크(GET), 기준서/용어=**KASB 게시판 링크 + ref_key
  안내**, 각 카드에 ref_key/doc_no/source
  · **PDF 페이지 렌더 폐기(2026-07-03)**: PyMuPDF로 원본 PDF p.N 이미지 렌더를 넣었으나
    맥미니에서 미작동 확인 → 기능 제거하고 KASB 게시판 링크+번호 안내로 통일. 기준서 상세는
    POST(View3001.do)라 직접 딥링크 불가라 게시판 목록으로 안내. **data/raw/ 원본 PDF·HWP는
    재파싱용으로 보존(삭제 금지)**.
  ③ 해설(사용 근거 요약, 원문과 구분). 근거 못 찾으면 "근거를 찾지 못했습니다" 그대로 노출.
- **사이드바**: OpenAI 키 입력(password, session_state만·파일저장 금지, 우선순위 입력>env>.env),
  모델 선택 GPT/로컬 EXAONE(로컬 택 시 ollama 체크·미보유 안내), 대화 초기화(새 thread_id).
- **보안**: API 키를 State가 아닌 Pipeline 인스턴스에 둠 → SQLite 체크포인터에 키 저장 안 됨.
  .env는 권한 600 + .gitignore 차단.

## LangSmith 연동 (2026-07-03) — 토큰·비용·모델비교
- **역할 분리**: `data/traces.jsonl`=근거·ref_key 추적(RAGAS 평가 입력), **LangSmith**=토큰·비용·모델비교.
- **배선**(rag/llm.py): rewrite/route(raw openai)는 `wrap_openai`로, answer(ChatOpenAI)는 자동으로
  추적 — 둘 다 토큰·모델 캡처. 각 호출에 노드·모델 태그(`node:route`, `model:gpt-4o-mini` 등).
- **활성/비활성**: `.env`에 LANGCHAIN_TRACING_V2=true, LANGCHAIN_PROJECT=kasb-rag, LANGCHAIN_API_KEY.
  **키 없으면 `configure_langsmith()`가 TRACING_V2를 강제로 꺼서 조용히 비활성**(401·경고 없이,
  답변엔 영향 없음). get_llm/answer_chat_model/build_graph 진입 시 항상 호출.
- **토큰 캡처 검증**(rag/token_check.py): gpt-4o-mini 31/45·41, gpt-5.5 30/51(추론13),
  **Ollama(qwen3.5:9b)도 usage 잡힘 33/1174 → tiktoken 대안 불필요**(OpenAI-compat가 usage 반환).
  EXAONE 미설치라 로컬 검증은 qwen으로 대체(Ollama usage 동작은 모델 무관, 엔드포인트 특성).
- LangSmith 대시보드 확인은 사용자가 본인 LangSmith 키를 LANGCHAIN_API_KEY에 넣으면 즉시 활성화됨.
- 무효 키(401/403)도 `configure_langsmith`의 1회 검증으로 감지 → 조용히 off(매 호출 403 스팸 방지).

## 5단계: RAGAS 평가 (2026-07-04) — 배치(A)·실시간(B) 2트랙
- **지표**:
  - 검색(기계 채점, LLM 비용 0) — recall을 **3단 병기**(정직성): ① 문단 recall **exact**(정확한
    문단까지, 가장 엄격) ② 문단 recall **인접완화**(exact 1.0 + ±1 인접 문단 0.5점, 실용 하한)
    ③ **호 recall**(올바른 기준서 제NNNN호 회수, 실질 성능 상한). + 문단 exact **Precision**.
  - 생성(LLM-as-judge): **Faithfulness**(답변 주장이 근거로 뒷받침되는 비율+환각 문장 목록),
    **Answer Relevancy**(질문에 실제로 답했는지 0~1).
- **골든셋 출처·필터**(`rag/eval/build_goldenset.py` → `eval/goldenset.jsonl`): 질의회신 5개
  게시판(016001/002/005=K-IFRS, 016003/006=일반기업)에서 `question` 있는 글만. **정답 =
  standard_refs 중 실제 기준서 레코드(3001/3003.jsonl의 ref_key 또는 section_key)와 조인 성공한
  ref만**. 조인 안 되는 번호(노이즈·미수집·IFRS N 국제표기 등)는 검색이 못 찾는 게 당연 → 정답에서
  빼 recall 왜곡 방지. 조인 가능한 ref가 0개인 질의회신은 골든셋에서 제외.
  - 구축 결과: **골든셋 1,192건**(016001:112·002:142·005:486·003:437·006:15), 제외 244건
    (조인불가 239 + 질문없음 5), 정답 ref 평균 2.1개·최대 14개.
- **검색 대상 = 기준서 컬렉션 한정(핵심 방법론)**: 정답은 100% 기준서 문단이므로, 질의회신 질문을
  **전 컬렉션**에서 검색하면 유사 질의회신이 상위를 독식해 정답을 밀어냄(실측 문단 recall@10
  **3.7%→26%**로 7배 왜곡). → `run_batch.py`는 각 질의회신의 **기준서 컬렉션만**(board→standards)
  검색. 이로써 **self-leakage도 원천 차단**(질의회신 컬렉션을 안 봐 자기 글이 결과에 안 나옴,
  self제외 실측 0). doc_no 필터는 안전망으로 유지.
- **문단 exact가 낮은 이유(정직한 해석)**: 검색은 올바른 **호(기준서)는 잘 찾지만**(호 recall↑)
  인용된 **정확한 문단 번호**는 pinpoint가 어려움(예: 정답 `제1102호 B45`인데 검색 `B46/B53`).
  질의회신 1건당 평균 2.1·최대 14문단 인용 + 번호 축약/범위 인용 → exact는 하한, 호는 상한.
  그래서 **인접완화**(±1 문단 0.5점)를 별도 병기. 인접 판정은 자의성 배제 위해 **같은 호·같은
  상위경로(예 `5.7.`)·같은 접두(예 `B`,`AG`)·같은 꼬리·마지막 정수 차이 ±1**일 때만(단위테스트
  12/12). exact를 대체하지 않고 병기(인접완화 ≥ exact 구조 보장).
- **호/장 두 체계 혼재(주의)**: kifrs 기준서는 **"제NNNN호 문단 …"**, kgaap(일반기업기준)는
  **"제N장 문단 …"** 체계. 게다가 일반기업 질의회신 정답에 kifrs "제NNNN호"가 섞임. 그래서 호 추출·
  인접 판정 정규식은 `제\d+[호장]`으로 **둘 다** 잡아야 함(초판은 `제\d+호`만 잡아 016003/016006의
  호 recall이 0·인접완화 무효가 되는 버그 → 수정, 단위테스트 10/10).
- **배치(A, `rag/eval/run_batch.py`)**: 골든셋 → **retrieve만**(answer·라우터 LLM 미사용, 기준서
  컬렉션 dense+리랭킹) → 3단 recall+precision. top-5/top-10, 전체+게시판별. 산출:
  `eval/results/batch_YYYYMMDD.json`(원시 집계) + `eval/results/retrieval_YYYYMMDD.jsonl`(원시
  검색결과 dump) + `eval/results/summary.md`(성능표, 공개). **성능 수치는 summary.md 참조**.
- **재채점 구조(`rag/eval/rescore.py`)**: 배치가 원시 검색결과(top-k ref_key)를 dump로 남김 →
  채점 로직(호 추출·인접 판정·score)을 바꾼 뒤 `rescore.py`를 돌리면 **2시간 검색 재실행 없이 수 초
  내 지표 재계산**. (kgaap 호 버그도 이 구조 도입 후엔 재실행 불필요했을 것 → 교훈으로 반영.)
- **표기 정규화(score의 `_nk`)**: exact 매칭 시 **NFKC**로 원문자·전각을 표준형으로 풀어씀(제거
  아님) + 공백만 제거 → `B96⑷`↔`B96(4)`는 같게, `B96⑷`↔`B96⑴`은 **다르게**(하위항목 ⑴⑵⑷ 보존).
  ⚠️ **교훈**: 초기엔 원문자를 '제거'해 `B96⑴/⑵/⑷`를 하나로 합치는 바람에 recall이 26.7%→30.1%로
  **부풀려졌음**(기준서 고유키 3,131그룹 충돌 = false-positive). NFKC식은 충돌 **0개**, 실제 효과
  **+0.0%p**(골든셋에 `괄호숫자↔원문자` 진짜 표기차가 거의 없음). rescore로 재검색 없이 검증.
- **문단 exact 실패 원인 분류(dump 1,956건 자동)**: 표기차는 정규화로 흡수 → **진짜 검색 실패
  100%**. 내역: 62.6% 호는 맞고 문단만 놓침(pinpoint 한계) · 30.0% 기준서조차 못 찾음(광범위 인용)
  · 7.4% ±1 인접. **27%는 채점 아티팩트가 아닌 실제 검색 성능**. 답변 품질 영향 제한적(호 81% 커버).
- **향후 검색 개선 로드맵(미구현 — 문서화만)**:
  · **문단 정밀도**(실패 59%): per_coll·k 상향, 리랭커 강화, 문단 청킹 재검토. 예상효과=문단 exact
    소폭↑ / 비용=쿼리당 리랭킹 시간↑(후보 증가) / 리스크=**지표 개선이 답변 품질로 이어지는지
    미검증**(이미 호 recall 81%가 커버하는 영역) → **우선순위 뒤로**.
  · **기준서 회수**(실패 30%): 멀티홉 검색·쿼리 확장(질의회신이 여러 기준서에 걸쳐 인용). 예상효과=
    호·문단 recall 동반↑ / 비용=검색 단계 추가·지연↑ / 리스크=쿼리 확장이 노이즈 유입 가능.
- **리랭커 성능**: `load_reranker`를 **fp16 + max_length 512**로(임베더와 동일 정책). 문서 대부분이
  짧아(중앙 161자) 512로 99%+ 커버. fp32/1024 대비 쿼리당 40s→**6.3s(~6.3배)**, 전체 1192건 ≈ 2시간.
- **실시간(B, `rag/eval/judge.py` + app.py 사이드바 토글)**: "🔍 답변 품질 평가" 체크박스(기본
  off → 판사 호출 0). on 시 판사 벤더(OpenAI/Anthropic/Google) 선택 + 키 입력(세션 메모리만).
  답변 후 Faithfulness·Answer Relevancy 채점 → 컴팩트 표시 + 상세 expander.
- **판사 독립성**: 판사가 답변 모델과 같은 벤더면 "⚠ 자기편향 가능" 경고(다른 벤더 권장). GPT 답변
  =OpenAI, 로컬 EXAONE는 클라우드 벤더 아님 → 자기편향 대상 아님.
- **강건성**: 근거 없는 refusal(미국세법 등)은 채점 스킵. 판사 호출 실패 시 답변엔 영향 없이 평가만
  조용히 실패 표시. 평가 결과는 `graph.attach_eval`이 traces.jsonl 해당 레코드에 병합(질문-답변-평가
  한 레코드). 검증: 토글 off/키없음/refusal → 판사 0회, on+근거 → 1회(오프라인 계측 통과).
- **B 실검증 상태(2026-07-04)**: `judge.py` **OpenAI 경로 실 API 파싱 성공**(스텁 아님) — 근거충실
  답 Faithfulness 1.0·Relevancy 1.0, 억지/무근거 답 Faithfulness 0.0·unsupported에 문제 문장 포착.
  자기편향 배선 확인(`answer_vendor=None if local else "OpenAI"`, 사이드바+결과 양쪽 경고).
  **Anthropic/Google 경로는 SDK 설치·문법 확인, 실 API·브라우저 검증은 판사 키 확보 후**(현재 .env엔
  OpenAI·LangChain 키만).
- **C 로컬 모델 품질 정량화(`rag/eval/compare_models.py`, 2026-07-04 실행)**: 4케이스, 판사
  **OpenAI/gpt-4o-mini**(사용자 지시 — Anthropic/Google 키 없음. GPT 답변은 판사와 동일 벤더라
  **자기편향 참고**, EXAONE는 독립 평가). 결과 — **EXAONE Faithfulness가 낮음**: 파생상품 0.75·
  틀린전제 0.5 (GPT 1.0) = 근거 이탈/환각 경향. **질적 차이**: ① 인용 — EXAONE는 검색 근거
  ref_key(`016005-40670`) 대신 자체 지식 문단번호(`B4.3.5(5)`·`IE159`)를 인용(근거 이탈, used_refs도
  본문과 불일치) / GPT는 검색 ref_key 준수. ② 틀린전제('12개월 초과=금융리스') — GPT는 "아니오"로
  명확 교정+근거, EXAONE는 "확인되지 않습니다"로 애매. ③ 근거없음(미국세법) — 둘 다 refusal(환각방지
  정상). **정직성**: GPT 점수는 자기편향으로 부풀 수 있어 상대 우열은 참고용, EXAONE의 절대 약점
  (근거 충실도·틀린전제 교정)은 독립 평가라 신뢰 가능. 정성 관찰(로컬 인용·교정 약함)을 정량 확인.
- **키·PII 안전**: 평가 코드에 키·개인정보 하드코딩 없음. `.gitignore`로 `data/traces.jsonl`(라이브
  쿼리 원시로그)·`eval/goldenset.jsonl`·`eval/results/*.json`·`eval/results/*.jsonl`(dump) 제외,
  `summary.md`만 공개.

## answer has_grounds 이중 기준 (2026-07-03) — 로컬 완화 / GPT 엄격
- **GPT 경로(기본)**: 유효 인용([검색된 ref_key]) 필수. 인용 없으면 refusal. 5/5 검증 통과, 손대지 않음.
- **로컬(--local, EXAONE 3.5 7.8B) 경로만 완화**: EXAONE는 긴 컨텍스트(근거 6건)에서 [ref_key]
  인용 준수가 약해, 정답을 생성하고도 인용 형식 불일치로 refusal 오탐이 남(단기리스 케이스 실측).
  → 로컬은 `has_grounds = 모델이 refusal 안 함`으로 완화(인용 형식 무관). used_refs는 EXAONE
  인용 중 검색된 것 우선, 없으면 top 근거로 best-effort.
- **환각방지 보존**: 근거가 정말 없어 모델이 스스로 refusal하면(미국세법 등) 로컬도 그대로 유지.
  완화는 "근거는 있는데 인용 형식만 틀린" 경우로 한정. (retrieve가 근거 0이면 answer 진입 전 refusal)
- **이중 기준 이유**: GPT는 지시 준수가 강해 엄격 인용으로 근거-답변 정합을 보장할 수 있으나,
  7.8B 로컬 모델엔 과도한 게이트 → 사용 불가가 됨. 모델 역량 차이에 맞춘 게이트.

## 라우팅·검색 컬렉션별 보장 (2026-07-04) — 개발비 refusal 버그 수정
- **증상**: "개발비 자산인식요건"(무형자산)에 GPT가 **비결정적으로 refusal**(근거 카드는 뜨는데 답변
  "근거를 찾지 못했습니다"). EXAONE는 자기 지식으로 답(제11장 11.25 환각).
- **원인 3겹**(dump 실측): ① route LLM 비결정성으로 `kgaap_standards` 누락되는 실행 존재 ②
  `retrieve_routed`의 기준서 최소보장이 **전체 1개**라 kifrs 문단이 슬롯 독식 → kgaap 정답(제11장
  11.20) 누락 ③ **리랭커가 질의회신을 기준서 문단보다 극히 높게 줌**(0.9 vs 0.1) → 기준서 항상 밀림
  (개발비 정답 제11장 11.20이 4컬렉션 리랭킹 17위·kgaap 단독 2위). GPT는 근거 부실이면 정직하게
  refusal, EXAONE는 게이트 완화라 환각 답변으로 통과.
- **수정**: (a) `graph._framework_guard`: 모호(kgaap/kifrs 명시 신호 없음)면 QA뿐 아니라 **standards도
  양쪽** 보장. (b) `search.retrieve_routed`: 라우팅된 **각 standards 컬렉션마다 최소 1개씩** 보장
  (컬렉션 독식 방지; `min_standards=0`이면 스킵). (c) retrieve `k` 6→8.
- **검증**: 개발비 GPT 3회 모두 제11장 11.20 포함·정상 답변(비결정성 해소). 회귀 통과(단기리스·파생·
  중소기업특례 정상, 미국세법 refusal 유지 — 환각방지 보존).
- **배치 평가 무영향**: 배치(`run_batch`)는 **라우터 미사용** + `min_standards=0`으로 새 보장 로직을
  스킵 → recall 수치(문단 exact 0.267·호 0.808 등) 불변. 이번 수정은 실시간 앱 경로만 변경.
- **남은 근본 한계**: ③ 리랭커의 기준서 문단 저평가는 미해결(향후 개선 로드맵 참조). 이번 수정은
  "정답 컬렉션이 라우팅·슬롯에서 통째로 누락되던" 문제를 막은 것이지, 리랭커 순위 자체는 못 올림.

## rewrite 질의 정규화 (2026-07-05) — 표면형 민감 refusal 잔존분 수정
- **증상**: 위 2026-07-04 수정 후에도 "개발비**의** 자산인식요건 **알려줘**"는 GPT가 여전히
  비결정적 refusal. 재현 3/3 GPT 스스로 "근거를 찾지 못했습니다" 출력(인용 게이트 오탐 아님).
- **원인(실측)**: BGE-M3 임베더·리랭커가 **질의 표면형에 민감**. 동일 정답 제11장 11.20 리랭커
  점수가 "개발비 자산 인식 요건"(띄어쓰기) **0.608** vs "개발비의 자산인식요건 알려줘"(붙임+구어체)
  **0.012** → 후보 탈락. 제1038호 57도 동일(0.85 vs 탈락). 구어체·붙임 복합어가 정답 문단을
  검색에서 통째로 떨어뜨림. (EXAONE가 답한 건 게이트 완화로 자기지식 환각 — 제11장 11.25 등.)
- **수정**(`graph.Pipeline.rewrite`): 기존 "히스토리 있을 때만" 재작성을 **매 질의 정규화**로 변경.
  프롬프트에 검색 친화 규칙 추가(① 구어체 어미·군더더기 제거 ② 붙임 복합어 띄어쓰기 ③ 의미·핵심어
  보존·삭제금지 ④ 히스토리 있으면 맥락 확장). 매 질의 gpt-4o-mini 1회 추가 호출(경량).
- **검증(A/B/C)**: A 통과(정규화 후 11.20 점수 0.012→0.576·top-8 진입·정상답변) / B 통과(회귀 0 —
  단기리스·파생·중소기업특례 정상, **미국세법 refusal 유지**=억지 근거 안 만듦, 2턴 맥락 확장 정상)
  / C 2/3(붙임·구어체 민감도 해결, 단 "개발비는 **언제** 잡아?"→"인식 **시점**" 정규화는 요건 문단
  못 회수 — 의미가 다른 패러프레이즈 + 리랭커 저평가 잔존, 현행 유지 결정).
- **남은 한계**: 위 ③ 리랭커 기준서 저평가와 동일 뿌리. 정규화는 표면형 편차만 흡수, 리랭커 순위
  자체는 못 올림(향후 검색 depth·리랭커 개선 로드맵).
- **후속 보정(같은 날) — 주제전환 오염 방지**: "매 질의 정규화"가 히스토리를 무조건 붙여, 주제전환
  시 이전 주제가 오염됨(수익인식 대화 후 "파생상품 정의?"가 "수익인식 기준에서 파생상품~"으로 잘못
  확장). → rule ④를 **후속/새주제 판단 우선**으로 교체: 후속질문(지시대명사·생략)이면 맥락 확장,
  새 주제면 히스토리 무시하고 현재 질문만 재작성, 애매하면 현재 질문 우선(오염 < 맥락 누락).
  검증(2턴 5케이스+회귀): 주제전환 2/2 오염 0(수익인식→파생, 리스→재고), 후속 3/3 맥락 확장 유지
  (예: "개발비는 어떤가?"→used_refs 제11장 11.20, "그럼 그건 언제 환입하나?"→'그건'을 무형자산
  손상차손으로 해소), 단일턴 회귀 유지(개발비 정상·미국세법 refusal).

## 게시판별 구조 차이 (2026-07-03 확정, 각 3건 실측)

| 게시판 | source | 페이지수 | Q/A 위치 | 마커/형식 | doc_no |
|---|---|---|---|---|---|
| 016001 | K-IFRS질의회신 | 12p | 신형: 본문 / **구형: 첨부 PDF만**(본문은 색인카드) | `배경 및 질의`/`회신`/`판단근거` 헤딩. 첨부 PDF는 줄바꿈이 없어 **비앵커 패턴** 필요(`회신(?=\s*\d)`) | **공식번호** (첨부 파일명, 예: 2025-I-KQA006) |
| 016002 | IFRS해석위원회 | 18p | 본문 (해석위 안건결정 번역) | `1. 질의 내용`/`2. 검토 내용과 결정`·`2. 조사 결과와 결론`. 푸터 `● 색인어/Notice/알림` 트림 | 없음 → 게시판-seq. **참조가 IFRS/IAS 국제표기**라 standard_refs 빈 경우 있음(IFRS N↔제1NNN호 매핑 미구현) |
| 016003 | 일반기업질의회신 | 56p | 본문 | `1. (질의)현황`/`2. 질의 요약`/`3. 회신(안)·[안] 요약`/`판단근거(질의N)`. 구형은 `1. 질의 요약`/`2. 회신 요약` 2단 구조 | **공식번호** (예: 2025-G-KQA001) |
| 016005 | K-IFRS신속처리 | 59p | 본문 | `[질의]`/`[회신]`/`[관련 회계기준]` + 면책문구 트림 | 게시판-seq |
| 016006 | 일반기업신속처리 | 2p | 본문 | 괄호형 + `1. 질의요약`/`2. 회신요약` 헤딩형 혼재 (NBSP 주의) | 게시판-seq |
| 3001 | K-IFRS기준서 | 단일 | 첨부 HWP(+PDF) | 문단/용어 분리(split_standard). 접미사 문단은 사전순 아님(76→76ZA→76A→76B) → 숫자부만 단조 검사 | 3001-seq |
| 3003 | 일반기업 기준서 | 단일 | 첨부 HWP(+PDF) | **제N장 체계**, 문단 "31.9 …" 행 시작(split_kgaap_chapter). 재무회계개념체계는 장 형식 아님 → 원본만 | 3003-seq |

- Q/A가 본문에 없고 첨부에만 있으면 `extract_document_text`(hwp5html→pypdf) 체인으로 첨부에서 분리 (qa_source 필드에 출처 기록: html / attachment(pdf) / body_fallback)
- 한 문단 안에서 ⑴⑵… 목록이 재시작하는 경우(제1001호 문단 7 등) 첫 목록만 개별 레코드 (ref_key 중복 방지)
- 제1109호 등 대형 HWP는 hwp5html 변환이 10분 이상 → 타임아웃 1800s, 텍스트 캐시 필수

## 역분석 결과 — 기준서 게시판 (2026-07-02, ingAccountingList.do)

- **목록**: `GET /front/board/ingAccountingList.do` — 페이지네이션 없음(단일 페이지 전체 목록).
  컬럼: 구분(시행 중 등)/기준명/다운로드. 첨부(HWP+PDF)가 목록에서 바로 노출됨.
- **상세**: `fn_Detail(gubun, accstdSeq)` → `POST /front/board/View3001.do` (gubun=3001)
  — 질의회신 게시판과 파라미터 구조가 다름. board_id는 "3001" 사용.
- **첨부**: 목록의 `fileDownload('<fileNo>','<fileSeq>')` — 같은 fileNo에 fileSeq 1=PDF, 2=HWP.
- **기준서 파싱**: HWP가 1차 소스(**hwp5html** — 표 내용 포함 추출), 실패 시 pypdf 폴백,
  둘 다 실패 시 failures.log (`parsers.extract_document_text`). 문단 분리는 parsers/standard_split.py.
  - 개정 삽입 문단은 번호와 본문이 밀착됨("46A실무적…", "104리스이용자는…") → 밀착형 분기 존재.
  - 표는 행 단위 텍스트로 복원(셀 구분자 " | ") 후 해당 문단 text에 흡수. hwp5txt는 표를 유실하므로 사용 금지.
  - 부록A(용어의 정의)는 용어 하나당 레코드 하나, 2단 키로 저장:
    `ref_key="제1109호 용어의 정의:파생상품"` + `section_key="제1109호 용어의 정의"`
    (질의회신이 용어명 없이 섹션 수준으로 인용하므로 section_key로 거칠게 조인 후 term으로 좁힘).
    키 생성은 반드시 `refs.make_term_key()`/`refs.make_section_key()` 사용. page_no는 HWP 흐름에 없어 null.
  - 하위항목 원문자는 ⑴~⒇ 전부 개별 레코드 지원 (제1001호 문단 54의 ⒀~⒅ 실사용이
    warning으로 감지되어 2026-07-02 패턴 확장 완료 — 공용 모듈이라 조인 안 깨짐)
  - 용어정의 page_no는 A안: 원본 PDF에서 "용어명+정의 앞부분"을 정규화(공백 전제거)
    매칭해 시작 페이지를 채움 (바늘 40→24→12자 축소 재시도).
    못 찾으면 null 유지 + failures.log 기록. 제1116호 32/32건(100%) 채움 확인.
  - hwp5html 변환은 파일당 ~30초 → 크롤러는 추출 텍스트를 캐시할 것.

## 참조 정규화 (질의회신·기준서 파서 공용 — 필수)
ref_key 매칭이 깨지면 답변↔원문 대조가 불가능해지므로,
아래를 단일 모듈 **refs.py**(`normalize_ref` / `make_ref_key` / `extract_refs`)로
구현했고 양쪽 파서가 반드시 공유한다.

- 따옴표 변종(' ' ＇ " " 등) → 일반 ' 로 통일, 전각공백·NBSP → 일반공백
- 문단번호 패턴: `문단\s*((?:[A-Z]{1,2})?\d+(?:\.\d+)*[A-Z]*(?:[⑴-⒇])?)`
  → 9, 69, 76A, AG33, BC40, 7⑴ + 소수점 체계(4.1.2A, B4.1.7 — 제1109호 등)
  + 장.문단(10.14, 2.82⑶ — 일반기업) 커버. 한글(조사) 나오면 즉시 끊김
  ※ 원문자는 ⑴~⒇ (2026-07-02 확장: 제1001호 문단 54가 ⒅까지 실사용 확인)
- 일반기업회계기준 장(章) 참조: `make_kgaap_ref_key()` → "제10장 문단 10.14",
  "제2장 '재무제표의 작성과 표시Ⅰ'". 장 번호 선행 0 제거("제02장"→"제2장")
- 용어정의 섹션 참조: "(제1109호 용어의 정의)" → `make_section_key()` 키와 완전일치
- 중복 제거는 정규화 후 완전일치만. 기준서 제목참조와 문단참조는 별개로 둘 다 보존
- 기준서 파서는 문단 단위로 저장하고 각 문단에 ref_key를 부여
  (형식: "제1116호 문단 7⑴") → 질의회신 standard_refs와 동일 포맷이어야 조인됨
  ref_key 생성은 반드시 `refs.make_ref_key()` 사용
- 추가: "문단 9, 문단 BC40, 문단 BC41"처럼 쉼표로 이어지는 문단 나열도 전부 추출

### 용어정의 추출 (extract_term_records) 엣지케이스 처리 (2026-07-03)
- 일부 기준서(제1012호 법인세·제1019호 종업원급여)는 **부록A 용어표가 없고**
  정의가 문단 5에 콜론 리스트("회계이익: …")로 존재 → 문단 레코드에 보존됨.
  추출기가 이를 못 잡고 예시 계산표의 `|` 행을 용어로 오인 → 필터로 배제:
  ① 정의에 한글/영문 없음(숫자·기호뿐) = 예시표 셀, ② 용어가 날짜로 시작 = 개정이력,
  ③ 한 글자/표머리(계·합계·소계·구분). 검증된 정상 용어표(1109·1116·1113 등)는 불변.
- 결과: 3001 용어 224→204 (쓰레기 20 제거, 정상 전부 유지).

### 임베딩 전 조인키 정규화 4종 완료 (2026-07-03, 각 회귀 0)
질의회신 refs × 기준서 정밀 조인율 87.3% → **96.2%**, 전체 93.6% → **97.5%**.
1. **KGAAP 장문단 정규화** (`refs.normalize_kgaap_para`): "제N장 문단 M"→"제N장 문단 N.M"
   (QA 축약 인용 보정). 장문단 조인 81%→99%.
2. **QA 문단번호 노이즈 제거**: 본문 최대문단 초과 + 원문 부재 확인된 순수정수 문단 86종 제거
   (감사로그 data/noise_removed_audit.json). 원문 실재 문단은 절대 제거 안 함.
3. **부록A 없는 기준서 콜론 용어추출** (`extract_colon_terms`): 제1001·1007·1012·1016·1019·1032호
   문단5 "용어:정의" → 용어 레코드 (용어 204→393). 필터 3종 공용(`_valid_term`).
4. **국제표기 매핑** (`refs.extract_intl_refs`): IFRS N→제11NN, IAS N→제10NN, IFRIC N→제21NN,
   SIC N→제20NN (3001 목록 대조 검증). 016002 refs=0 8→4건, 조인 +291.

### 남은 파서 개선 (이월 — 조인 손실 관측됨)
- **제1039호(IAS39)·제1027호 삭제-갭 과소파싱**: 대폭 삭제된 기준서에서 문단 9→82 점프가
  MAX_NUM_JUMP(50) 초과로 잔존 문단(82·85·89·AG 등) 누락. cap을 150으로 올리면
  IG/예시 번호 오탐 폭증(제1039호 +507) → **섹션인식 기반 targeted fix 필요**.
  이월 상태에서 국제표기 매핑이 만든 제1039호 문단 refs 16종이 미조인(fix하면 자동 조인).
- **제1110호 부록A**: hwp5html이 표를 공백 2단으로 평탄화 → 전용 파서 필요(용어섹션 1종 미조인).
