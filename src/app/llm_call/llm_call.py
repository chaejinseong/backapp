import json
import logging
import os
import re
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, TypeVar

import anthropic
from anthropic import AsyncAnthropic
from pydantic import BaseModel, ValidationError

from app.enums.llm_call_enum import LlmCallPurpose
from app.schema.llm_schema import LlmReplyOutput, LlmSummaryOutput

logger = logging.getLogger(__name__)

# Claude 호출 (상담 턴 / 사후 요약)
# - DB에 접근하지 않는다. 필요한 값은 service가 조회해서 아래 dataclass로 넘긴다.
# - 예외를 밖으로 던지지 않는다. 항상 (파싱된 출력 또는 None, 호출 기록 dict)를 반환한다.
#   호출 기록은 service가 create_llm_call(message_id=..., **record)로 그대로 저장한다. (키 = LlmCall 컬럼명)
#   parse_status, validation_errors, message_id는 service가 채운다.
# - 함수 안에서 재시도하지 않는다. (재시도 루프는 service. SDK 자동 재시도도 최소로 둔다)
# - 대화 이력은 멀티턴 messages가 아니라 user 메시지 1개 안의 <conversation> 태그로 넣는다.
#
# 사용자 발화 처리 순서 (service에서)
# 1. 사용자 발화는 먼저 감정분석 모델과 위험분석 모델을 통과한다. (emotion_analyses, risk_events에 저장)
# 2. 그 결과를 ConversationMessage.emotion, ReplyContext.risk_signals로 담아 넘기면 여기서 프롬프트에 조립한다.
#
# structured output
# - messages.parse()는 응답이 스키마에 맞지 않으면(거절, 잘린 출력) SDK 안에서 바로 ValidationError를 던져
#   stop_reason과 토큰 사용량을 잃는다. 그래서 messages.create()에 스키마를 직접 넘기고 파싱은 여기서 한다.
#   스키마 변환은 parse()와 같은 anthropic.transform_schema를 쓴다.

# 1. 설정값
LLM_MODEL = os.getenv("LLM_MODEL", "claude-haiku-5-5")
LLM_SUMMARY_MODEL = os.getenv("LLM_SUMMARY_MODEL", LLM_MODEL)
LLM_EFFORT = os.getenv("LLM_EFFORT", "low")  # 상담 턴은 응답 속도가 중요하다
LLM_SUMMARY_EFFORT = os.getenv("LLM_SUMMARY_EFFORT", "medium")
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "8000"))  # thinking도 포함되므로 여유 있게
LLM_SUMMARY_MAX_TOKENS = int(os.getenv("LLM_SUMMARY_MAX_TOKENS", "8000"))
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "60"))
# 타임아웃·네트워크·API 오류는 SDK 재시도만 한다. (service 재시도 루프는 출력 형식 오류만 다시 호출한다)
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "1"))
ERROR_TEXT_LIMIT = 500  # llm_calls.error에 남길 최대 길이

PROMPT_DIR = Path(__file__).parent / "prompts"
REPLY_PROMPT_FILE = "rag_counseling_system_prompt.txt"
SUMMARY_PROMPT_FILE = "session_summary_system_prompt.txt"

# RAG 문서 source -> 프롬프트 태그. 목록에 없는 문서는 reference_qna로 넣는다.
REFERENCE_TAG_BY_SOURCE = {
    "01_hospital_counseling_guidelines.txt": "hospital_guidelines",
    "02_doctor_counseling_style_demo.txt": "doctor_style",
}
REFERENCE_TAGS = ("hospital_guidelines", "doctor_style", "reference_qna")
DEFAULT_REFERENCE_TAG = "reference_qna"

NOT_PROVIDED = "제공되지 않음"

# llm_calls.status 값
# - success  : 호출 성공 (출력이 스키마를 통과하지 못했으면 error에 상세 사유가 남는다)
# - error    : API·네트워크 오류, 예상하지 못한 예외
# - timeout  : 타임아웃
# - refusal  : 안전 분류기 거절
# - truncated: max_tokens에서 출력이 잘림
STATUS_SUCCESS = "success"
STATUS_ERROR = "error"
STATUS_TIMEOUT = "timeout"
STATUS_REFUSAL = "refusal"
STATUS_TRUNCATED = "truncated"

