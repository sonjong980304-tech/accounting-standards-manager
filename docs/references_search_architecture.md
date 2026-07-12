# 검색 아키텍처 근거 문헌 (1차 출처 검증, 2026-07-12)

BM25+dense 하이브리드 → RRF 병합 → cross-encoder 리랭커 재정렬 파이프라인의 학술 근거.
`factcheck` 프로토콜로 각 항목을 1차 출처(논문 원문/arXiv/발행사 공식 페이지)와 대조했다.

| 항목 | 확정값 | 출처 | 확신도 |
|---|---|---|---|
| RRF 원 논문 | Gordon V. Cormack, Charles L. A. Clarke, Stefan Büttcher, "Reciprocal rank fusion outperforms condorcet and individual rank learning methods", SIGIR '09 (ACM, pp. 758–759), 2009 | [research.google 공식 pub 페이지](https://research.google/pubs/reciprocal-rank-fusion-outperforms-condorcet-and-individual-rank-learning-methods/), [dblp](https://dblp.org/rec/conf/sigir/CormackCB09.html) | 확정 (서지사항 2개 독립 출처 일치) |
| cross-encoder 재순위화 근거 | Rodrigo Nogueira, Kyunghyun Cho, "Passage Re-ranking with BERT", arXiv:1901.04085 (2019). MS MARCO passage retrieval 리더보드에서 기존 SOTA 대비 MRR@10 27% 개선 | [arXiv:1901.04085](https://arxiv.org/abs/1901.04085) | 확정 |
| BGE-M3(이 프로젝트가 쓰는 임베더) 원 논문 | Jianlv Chen, Shitao Xiao, Peitian Zhang, Kun Luo, Defu Lian, Zheng Liu, "M3-Embedding: Multi-Linguality, Multi-Functionality, Multi-Granularity Text Embeddings Through Self-Knowledge Distillation", arXiv:2402.03216 (2024-02) | [arXiv:2402.03216](https://arxiv.org/abs/2402.03216) | 확정 |
| BM25/dense 하이브리드 일반 근거(BEIR) | Nandan Thakur, Nils Reimers, Andreas Rücklé, Abhishek Srivastava, Iryna Gurevych, "BEIR: A Heterogenous Benchmark for Zero-shot Evaluation of Information Retrieval Models", arXiv:2104.08663 (2021) | [arXiv:2104.08663](https://arxiv.org/abs/2104.08663) | 확정 |

## 각 논문이 실제로 뒷받침하는 주장

- **RRF (Cormack et al. 2009)**: 서로 다른 검색기(BM25 vs dense)의 원 점수는 스케일이 안 맞아(BM25는 18.3 같은 비정규화 점수, dense는 0~1 코사인 유사도) 직접 비교·가중합이 불가능하다는 문제에서, **점수 대신 등수(rank)만 사용**해 `score = Σ 1/(k+rank)`로 병합하는 방식을 제안하고, 이게 개별 방식이나 Condorcet 융합보다 성능이 낫다는 게 원 논문의 핵심 주장이다. k(보통 60)는 상위 등수 쏠림을 완화하는 감쇠 상수 — 이 프로젝트가 `RRF_K=60`을 쓰는 것과 정확히 일치.
- **BERT 재순위화 (Nogueira & Cho 2019)**: 1차 검색(BM25 등) 결과를 cross-encoder(질의+문서를 함께 인코딩)로 다시 채점하면 성능이 크게 오른다는 것을 MS MARCO에서 실증. "1차로 넓게 후보를 모으고, 2차로 비싼 모델이 정밀 재채점한다"는 이 프로젝트의 2단계 구조(retrieve→rerank)와 정확히 같은 패턴.
- **BGE-M3 (Chen et al. 2024)**: 이 프로젝트가 쓰는 임베딩 모델(`BAAI/bge-m3`) 자체의 논문. Abstract에서 "dense retrieval, multi-vector retrieval, sparse retrieval을 하나의 모델로 동시에 수행할 수 있다"고 명시 — 즉 **BGE-M3는 설계 단계부터 dense+sparse(BM25류) 하이브리드를 염두에 둔 모델**이다. 단, abstract 자체에 "하이브리드가 단일 방식보다 몇 %p 낫다"는 구체 수치는 없음(자기지식증류 학습기법이 핵심 기여) — 이 부분은 abstract 범위에서 확인 안 됨(과장하지 않음).
- **BEIR (Thakur et al. 2021)**: 18개 도메인 벤치마크에서 BM25가 여전히 강건한 baseline이며, dense/sparse 모델이 계산은 효율적이지만 일반화(도메인 이동) 성능이 떨어지는 경우가 많다는 결과. 이는 "dense만으로는 부족할 수 있어 BM25를 함께 쓴다"는 하이브리드 설계의 일반적 근거는 되지만, **RRF+rerank 조합이 우수하다는 직접 실험 결과는 이 논문 범위 밖**(정직하게 병기).

## 이 프로젝트 아키텍처와의 관계

이 프로젝트(`rag/search.py`)는 BM25(rank_bm25, BGE-M3 서브워드 토크나이저로 분절) + dense(BGE-M3 임베딩)를 각각 돌려 RRF(k=60)로 병합한 뒤 `BAAI/bge-reranker-v2-m3`로 재정렬한다. 이는 위 4개 문헌이 개별적으로 뒷받침하는 요소(RRF의 등수기반 병합 근거, cross-encoder 재순위화 근거, BGE-M3 자체의 멀티기능 설계 의도, BM25 병용의 일반적 타당성)를 조합한 표준적인 "하이브리드 검색 + 2단계 재순위화" 패턴이며, **이 정확한 조합(BM25+dense→RRF→BGE reranker) 자체를 실험적으로 검증한 단일 논문은 없다** — 각 구성요소가 개별적으로 검증된 것이지 전체 파이프라인이 하나의 논문에서 나온 게 아니라는 점을 명확히 한다(과장 금지).
