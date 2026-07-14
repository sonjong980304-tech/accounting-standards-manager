# -*- coding: utf-8 -*-
"""판사 LLM 추상화 (실시간 평가 B트랙): OpenAI / Anthropic / Google.

- rag/llm.py 모델 추상화와 같은 패턴. 채점 프롬프트는 벤더 무관 공용.
- 지표: Faithfulness(근거 충실도), Answer Relevancy(질문 관련성). 0~1 + 구조화 출력.
- 키는 인자로만 받아 메모리에서 사용(파일·로그 저장 금지). 실패 시 예외 → 호출부가 조용히 처리.
- 판사 벤더가 답변 모델과 같으면 자기편향 경고는 호출부(UI)에서 표시.
"""
import json
import re
import time

VENDORS = {
    "OpenAI": {"default_model": "gpt-4o-mini", "family": "openai"},
    "Anthropic": {"default_model": "claude-sonnet-5", "family": "anthropic"},
    "Google": {"default_model": "gemini-2.5-flash-lite", "family": "google"},
}

FAITH_SYS = (
    "너는 회계 답변 평가자다. '답변'의 각 주장이 '근거'에서 뒷받침되는지 검증한다. "
    "근거에 없는 내용을 지어낸 주장(환각)을 찾는다. 아래 JSON만 출력한다.\n"
    '{"score": 0~1 사이 실수, "unsupported": ["근거 없는 주장 문장", ...]}\n'
    "score = 근거로 뒷받침되는 주장 비율. 모두 뒷받침되면 1.0, 절반이 환각이면 0.5.")

RELEVANCY_SYS = (
    "너는 회계 답변 평가자다. '답변'이 '질문'에 실제로 답했는지 평가한다. "
    "질문의 핵심을 다뤘으면 높고, 겉돌거나 회피하면 낮다. 아래 JSON만 출력한다.\n"
    '{"score": 0~1 사이 실수, "reason": "한 줄 근거"}')


def _extract_json(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


class Judge:
    """벤더 무관 판사. score/구조화 출력, 파싱 실패 시 1회 재시도."""

    def __init__(self, vendor, api_key, model=None):
        if vendor not in VENDORS:
            raise ValueError("알 수 없는 판사 벤더: " + vendor)
        self.vendor = vendor
        self.family = VENDORS[vendor]["family"]
        self.model = model or VENDORS[vendor]["default_model"]
        self.api_key = api_key      # 메모리에만
        self._client = self._make_client()

    def _make_client(self):
        if self.family == "openai":
            from openai import OpenAI
            return OpenAI(api_key=self.api_key)
        if self.family == "anthropic":
            import anthropic
            return anthropic.Anthropic(api_key=self.api_key)
        if self.family == "google":
            from google import genai
            return genai.Client(api_key=self.api_key)
        raise ValueError(self.family)

    def _raw(self, system, user):
        max_attempts = 5
        for attempt in range(max_attempts):
            try:
                return self._raw_once(system, user)
            except Exception:
                if attempt == max_attempts - 1:
                    raise
                time.sleep(2 ** (attempt + 1))  # 일시적 서버 과부하(429/503) 대응, 2/4/8/16초

    def _raw_once(self, system, user):
        if self.family == "openai":
            r = self._client.chat.completions.create(
                model=self.model, temperature=0,
                response_format={"type": "json_object"},
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}])
            return r.choices[0].message.content
        if self.family == "anthropic":
            r = self._client.messages.create(
                model=self.model, max_tokens=1024, temperature=0,
                system=system, messages=[{"role": "user", "content": user}])
            return "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
        if self.family == "google":
            from google.genai import types
            r = self._client.models.generate_content(
                model=self.model, contents=system + "\n\n" + user,
                config=types.GenerateContentConfig(
                    temperature=0, response_mime_type="application/json"))
            return r.text
        raise ValueError(self.family)

    def _scored(self, system, user):
        for _ in range(2):          # 파싱 실패 시 1회 재시도
            parsed = _extract_json(self._raw(system, user))
            if parsed and "score" in parsed:
                try:
                    parsed["score"] = max(0.0, min(1.0, float(parsed["score"])))
                    return parsed
                except (TypeError, ValueError):
                    pass
        return None

    def faithfulness(self, answer, grounding):
        usr = "근거:\n{}\n\n답변:\n{}".format(grounding[:6000], answer)
        return self._scored(FAITH_SYS, usr)

    def answer_relevancy(self, question, answer):
        usr = "질문: {}\n\n답변:\n{}".format(question, answer)
        return self._scored(RELEVANCY_SYS, usr)

    def evaluate(self, question, answer, grounding):
        """두 지표 채점. 반환: {faithfulness, unsupported, answer_relevancy, reason} 또는 None."""
        f = self.faithfulness(answer, grounding)
        r = self.answer_relevancy(question, answer)
        if f is None and r is None:
            return None
        return {
            "faithfulness": f.get("score") if f else None,
            "unsupported": f.get("unsupported", []) if f else [],
            "answer_relevancy": r.get("score") if r else None,
            "relevancy_reason": r.get("reason", "") if r else "",
            "judge_vendor": self.vendor, "judge_model": self.model,
        }