EMOTION_FIELDS = ("anger", "joy", "anxiety", "embarrassment", "sadness", "hurt")


# 2. 입력 데이터 (service가 ORM에서 값만 옮겨 담는다)
# 감정분석 모델 결과 (6분류 점수 0~1)
@dataclass(frozen=True)
class EmotionScores:
    anger: float
    joy: float
    anxiety: float
    embarrassment: float
    sadness: float
    hurt: float
    dominant: str | None = None


# 위험분석 모델 결과 1건 (RiskEvent)
@dataclass(frozen=True)
class RiskSignal:
    message_seq: int  # 위험이 감지된 사용자 메시지의 seq
    level: str  # low / medium / high
    category: str | None = None
    evidence: str | None = None
    is_uncertain: bool = False


# 대화 메시지 1건
# - emotion: 사용자 메시지의 감정분석 결과 (assistant 메시지, 분석 전이면 None)
@dataclass(frozen=True)
class ConversationMessage:
    seq: int
    role: str  # user / assistant
    content: str
    emotion: EmotionScores | None = None


# 세션 체크리스트 항목 1건
@dataclass(frozen=True)
class ChecklistEntry:
    item_id: int
    label: str
    instruction: str
    answer_type: str
    options: Any  # 응답 범위 (JSONB 그대로). number·scale {"min","max"}, choice {"label": [...]}, 그 밖에는 null
    required: bool
    status: str  # pending / done / skipped
    ask_count: int
    skip_reason: str | None = None  # skipped일 때의 사유 (종료 조건 판정용, 프롬프트에는 넣지 않는다)


# RAG 검색 결과 1건
@dataclass(frozen=True)
class ReferenceChunk:
    source: str | None
    title: str
    content: str


# 상담 턴 입력
@dataclass(frozen=True)
class ReplyContext:
    conversation: list[ConversationMessage]  # seq 순, 이번 사용자 메시지 포함
    checklist: list[ChecklistEntry]
    # 위험분석 결과 (이번 세션의 감지 건. 이번 발화와 이전 발화를 seq로 구분한다)
    # - None: 분석 결과가 없다 (모델 적용 전, 오류). 위험이 없다는 뜻이 아니다.
    # - []: 분석했고 감지된 신호가 없다.
    risk_signals: list[RiskSignal] | None
    # RAG 검색 결과. None이면 검색 실패 또는 미사용 (RAG 없이 진행)
    references: list[ReferenceChunk] | None
    previous_summary: str | None = None  # 이전 세션의 context_summary
    care_notice: str | None = None  # 서버가 만든 진료 변경 안내 문장 (의사 탈퇴 등). 없으면 None
    user_turns: int = 0  # 이번 메시지를 포함한 사용자 발화 수
    min_user_turns: int = 0  # 종료할 수 있는 최소 사용자 발화 수 (service의 MIN_USER_TURNS)
    feedback: str | None = None  # 직전 시도의 검증 실패 사유
    attempt: int = 1


# 사후 요약 입력
@dataclass(frozen=True)
class SummaryContext:
    conversation: list[ConversationMessage]  # 세션 전체, 사용자 메시지에 emotion 포함
    session_type: str  # intake / 그 밖
    has_emotion: bool
    feedback: str | None = None
    attempt: int = 1


# 3. 클라이언트 / 프롬프트 (프로세스당 1번만 만든다)
@lru_cache(maxsize=1)
def get_client() -> AsyncAnthropic:
    # API 키는 ANTHROPIC_API_KEY 환경 변수에서 SDK가 읽는다.
    return AsyncAnthropic(timeout=LLM_TIMEOUT, max_retries=LLM_MAX_RETRIES)


@dataclass(frozen=True)
class PromptFile:
    text: str
    version: str  # llm_calls.prompt_version (예: counsel-0.3)


@lru_cache(maxsize=None)
def load_prompt(file_name: str, prefix: str) -> PromptFile:
    text = (PROMPT_DIR / file_name).read_text(encoding="utf-8").strip()
    # 파일 머리의 "버전: 0.3" 줄에서 버전을 읽는다. 프롬프트를 고치면 버전만 올리면 기록이 구분된다.
    match = re.search(r"^버전:\s*(\S+)", text, re.MULTILINE)
    version = match.group(1) if match else "unknown"
    return PromptFile(text=text, version=f"{prefix}-{version}")


@lru_cache(maxsize=None)
def _output_schema(output_model: type[BaseModel]) -> dict[str, Any]:
    return anthropic.transform_schema(output_model.model_json_schema())


# 4. 프롬프트 조립 (순수 함수)
# 사용자 발화나 문서에 태그 문자열이 들어 있으면 구조가 깨지므로 꺾쇠를 이스케이프한다.
def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _tag(name: str, body: str) -> str:
    return f"<{name}>\n{body}\n</{name}>"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _render_emotion(emotion: EmotionScores) -> str:
    scores = ", ".join(f"{name}={getattr(emotion, name):.2f}" for name in EMOTION_FIELDS)
    return f"{scores}, dominant={emotion.dominant}" if emotion.dominant else scores


def _render_message(message: ConversationMessage, with_emotion: bool) -> str:
    header = f"seq={message.seq}, role={message.role}"
    if with_emotion and message.emotion is not None:
        header += f", emotion: {_render_emotion(message.emotion)}"
    return f"[{header}] {_escape(message.content)}"


def _render_conversation(conversation: list[ConversationMessage], with_emotion: bool) -> str:
    if not conversation:
        return NOT_PROVIDED
    return "\n".join(_render_message(message, with_emotion) for message in conversation)


def _render_checklist(checklist: list[ChecklistEntry]) -> str:
    if not checklist:
        return NOT_PROVIDED
    lines: list[str] = []
    for entry in checklist:
        lines.append(
            f"- item_id={entry.item_id} | label={_escape(entry.label)} | answer_type={entry.answer_type}"
            f" | required={str(entry.required).lower()} | status={entry.status} | ask_count={entry.ask_count}"
        )
        lines.append(f"  instruction: {_escape(entry.instruction)}")
        if entry.options is not None:
            lines.append(f"  options: {_escape(_json(entry.options))}")
    return "\n".join(lines)


def _latest_user_seq(conversation: list[ConversationMessage]) -> int | None:
    user_seqs = [message.seq for message in conversation if message.role == "user"]
    return max(user_seqs) if user_seqs else None


def _render_risk_fields(signal: RiskSignal) -> str:
    parts = [f"level={signal.level}"]
    if signal.category:
        parts.append(f"category={_escape(signal.category)}")
    parts.append(f"is_uncertain={str(signal.is_uncertain).lower()}")
    if signal.evidence:
        parts.append(f"evidence={_escape(signal.evidence)}")
    return ", ".join(parts)


def _render_risk(risk_signals: list[RiskSignal] | None, latest_user_seq: int | None) -> str:
    if risk_signals is None:
        return NOT_PROVIDED

    current = [signal for signal in risk_signals if signal.message_seq == latest_user_seq]
    previous = sorted(
        (signal for signal in risk_signals if signal.message_seq != latest_user_seq),
        key=lambda signal: signal.message_seq,
    )

    lines = [f"이번 발화(seq={latest_user_seq}):"]
    if current:
        lines.extend(f"- {_render_risk_fields(signal)}" for signal in current)
    else:
        lines.append("- 감지된 위험 신호 없음")
    if previous:
        lines.append("이전 발화:")
        lines.extend(f"- seq={signal.message_seq}, {_render_risk_fields(signal)}" for signal in previous)
    return "\n".join(lines)


def _render_current_emotion(conversation: list[ConversationMessage], latest_user_seq: int | None) -> str:
    for message in conversation:
        if message.seq == latest_user_seq and message.emotion is not None:
            return f"이번 발화(seq={latest_user_seq}): {_render_emotion(message.emotion)}"
    return NOT_PROVIDED


def _render_references(references: list[ReferenceChunk] | None) -> dict[str, str]:
    grouped: dict[str, list[str]] = {tag: [] for tag in REFERENCE_TAGS}
    for chunk in references or []:
        tag = REFERENCE_TAG_BY_SOURCE.get(chunk.source or "", DEFAULT_REFERENCE_TAG)
        grouped[tag].append(f"[출처: {_escape(chunk.title)}]\n{_escape(chunk.content)}")
    return {tag: "\n\n".join(items) if items else NOT_PROVIDED for tag, items in grouped.items()}


def _render_session_state(user_turns: int, min_user_turns: int) -> str:
    # 종료 조건을 미리 알려서, 조건이 안 되는데 마무리 인사를 하는 출력(재시도 대상)을 줄인다.
    lines = ["상담이 진행 중이다.", f"사용자 발화 수: {user_turns}회 (이번 발화 포함)"]
    if user_turns < min_user_turns:
        lines.append(f"사용자 발화가 {min_user_turns}회 이상이어야 종료할 수 있다. 이번 턴에는 counsel_end를 true로 두지 않는다.")
    else:
        lines.append("필수 항목이 모두 끝났다면 이번 턴에 종료할 수 있다.")
    return "\n".join(lines)


# 상담 턴 user 메시지
# - 순서: 세션 상태 -> 이전 요약 -> 체크리스트 -> 위험/감정 분석 -> RAG -> 대화 원문 -> 재시도 사유(맨 끝)
def build_reply_user_content(context: ReplyContext) -> str:
    latest_user_seq = _latest_user_seq(context.conversation)
    references = _render_references(context.references)

    sections = [
        _tag("session_state", _render_session_state(context.user_turns, context.min_user_turns)),
        _tag("previous_session_summary", _escape(context.previous_summary) if context.previous_summary else NOT_PROVIDED),
        _tag("care_notice", _escape(context.care_notice) if context.care_notice else NOT_PROVIDED),
        _tag("checklist", _render_checklist(context.checklist)),
        _tag("risk_analysis", _render_risk(context.risk_signals, latest_user_seq)),
        _tag("emotion_analysis", _render_current_emotion(context.conversation, latest_user_seq)),
        *(_tag(tag, references[tag]) for tag in REFERENCE_TAGS),
        _tag("conversation", _render_conversation(context.conversation, with_emotion=False)),
    ]
    if context.feedback:
        sections.append(_tag("retry_feedback", _escape(context.feedback)))
    return "\n\n".join(sections)


# 사후 요약 user 메시지
def build_summary_user_content(context: SummaryContext) -> str:
    session_info = (
        f"session_type={context.session_type}\n"
        f"감정 분석 데이터: {'있음' if context.has_emotion else '없음'}"
    )
    sections = [
        _tag("session_info", session_info),
        _tag("conversation", _render_conversation(context.conversation, with_emotion=True)),
    ]
    if context.feedback:
        sections.append(_tag("retry_feedback", _escape(context.feedback)))
    return "\n\n".join(sections)


# 5. 호출
OutputT = TypeVar("OutputT", bound=BaseModel)


def _error_text(error: Exception) -> str:
    # 상담 원문이나 프롬프트가 섞이지 않도록 예외 종류와 짧은 메시지만 남긴다.
    if isinstance(error, anthropic.APIStatusError):
        text = f"{type(error).__name__}(status={error.status_code}): {error.message}"
    else:
        text = f"{type(error).__name__}: {error}"
    return text[:ERROR_TEXT_LIMIT]


def _schema_error_text(error: ValidationError) -> str:
    # 필드 경로와 사유만 남긴다. (input은 상담 원문이 섞일 수 있어 넣지 않는다)
    details = "; ".join(
        f"{'.'.join(str(part) for part in item['loc']) or '(전체)'}: {item['msg']}"
        for item in error.errors(include_url=False, include_context=False, include_input=False)
    )
    return f"출력 스키마 검증 실패 ({error.error_count()}건): {details}"[:ERROR_TEXT_LIMIT]


def _sum_input_tokens(usage: Any) -> int | None:
    if usage is None:
        return None
    return (
        (usage.input_tokens or 0)
        + (usage.cache_read_input_tokens or 0)
        + (usage.cache_creation_input_tokens or 0)
    )


def _finish(record: dict[str, Any], started: float) -> dict[str, Any]:
    record["latency_ms"] = int((time.perf_counter() - started) * 1000)
    return record


# 공통 호출 1회
# - 반환: (파싱된 출력 또는 None, 호출 기록)
async def _call(
    *,
    purpose: LlmCallPurpose,
    model: str,
    effort: str,
    max_tokens: int,
    prompt: PromptFile,
    user_content: str,
    output_model: type[OutputT],
    attempt: int,
) -> tuple[OutputT | None, dict[str, Any]]:
    record: dict[str, Any] = {
        "purpose": purpose,
        "model": model,
        "prompt_version": prompt.version,
        "attempt": attempt,
        "tokens_in": None,
        "tokens_out": None,
        "latency_ms": None,
        "status": STATUS_ERROR,
        "error": None,
    }
    started = time.perf_counter()

    try:
        response = await get_client().messages.create(
            model=model,
            max_tokens=max_tokens,
            # 고정 system prompt는 캐시한다. 매 턴 바뀌는 내용은 모두 user 메시지에 있다.
            system=[{"type": "text", "text": prompt.text, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_content}],
            output_config={
                "effort": effort,
                "format": {"type": "json_schema", "schema": _output_schema(output_model)},
            },
        )
    except anthropic.APITimeoutError as error:  # APIConnectionError의 하위 클래스라 먼저 잡는다
        record.update(status=STATUS_TIMEOUT, error=_error_text(error))
        return None, _finish(record, started)
    except (anthropic.RateLimitError, anthropic.APIStatusError, anthropic.APIConnectionError) as error:
        record.update(status=STATUS_ERROR, error=_error_text(error))
        return None, _finish(record, started)
    except Exception as error:  # 계약상 밖으로 던지지 않는다. (예상하지 못한 오류)
        logger.exception("LLM 호출 중 예상하지 못한 오류 (purpose=%s)", purpose.value)
        record.update(status=STATUS_ERROR, error=_error_text(error))
        return None, _finish(record, started)

    record.update(
        model=response.model or model,
        tokens_in=_sum_input_tokens(response.usage),
        tokens_out=response.usage.output_tokens if response.usage else None,
    )

    # 안전 분류기 거절: 출력이 스키마를 따르지 않을 수 있다. 같은 입력이면 다시 거절될 가능성이 높다.
    if response.stop_reason == "refusal":
        category = getattr(response.stop_details, "category", None) if response.stop_details else None
        record.update(status=STATUS_REFUSAL, error=f"refusal (category={category})")
        return None, _finish(record, started)

    # 출력이 max_tokens에서 잘렸다. JSON이 불완전하다.
    if response.stop_reason == "max_tokens":
        record.update(status=STATUS_TRUNCATED, error=f"max_tokens({max_tokens})에서 출력이 잘렸습니다.")
        return None, _finish(record, started)

    # 응답이 thinking 블록으로 시작할 수 있으므로 위치가 아니라 type으로 text를 찾는다.
    text = "".join(block.text for block in response.content if block.type == "text")
    record["status"] = STATUS_SUCCESS
    try:
        output = output_model.model_validate_json(text)
    except ValidationError as error:
        # 호출은 성공했지만 출력이 스키마를 통과하지 못했다. (parse_status는 service가 FAILED로 채운다)
        # 상세 사유는 service가 retry_feedback으로 모델에 다시 전달한다.
        record["error"] = _schema_error_text(error)
        return None, _finish(record, started)

    return output, _finish(record, started)


# 6. 상담 턴 1회
async def call_reply(context: ReplyContext) -> tuple[LlmReplyOutput | None, dict[str, Any]]:
    return await _call(
        purpose=LlmCallPurpose.REPLY,
        model=LLM_MODEL,
        effort=LLM_EFFORT,
        max_tokens=LLM_MAX_TOKENS,
        prompt=load_prompt(REPLY_PROMPT_FILE, "counsel"),
        user_content=build_reply_user_content(context),
        output_model=LlmReplyOutput,
        attempt=context.attempt,
    )


# 7. 사후 요약 1회
async def call_summary(context: SummaryContext) -> tuple[LlmSummaryOutput | None, dict[str, Any]]:
    return await _call(
        purpose=LlmCallPurpose.SUMMARY,
        model=LLM_SUMMARY_MODEL,
        effort=LLM_SUMMARY_EFFORT,
        max_tokens=LLM_SUMMARY_MAX_TOKENS,
        prompt=load_prompt(SUMMARY_PROMPT_FILE, "summary"),
        user_content=build_summary_user_content(context),
        output_model=LlmSummaryOutput,
        attempt=context.attempt,
    )
