import json
import logging
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from typing import Any, NoReturn

from fastapi import Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError

from app.llm_call import llm_call
from app.llm_call.llm_call import (
    ChecklistEntry,
    ConversationMessage,
    EmotionScores,
    ReferenceChunk,
    ReplyContext,
    RiskSignal,
    SummaryContext,
)
from app.infrastructure.postgresql import AsyncSessionLocal
from app.repository.llm_repository import LlmRepository, get_llm_repository
from app.repository.rag_repository import RagRepository
from app.service.rag_service import RagService, get_rag_service
from app.service.care_notice_service import CareNoticeService, get_care_notice_service
from app.ml.emotion_analysis import EmotionAnalyzer, get_emotion_analyzer
from app.models.chat_session_model import ChatSession
from app.models.message_model import Message
from app.models.session_checklist_model import SessionChecklist
from app.models.session_summary_model import SessionSummary
from app.models.emotion_analysis_model import EmotionAnalysis
from app.enums.chat_session_enum import (
    ChatSessionEndReason,
    ChatSessionStatus,
    ChatSessionSummaryStatus,
    ChatSessionType,
)
from app.enums.message_enum import MessageInputType, MessageRole, MessageSystemRecordStatus
from app.enums.llm_call_enum import LlmCallParseStatus
from app.enums.common_enum import ChecklistStatus
from app.enums.checklist_item_enum import ChecklistItemAnswerType
from app.enums.session_checklist_enum import SessionChecklistSkipReason
from app.schema.llm_schema import (
    KST,
    LlmReplyOutput,
    LlmSummaryOutput,
    ScheduledSessionCreateDTO,
    ChatSessionResponseDTO,
    MainSessionResponseDTO,
    PastSessionResponseDTO,
    MessageResponseDTO,
    MessageListResponseDTO,
    SendMessageResponseDTO,
)

# LLM 호출 + 검증을 반복하는 최대 횟수 (첫 호출 1회 + 재시도 1회)
MAX_LLM_ATTEMPTS = 2

# 세션 종료 판정에 필요한 최소 사용자 메시지 수
# TODO: 임시값. 확정 필요
MIN_USER_TURNS = 3

# 필수 항목에서 "완료로 인정"하는 건너뜀 사유 (UNCLEAR는 인정하지 않고 재질문)
# TODO: 임시값. 확정 필요
ALLOWED_REQUIRED_SKIP_REASONS = {SessionChecklistSkipReason.USER_DECLINED, SessionChecklistSkipReason.NOT_APPLICABLE}

# 모든 LLM 시도가 실패했을 때 프론트에 내려줄 오류 문구 (messages에는 저장하지 않는다)
FALLBACK_REPLY_TEXT = "llm답변 오류!"

# 감정분석 6종 (emotion_analyses 컬럼명). 사후요약 LLM 입력에 메시지별로 붙인다.
EMOTION_FIELDS = ("anger", "joy", "anxiety", "embarrassment", "sadness", "hurt")

# RAG 검색 질의에 합칠 최근 사용자 발화 수 ("네", "가끔요" 같은 짧은 답만으로는 검색이 어렵다)
RAG_QUERY_USER_TURNS = 3

logger = logging.getLogger(__name__)


# 예외처리
# 트랜잭션 관리 (commit / rollback은 이 service에서 정한다. repository는 flush까지만)
#
# 안전필터, 감정분석은 나중에 끼워 넣는다. (send_message 안의 표시된 위치)
# 세션 사후요약은 6번(create_session_summary). 세션이 ENDED가 된 요청(send_message, end_session)의 응답 뒤에
# API가 BackgroundTasks로 run_session_summary를 호출한다. (파일 맨 아래)
# 타임아웃 배치는 repository 함수가 추가된 뒤에 구현한다. (파일 맨 아래 메모)
# 세션 종료 경로는 두 가지다. 둘 다 종료할 때 summary_status=PENDING을 넣는다. (종료 전에는 null)
# - 체크리스트 완료: send_message의 종료 판정 (end_reason=COMPLETED)
# - 중간 이탈: 프론트가 상담 화면을 떠날 때 end_session으로 통지 (end_reason=USER). 종료 버튼은 없다.
# - 시작한 적 없이 마감(expires_at)이 지난 예정 세션은 process_session_timeouts가 MISSED로 바꾼다. (요청 시 처리)
# - TIMEOUT은 나중에 강제 종료 등으로 방치된 ACTIVE 세션을 정리할 때 쓴다. (파일 맨 아래 메모, 지금은 다루지 않음)
class LlmService:

    # rag_service는 같은 요청의 DB 세션을 함께 쓴다. (FastAPI가 get_postgresql_db를 요청당 1번만 만든다)
    # emotion_analyzer는 lifespan에서 한 번 로드한 모델이다. 로드에 실패했으면 None (감정분석 없이 진행)
    # care_notice_service는 진료 변경 안내(의사 탈퇴 등, Redis)를 상담 답변으로 전달할 때 쓴다.
    #   None이면 안내 없이 진행한다. (run_session_summary처럼 직접 생성하는 곳은 넘기지 않으므로 맨 뒤 선택 인자)
    def __init__(
        self,
        repo: LlmRepository,
        rag_service: RagService,
        emotion_analyzer: EmotionAnalyzer | None = None,
        care_notice_service: CareNoticeService | None = None,
    ):
        self.repo = repo
        self.rag_service = rag_service
        self.emotion_analyzer = emotion_analyzer
        self.care_notice_service = care_notice_service


    # 예외가 나면 rollback하고 다시 던진다. (HTTPException 포함. 잠금도 함께 풀린다)
    async def _rollback_and_raise(self, error: Exception) -> NoReturn:
        await self.repo.rollback()
        raise error


    # 세션 ORM -> 응답 DTO
    @staticmethod
    def _to_session_response(session: ChatSession) -> ChatSessionResponseDTO:
        return ChatSessionResponseDTO.model_validate(session)


    # 지금 접속할 수 있는 시간대인지: scheduled_at <= 현재 < expires_at
    @staticmethod
    def _is_within_access_window(session: ChatSession, now: datetime) -> bool:
        if session.scheduled_at and now < session.scheduled_at:
            return False
        if session.expires_at and now >= session.expires_at:
            return False
        return True


    # 1. 의사: 예정 세션 생성 (한 번 호출에 세션 1건)
    # - 담당 환자인지 확인: exists_care_relation (아니면 403). 담당 관계는 이 함수에서 한 번만 검증한다.
    # - 접속 기간 중복 확인: exists_overlapping_session (같은 환자의 기존 세션과 겹치면 409)
    # - 직전 세션: find_latest_session_summary(user_id)로 환자의 가장 최근 세션 요약을 찾아
    #   그 session_id를 prev_session_id로 넣는다. 없으면 None
    #   (요약이 아직 없는 세션은 직전 세션으로 잡히지 않는다)
    # - 체크리스트: find_checklist_items로 항목 검증(없거나 is_active=False면 거절)
    #   -> create_session_checklist(required, sort_order 포함)
    #   sort_order는 NOT NULL이라, 요청에 없으면 요청 목록의 순서(1부터)로 채운다.
    # - summary_status는 넣지 않는다. (null, 세션이 종료될 때 PENDING이 된다)
    # - 한 트랜잭션으로 묶어 commit
    async def create_scheduled_session(
        self,
        doctor_id: int,
        request: ScheduledSessionCreateDTO,
    ) -> ChatSessionResponseDTO:
        try:
            # 1. 담당 환자 확인 (403)
            if not await self.repo.exists_care_relation(doctor_id, request.user_id):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="담당 환자가 아닙니다."
                )

            # 2. 접속 기간 중복 확인 (409)
            if await self.repo.exists_overlapping_session(
                request.user_id, request.scheduled_at, request.expires_at
            ):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="해당 환자의 다른 세션과 접속 기간이 겹칩니다."
                )

            # 3. 체크리스트 항목 검증 (없거나 비활성이면 400)
            item_ids = [item.item_id for item in request.checklist_items]
            items = await self.repo.find_checklist_items(item_ids)
            if len(items) != len(item_ids) or any(not item.is_active for item in items):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="존재하지 않거나 사용할 수 없는 체크리스트 항목이 포함되어 있습니다."
                )

            # 4. 직전 세션 (없으면 None)
            latest_summary = await self.repo.find_latest_session_summary(request.user_id)
            prev_session_id = latest_summary.session_id if latest_summary else None

            # 5. 세션 + 체크리스트 생성
            session = ChatSession(
                user_id=request.user_id,
                prev_session_id=prev_session_id,
                status=ChatSessionStatus.SCHEDULED,
                session_type=request.session_type,
                scheduled_at=request.scheduled_at,
                expires_at=request.expires_at,
            )
            await self.repo.create_sessions([session])
            await self.repo.create_session_checklist(
                session.id,
                [
                    {
                        "item_id": item.item_id,
                        "required": item.required,
                        "sort_order": item.sort_order if item.sort_order is not None else index,
                    }
                    for index, item in enumerate(request.checklist_items, start=1)
                ],
            )
            await self.repo.commit()
        except Exception as error:
            await self._rollback_and_raise(error)

        return self._to_session_response(session)


    # 2. 환자: 상담 메인 (진행 중 세션, 없으면 다음 예정 세션)
    # - available(지금 접속 가능 여부)은 service가 scheduled_at과 비교해 계산
    # - 세션 상태를 화면에 보여 주는 요청이므로, 먼저 이 환자의 마감 지난 예정 세션을 MISSED로 정리한다. (7번)
    async def get_main_session(self, user_id: int) -> MainSessionResponseDTO | None:
        await self.process_session_timeouts(user_id)

        session = await self.repo.find_active_or_next_session(user_id)
        if session is None:
            return None

        # ACTIVE이면 항상 접속 가능, 예정 세션이면 접속 시간대 안인지 확인
        if session.status == ChatSessionStatus.ACTIVE:
            available = True
        else:
            available = self._is_within_access_window(session, datetime.now(KST))

        base = self._to_session_response(session)
        return MainSessionResponseDTO(**base.model_dump(), available=available)


    # 2-1. 환자: 이전 상담 목록 (시작한 적 있고 끝난 세션, 최근에 시작한 순)
    # - 상세 대화는 get_messages(GET /sessions/{session_id}/messages)로 본다.
    # - 사후요약(소견, 감정 요약)은 의사용 정보라 내려주지 않는다.
    # - 마감 정리(process_session_timeouts)는 부르지 않는다. 대상(MISSED)이 이 목록에 들어가지 않기 때문
    async def get_past_sessions(self, user_id: int) -> list[PastSessionResponseDTO]:
        sessions = await self.repo.find_past_sessions(user_id)
        return [PastSessionResponseDTO.model_validate(session) for session in sessions]


    # 3. 환자: 세션 시작 (SCHEDULED -> ACTIVE)
    # - 본인 세션인지(user_id) 확인한다. 아니면 404
    # - expires_at 검증은 이때 한 번만 한다. (시작한 뒤에는 다시 검증하지 않는다)
    # - 환자당 ACTIVE 1개는 DB의 부분 unique 인덱스(uix_chat_sessions_user_active)로도 막힌다.
    #   다른 세션 두 개를 동시에 시작하면 exists_active_session을 둘 다 통과할 수 있는데,
    #   이때 뒤쪽 요청은 flush에서 IntegrityError가 나므로 409로 바꿔서 돌려준다.
    async def start_session(self, user_id: int, session_id: int) -> ChatSessionResponseDTO:
        try:
            session = await self.repo.find_session_by_id(session_id, user_id=user_id, for_update=True)
            if session is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="세션이 존재하지 않습니다."
                )
            if session.status != ChatSessionStatus.SCHEDULED:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="시작할 수 있는 상태의 세션이 아닙니다."
                )
            if await self.repo.exists_active_session(user_id):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="이미 진행 중인 세션이 있습니다."
                )

            now = datetime.now(KST)
            if session.scheduled_at and now < session.scheduled_at:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="아직 접속할 수 있는 시간이 아닙니다."
                )
            if session.expires_at and now >= session.expires_at:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="접속 가능 시간이 지났습니다."
                )

            await self.repo.update_session(session, status=ChatSessionStatus.ACTIVE, started_at=now)
            await self.repo.commit()
        except IntegrityError:
            await self._rollback_and_raise(HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="이미 진행 중인 세션이 있습니다."
            ))
        except Exception as error:
            await self._rollback_and_raise(error)

        return self._to_session_response(session)


    # 3-1. 환자: 세션 이탈 종료 (프론트가 상담 화면을 떠날 때 통지)
    # - 설계: AI 상담은 접속하면 끝날 때까지 진행한다. 중간에 나가면 그 세션은 종료된다.
    # - ACTIVE -> ENDED, end_reason=USER, ended_at=now, summary_status=PENDING (사후요약 대상)
    # - 이미 ENDED면 바꾸지 않고 현재 상태를 돌려준다. (체크리스트 완료 후 나가는 경우, 통지가 두 번 오는 경우)
    # - 그 밖의 상태(SCHEDULED 등)는 409
    # - 진행 중인 send_message가 있으면, 그 턴의 트랜잭션 2가 재잠금에서 409가 되어 답변은 저장되지 않는다.
    async def end_session(self, user_id: int, session_id: int) -> ChatSessionResponseDTO:
        try:
            session = await self.repo.find_session_by_id(session_id, user_id=user_id, for_update=True)
            if session is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="세션이 존재하지 않습니다."
                )

            if session.status == ChatSessionStatus.ACTIVE:
                await self.repo.update_session(
                    session,
                    status=ChatSessionStatus.ENDED,
                    end_reason=ChatSessionEndReason.USER,
                    ended_at=datetime.now(KST),
                    summary_status=ChatSessionSummaryStatus.PENDING,
                )
            elif session.status != ChatSessionStatus.ENDED:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="진행 중인 세션이 아닙니다."
                )

            await self.repo.commit() # 이미 ENDED면 변경 없이 잠금만 푼다.
        except Exception as error:
            await self._rollback_and_raise(error)

        return self._to_session_response(session)


    # 4. 환자: 화면용 대화 기록 조회 (커서 방식)
    async def get_messages(
        self,
        user_id: int,
        session_id: int,
        after_seq: int | None,
        limit: int,
    ) -> MessageListResponseDTO:
        # 본인 세션인지만 확인한다. (타인 세션도 404로 존재를 숨긴다)
        session = await self.repo.find_session_by_id(session_id, user_id=user_id)
        if session is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="세션이 존재하지 않습니다."
            )

        messages, next_cursor = await self.repo.find_messages(session_id, after_seq, limit)
        return MessageListResponseDTO(
            items=[MessageResponseDTO.model_validate(message) for message in messages],
            next_cursor=next_cursor,
        )


    # 5. 환자: 사용자 메시지 전송 (한 턴 전체를 처리하고 AI 답변과 세션 상태를 반환)
    #
    # [트랜잭션 1: 세션 잠금]
    #  - _get_locked_active_session으로 잠금 조회 (본인 세션, ACTIVE)
    #  - 재전송 확인: find_message_by_client_id가 있으면 _find_reply로 기존 답변을 돌려주고 끝
    #  - 사용자 메시지 저장(create_message), commit (잠금 해제)
    #  - (나중에) 입력 안전필터
    # [프롬프트 조립 + LLM 호출 / 검증 루프, 최대 MAX_LLM_ATTEMPTS회]
    #  - _build_prompt_context -> _call_llm -> _validate_llm_output
    #  - 오류가 있으면 _build_retry_feedback으로 사유를 만들어 다시 호출
    #    (출력 형식·검증 오류만 재시도. 타임아웃·네트워크·거절은 _parse_failure_errors가 None -> 바로 실패 처리)
    #  - 호출 기록은 메모리에 모은다. (llm_calls.message_id가 NOT NULL이라 연결할 메시지가 정해진 뒤에 저장)
    #  - 모두 실패하면 _save_failed_turn: 오류 문구는 messages에 저장하지 않고 502 응답으로만 내려준다.
    # [트랜잭션 2: 재잠금 -> _save_turn_result -> commit]
    #  - (나중에) 출력 안전필터, 감정분석
    #  - 체크리스트 반영, 답변 메시지 저장, llm_calls 저장, 종료 판정
    # [응답]
    #  - 사용자에게 보일 값(답변, 표정)과 session_status만 내려준다. 체크리스트 값 등 내부 값은 제외.
    async def send_message(
        self,
        user_id: int,
        session_id: int,
        client_message_id: str,
        content: str,
        input_type: MessageInputType,
    ) -> SendMessageResponseDTO:
        # --- 트랜잭션 1: 세션 잠금 -> 재전송 확인 -> 사용자 메시지 저장 ---
        try:
            session = await self._get_locked_active_session(user_id, session_id)

            # 재전송이면 이미 만든 답변을 그대로 돌려준다.
            existing = await self.repo.find_message_by_client_id(session_id, client_message_id)
            if existing is not None:
                reply = await self._find_reply(session_id, existing)
                if reply is None:
                    # 같은 메시지를 아직 처리 중이거나, LLM 오류로 답변을 만들지 못한 메시지
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="이미 처리 중이거나 답변을 만들지 못한 메시지입니다."
                    )
                response = SendMessageResponseDTO(
                    message=MessageResponseDTO.model_validate(reply),
                    session_status=session.status,
                )
                await self.repo.commit() # 변경 없이 잠금만 푼다.
                return response

            user_message = await self.repo.create_message(
                session_id,
                MessageRole.USER,
                content,
                client_message_id=client_message_id,
                input_type=input_type,
            )
            await self.repo.commit() # 잠금 해제. 이후 LLM 호출 동안 세션을 잠그지 않는다.
        except Exception as error:
            await self._rollback_and_raise(error)

        # 여기부터 트랜잭션 2 전까지는 ORM 객체 대신 값만 쓴다.
        # - 분석/RAG의 실패 경로가 rollback하면 그 전에 읽은 ORM 객체가 모두 만료(expire)되고,
        #   async에서 만료된 속성에 접근하면 MissingGreenlet 오류로 턴 전체가 실패하기 때문이다.
        # - 체크리스트도 검증에는 context.checklist(값 스냅샷)를 쓰고, 저장할 때는 트랜잭션 2에서 다시 읽는다.
        user_message_id = user_message.id
        prev_session_id = session.prev_session_id

        # --- 사용자 발화 분석 ---
        # - 결과를 DB에 저장하면 아래 _build_prompt_context가 DB에서 일괄 조회해 프롬프트에 넣는다.
        # - 분석이 없거나 실패해도 상담은 계속한다. (프롬프트에는 "제공되지 않음"으로 들어간다)
        await self._analyze_emotion(user_message_id, content)
        # TODO: 위험분석 (guard_checks gate=a / risk_events). _analyze_emotion과 같은 패턴으로 이 위치에 넣는다.

        # --- 프롬프트 조립 + LLM 호출 / 검증 루프 ---
        # 프롬프트 재료(원문 + 감정, 체크리스트, 위험, 이전 요약)는 DB에서 일괄 조회하고, RAG 검색은 턴당 1번만 한다.
        context = await self._build_prompt_context(user_id, session_id, prev_session_id)
        document_ids = await self.repo.find_session_document_ids(session_id)
        await self.repo.commit() # 읽기 트랜잭션을 닫는다. (외부 API 호출 중 연결을 붙잡지 않기 위함)
        context = replace(
            context, references=await self._retrieve_references(context.conversation, document_ids)
        )

        output: LlmReplyOutput | None = None
        llm_call_records: list[dict[str, Any]] = []
        feedback: str | None = None

        for attempt in range(1, MAX_LLM_ATTEMPTS + 1):
            parsed, record = await self._call_llm(context, feedback, attempt)

            if parsed is None:
                errors = self._parse_failure_errors(record)
                if errors is None:
                    # 타임아웃·네트워크·API 오류·거절: 다시 호출하지 않는다. (네트워크 계열은 SDK가 이미 재시도함)
                    record["validation_errors"] = None
                    record["parse_status"] = LlmCallParseStatus.FAILED
                    llm_call_records.append(record)
                    break
            else:
                errors = await self._validate_llm_output(
                    session_id, parsed, context.checklist, context.user_turns
                )

            

            record["validation_errors"] = errors or None
            if errors:
                record["parse_status"] = LlmCallParseStatus.FAILED
            else:
                record["parse_status"] = LlmCallParseStatus.OK if attempt == 1 else LlmCallParseStatus.RETRIED_OK
            llm_call_records.append(record)

            if not errors:
                output = parsed
                break
            feedback = self._build_retry_feedback(errors)

        # 모든 시도 실패: 호출 기록만 남기고 502로 오류 문구를 내려준다.
        if output is None:
            await self._save_failed_turn(llm_call_records)

        # --- 트랜잭션 2: 재잠금 -> 저장 -> commit ---
        try:
            session = await self._get_locked_active_session(user_id, session_id)
            # TODO: 출력 안전필터 (guard_checks gate=OUT), 감정분석
            reply = await self._save_turn_result(session, output, llm_call_records)
        except Exception as error:
            await self._rollback_and_raise(error)

        # 진료 변경 안내를 프롬프트에 넣었고 답변 저장(commit)까지 성공했으면 안내를 지운다.
        # - 재전송(existing) 경로, 모든 시도 실패(_save_failed_turn) 경로는 여기까지 오지 않으므로 지우지 않는다.
        if context.care_notice is not None:
            await self._delete_care_notice(user_id)

        return SendMessageResponseDTO(
            message=MessageResponseDTO.model_validate(reply),
            session_status=session.status,
        )


    # 5-0. 턴 처리 내부: 사용자 발화 감정분석 -> emotion_analyses 저장
    # - 사용자 메시지를 commit한 뒤에 부른다. 추론하는 동안 세션 잠금과 DB 연결을 붙잡지 않는다.
    # - 점수는 Numeric(4,3) 컬럼에 맞춰 소수 셋째 자리로 반올림한다. (그래서 6개 합이 정확히 1이 아닐 수 있다)
    # - 모델이 없거나(로드 실패) 분석/저장이 실패해도 상담은 계속한다. 이 발화는 감정분석 없음으로 프롬프트에 들어간다.
    # - ORM 객체가 아니라 값(message_id, content)을 받는다. 실패 경로의 rollback 이후에도 안전하게 쓰기 위함
    async def _analyze_emotion(self, message_id: int, content: str) -> None:
        if self.emotion_analyzer is None:
            return

        try:
            result = await self.emotion_analyzer.analyze(content)
            await self.repo.create_emotion_analysis(
                message_id,
                model_version=result.model_version,
                dominant=result.dominant,
                **{field: Decimal(str(round(getattr(result, field), 3))) for field in EMOTION_FIELDS},
            )
            await self.repo.commit()
        except Exception:
            logger.warning("감정분석 실패. 감정 정보 없이 진행합니다. (message_id=%s)", message_id, exc_info=True)
            await self.repo.rollback()


    # 5-1. 턴 처리 내부: 프롬프트 재료 일괄 조회 (DB)
    # - 세션 원문 + 메시지별 감정분석, 체크리스트, 이 세션의 위험 감지 기록, 직전 세션 요약
    # - 반환: LLM 입력 (값만 담은 dataclass). context.checklist는 검증(_validate_llm_output)에도 쓴다.
    #   ORM 객체를 돌려주지 않는다. 이후 실패 경로의 rollback으로 만료되면 접근할 때 오류가 나기 때문이다.
    # - RAG 검색 결과(references)는 여기서 채우지 않는다. 읽기 트랜잭션을 닫은 뒤 _retrieve_references로 채운다.
    # - 위험 신호 구분 (llm_call.ReplyContext.risk_signals)
    #   - 이번 사용자 발화에 입력 검사(guard_checks gate=a) 기록이 있으면 분석한 것으로 보고 목록을 넘긴다. (감지 없으면 [])
    #   - 기록이 없으면 None ("제공되지 않음"). 분석하지 않은 발화를 위험 없음으로 넘기지 않기 위함
    # - 진료 변경 안내(care_notice): Redis에서 읽어 문장으로 바꿔 넣는다. 없거나 조회 실패면 None (_load_care_notice)
    async def _build_prompt_context(
        self, user_id: int, session_id: int, prev_session_id: int | None
    ) -> ReplyContext:
        rows = await self.repo.find_messages_with_emotions(session_id)
        checklists = await self.repo.find_session_checklist(session_id)
        risk_rows = await self.repo.find_session_risk_events(session_id)

        latest_user_message = next(
            (message for message, _ in reversed(rows) if message.role == MessageRole.USER), None
        )
        analyzed = (
            latest_user_message is not None
            and await self.repo.exists_input_guard_check(latest_user_message.id)
        )
        risk_signals = self._to_risk_signals(risk_rows) if analyzed else None

        previous_summary: str | None = None
        if prev_session_id is not None:
            summary = await self.repo.find_session_summary(prev_session_id)
            previous_summary = summary.context_summary if summary else None

        return ReplyContext(
            conversation=self._to_conversation(rows),
            checklist=self._to_checklist_entries(checklists),
            risk_signals=risk_signals,
            references=None,
            previous_summary=previous_summary,
            care_notice=await self._load_care_notice(user_id),
            user_turns=sum(1 for message, _ in rows if message.role == MessageRole.USER),
            min_user_turns=MIN_USER_TURNS,
        )


    # 5-1-0. 진료 변경 안내 조회 (Redis) -> 프롬프트용 한국어 문장
    # - care_notice_service가 없거나 안내가 없으면 None
    # - 조회가 실패해도 상담은 계속한다. (RAG 실패 처리와 같은 방식, 안내는 다음 턴에 다시 시도된다)
    async def _load_care_notice(self, user_id: int) -> str | None:
        if self.care_notice_service is None:
            return None

        try:
            notice = await self.care_notice_service.get_notice(user_id)
            return self._to_care_notice_text(notice) if notice else None
        except Exception:
            logger.exception("진료 변경 안내 조회 실패. 안내 없이 진행합니다. (user_id=%s)", user_id)
            return None


    # 5-1-0-1. 안내 dict -> 사람이 읽을 수 있는 문장 (순수 함수)
    # - 예약 일시는 KST 기준 "M월 D일 HH:MM"
    @staticmethod
    def _to_care_notice_text(notice: dict) -> str:
        sentences = ["담당 선생님이 더 이상 진료를 이어갈 수 없게 되었습니다."]

        appointment_times = [
            f"{at.month}월 {at.day}일 {at:%H:%M}"
            for at in (
                datetime.fromisoformat(value).astimezone(KST)
                for value in notice.get("cancelled_appointments", [])
            )
        ]
        if appointment_times:
            sentences.append(f"{', '.join(appointment_times)} 예약이 취소되었습니다.")
        if notice.get("cancelled_request"):
            sentences.append("진행 중이던 진료 요청도 취소되었습니다.")

        sentences.append("새 병원 연결은 진료 요청 화면에서 다시 할 수 있습니다.")
        return " ".join(sentences)


    # 5-1-0-2. 진료 변경 안내 삭제 (답변 저장 성공 후)
    # - 삭제가 실패해도 응답은 정상으로 내려준다. (다음 턴에 안내가 한 번 더 들어갈 수 있다)
    async def _delete_care_notice(self, user_id: int) -> None:
        if self.care_notice_service is None:
            return

        try:
            await self.care_notice_service.delete_notice(user_id)
        except Exception:
            logger.exception("진료 변경 안내 삭제 실패 (user_id=%s)", user_id)


    # 5-1-1. RAG 검색 (턴당 1번, LLM 재시도와 관계없이)
    # - 질의: 최근 사용자 발화 RAG_QUERY_USER_TURNS개를 합친 문장
    # - 실패해도 상담은 계속한다. None을 돌려주면 프롬프트에 "제공되지 않음"으로 들어간다.
    # - 검색이 연 읽기 트랜잭션은 여기서 닫는다. (LLM 호출 중 연결을 붙잡지 않기 위함)
    # - 검색 범위 규칙: 세션에 지정된 문서(session_documents)가 없으면 전체 RAG 문서, 있으면 그 문서들만
    #   기본은 "모든 세션이 전체 문서를 쓴다"이고, 의사가 범위를 좁히고 싶을 때만 session_documents에 넣는다.
    #   세션 생성 때 전체 문서를 미리 연결하지 않는 이유: session_documents.document_id가 ondelete=RESTRICT라
    #   연결된 문서는 RAG 재적재(replace)·정리(prune) 때 지울 수 없고, 나중에 추가된 문서도 반영되지 않기 때문
    async def _retrieve_references(
        self, conversation: list[ConversationMessage], document_ids: list[int]
    ) -> list[ReferenceChunk] | None:
        user_texts = [message.content for message in conversation if message.role == MessageRole.USER.value]
        query = "\n".join(user_texts[-RAG_QUERY_USER_TURNS:])

        try:
            chunks = await self.rag_service.retrieve(query, document_ids=document_ids or None)
            await self.repo.commit()
        except Exception:
            logger.warning("RAG 검색 실패. 참고 자료 없이 진행합니다.", exc_info=True)
            await self.repo.rollback()
            return None

        return [
            ReferenceChunk(source=chunk.source, title=chunk.title, content=chunk.content)
            for chunk in chunks
        ]


    # 5-1-2. ORM -> LLM 입력 변환 (순수 함수)
    @staticmethod
    def _to_emotion(emotion: EmotionAnalysis | None) -> EmotionScores | None:
        if emotion is None:
            return None
        dominant = getattr(emotion.dominant, "value", emotion.dominant)
        return EmotionScores(
            **{field: float(getattr(emotion, field)) for field in EMOTION_FIELDS},
            dominant=dominant,
        )


    @classmethod
    def _to_conversation(
        cls, rows: list[tuple[Message, EmotionAnalysis | None]]
    ) -> list[ConversationMessage]:
        return [
            ConversationMessage(
                seq=message.seq,
                role=MessageRole(message.role).value,
                content=message.content,
                emotion=cls._to_emotion(emotion),
            )
            for message, emotion in rows
        ]


    @staticmethod
    def _to_checklist_entries(checklists: list[SessionChecklist]) -> list[ChecklistEntry]:
        return [
            ChecklistEntry(
                item_id=checklist.item_id,
                label=checklist.item.label,
                instruction=checklist.item.instruction,
                answer_type=getattr(checklist.item.answer_type, "value", checklist.item.answer_type),
                options=checklist.item.options,
                required=checklist.required,
                status=ChecklistStatus(checklist.status).value,
                ask_count=checklist.ask_count,
                skip_reason=(
                    SessionChecklistSkipReason(checklist.skip_reason).value
                    if checklist.skip_reason is not None else None
                ),
            )
            for checklist in checklists
        ]


    @staticmethod
    def _to_risk_signals(risk_rows: list[tuple[Any, int]]) -> list[RiskSignal]:
        return [
            RiskSignal(
                message_seq=seq,
                level=getattr(event.level, "value", event.level),
                category=event.category,
                evidence=event.evidence,
                is_uncertain=event.is_uncertain,
            )
            for event, seq in risk_rows
        ]


    # 5-2. 턴 처리 내부: LLM 호출 1회 (재시도 루프는 send_message가 돈다)
    # - context: _build_prompt_context + _retrieve_references로 만든 입력 (시도마다 같다)
    # - feedback: 이전 시도의 검증 실패 사유 (첫 호출은 None)
    # - 반환: (파싱된 출력, create_llm_call에 넘길 호출 기록). 파싱에 실패하면 출력은 None
    # - llm_call.call_reply는 예외를 던지지 않는다. 호출 기록 키는 LlmCall 컬럼명과 같다.
    #   (parse_status와 validation_errors는 send_message가 채운다)
    async def _call_llm(
        self,
        context: ReplyContext,
        feedback: str | None,
        attempt: int,
    ) -> tuple[LlmReplyOutput | None, dict[str, Any]]:
        return await llm_call.call_reply(replace(context, feedback=feedback, attempt=attempt))


    # 5-3. 턴 처리 내부: LLM 출력 검증
    # - item_id가 이 세션 항목인지, 상태 전이, value와 answer_type, skip_reason, evidence_seq(사용자 메시지) 확인
    # - checklists: 프롬프트에 넣은 체크리스트 스냅샷 (context.checklist, 값만 담은 dataclass)
    # - user_turns: 이번 메시지를 포함한 사용자 발화 수 (종료 조건 판정용)
    # - 종료 요청(counsel_end)도 여기서 확인한다. 조건이 안 되면 재시도시켜서,
    #   마무리 인사가 담긴 답변이 저장·전송되지 않게 한다. (세션은 ACTIVE인데 환자는 작별 인사를 보는 상황 방지)
    # - 반환: 오류 목록 (비어 있으면 통과)
    async def _validate_llm_output(
        self,
        session_id: int,
        output: LlmReplyOutput,
        checklists: list[ChecklistEntry],
        user_turns: int,
    ) -> list[str]:
        errors: list[str] = []
        checklist_by_item = {checklist.item_id: checklist for checklist in checklists}

        # 1. 이번 턴에 질문한 항목
        for item_id in output.asked_item_ids:
            if item_id not in checklist_by_item:
                errors.append(f"asked_item_ids의 {item_id}는 이 세션의 체크리스트 항목이 아닙니다.")

        # 2. 상태가 바뀐 항목
        seen_item_ids: set[int] = set()
        for change in output.checklist_changes:
            item_id = change.item_id
            checklist = checklist_by_item.get(item_id)
            if checklist is None:
                errors.append(f"item_id {item_id}는 이 세션의 체크리스트 항목이 아닙니다.")
                continue
            if item_id in seen_item_ids:
                errors.append(f"item_id {item_id}가 checklist_changes에 중복되었습니다.")
                continue
            seen_item_ids.add(item_id)

            # 상태 전이: done은 최종 상태다. 이미 받은 답을 덮어쓰지 않는다.
            # (pending -> done/skipped, skipped -> done/skipped는 허용. 건너뛴 항목도 나중에 답하면 done이 될 수 있다)
            if not self._is_allowed_transition(checklist.status):
                errors.append(
                    f"item_id {item_id}는 이미 done인 항목입니다. 이미 done인 항목은 checklist_changes에 넣지 마세요."
                )
                continue

            if change.status == "skipped":
                # skipped: skip_reason 필수, value는 null
                if change.skip_reason is None:
                    errors.append(f"item_id {item_id}: skipped이면 skip_reason이 필요합니다.")
                if change.value is not None:
                    errors.append(f"item_id {item_id}: skipped이면 value는 null이어야 합니다.")
            else:
                # done: skip_reason은 null, value와 근거 메시지(evidence_seq) 필수
                if change.skip_reason is not None:
                    errors.append(f"item_id {item_id}: done이면 skip_reason은 null이어야 합니다.")
                if change.value is None:
                    errors.append(f"item_id {item_id}: done이면 value가 필요합니다.")
                else:
                    value_error = self._check_value(checklist, change.value)
                    if value_error:
                        errors.append(f"item_id {item_id}: {value_error}")
                if change.evidence_seq is None:
                    errors.append(f"item_id {item_id}: done이면 근거가 된 evidence_seq가 필요합니다.")

            # evidence_seq: 이 세션의 사용자 메시지여야 한다.
            if change.evidence_seq is not None:
                evidence = await self.repo.find_message_by_seq(session_id, change.evidence_seq)
                if evidence is None or evidence.role != MessageRole.USER:
                    errors.append(f"item_id {item_id}: evidence_seq {change.evidence_seq}는 이 세션의 사용자 메시지가 아닙니다.")

        # 3. 종료 요청의 일관성: counsel_end가 true일 때만 end_reason이 있다.
        if output.counsel_end and output.end_reason is None:
            errors.append("counsel_end가 true이면 end_reason이 필요합니다.")
        if not output.counsel_end and output.end_reason is not None:
            errors.append("counsel_end가 false이면 end_reason은 null이어야 합니다.")

        # 4. 종료 조건: 이번 턴의 checklist_changes까지 반영했을 때 종료할 수 있는지
        if output.counsel_end and output.end_reason == "safety":
            errors.append(
                "안전 문제를 이유로 상담을 종료하지 않습니다. counsel_end는 false, end_reason은 null로 두고 대화를 이어가세요."
            )
        elif output.counsel_end and output.end_reason == "completed":
            blockers = self._end_blockers(checklists, output, user_turns)
            if blockers:
                errors.append(
                    "아직 상담을 종료할 수 없습니다. (" + " / ".join(blockers) + ") "
                    "마무리 인사를 하지 말고 counsel_end는 false, end_reason은 null로 두어 대화를 이어가세요."
                )

        return errors


    # 5-3-3. 검증 내부: 종료를 막는 사유 목록 (비어 있으면 종료 가능, 순수 함수)
    # - 프롬프트 스냅샷에 이번 출력의 checklist_changes를 덮어써서 "이번 턴이 저장된 뒤"의 상태로 판단한다.
    #   (이미 done인 항목은 바뀌지 않는다. 잘못된 변경은 위의 검증에서 따로 오류가 된다)
    # - 완료 규칙은 _is_required_item_closed를 _is_checklist_complete(저장 시 최종 확인)와 함께 쓴다.
    @classmethod
    def _end_blockers(
        cls,
        checklists: list[ChecklistEntry],
        output: LlmReplyOutput,
        user_turns: int,
    ) -> list[str]:
        changes = {change.item_id: change for change in output.checklist_changes}
        blockers: list[str] = []

        if user_turns < MIN_USER_TURNS:
            blockers.append(f"사용자 발화가 {user_turns}회로 최소 {MIN_USER_TURNS}회에 못 미칩니다")

        open_items: list[str] = []
        for checklist in checklists:
            if not checklist.required:
                continue
            status, skip_reason = checklist.status, checklist.skip_reason
            change = changes.get(checklist.item_id)
            if change is not None and cls._is_allowed_transition(status):
                status = change.status
                skip_reason = change.skip_reason.value if change.skip_reason is not None else None
            if not cls._is_required_item_closed(status, skip_reason):
                open_items.append(f"{checklist.item_id}({checklist.label})")
        if open_items:
            blockers.append("아직 끝나지 않은 필수 항목: " + ", ".join(open_items))

        return blockers


    # 5-3-0. 검증/저장 공통: 현재 상태에서 checklist_changes로 바꿀 수 있는지
    # - done은 최종 상태라 바꿀 수 없다. pending, skipped에서는 done/skipped로 바꿀 수 있다.
    # - status는 스냅샷의 문자열("done")과 ORM의 ChecklistStatus 둘 다 받는다.
    @staticmethod
    def _is_allowed_transition(from_status: ChecklistStatus | str) -> bool:
        return ChecklistStatus(from_status) != ChecklistStatus.DONE


    # 5-3-1. 검증 내부: value가 항목의 answer_type과 응답 범위에 맞는지 (맞으면 None, 아니면 사유)
    # - 응답 범위는 checklist_items.options(글로벌 체크리스트의 "발생 가능한 응답 범위")에 있다. (checklist.options)
    # - answer_type별 value 규칙과 options 형식
    #   - boolean: true / false                    | options: null
    #   - number : 숫자(정수·실수), min <= 값 <= max | options: {"min": 1, "max": 30}  (일반 질문)
    #   - scale  : 정수, min <= 값 <= max           | options: {"min": 0, "max": 9}   (척도 기반 검사)
    #   - choice : label 중 하나 (문자열 그대로)      | options: {"label": ["a", "b", "c"]}
    #   - text   : 비어 있지 않은 문자열              | options: null (도메인 검증 없음)
    # - bool은 Python에서 int의 하위 타입이라 number·scale에서 따로 거절한다. (true가 1로 통과하지 않게)
    # - 숫자 문자열("5")은 변환하지 않고 거절한다. 사유가 retry_feedback으로 전달되어 재시도에서 고쳐진다.
    # - options에 min/max/label이 없으면(데이터 누락) 그 범위 검사는 건너뛰고 타입만 확인한다.
    #   잘못 등록된 항목 때문에 턴 전체가 실패하지 않게 하기 위함
    @staticmethod
    def _check_value(checklist: ChecklistEntry, value: Any) -> str | None:
        answer_type = ChecklistItemAnswerType(checklist.answer_type)
        options = checklist.options if isinstance(checklist.options, dict) else {}
        shown = json.dumps(value, ensure_ascii=False)

        if answer_type == ChecklistItemAnswerType.BOOLEAN:
            if not isinstance(value, bool):
                return f"answer_type이 boolean이면 value는 true 또는 false여야 합니다. (받은 값: {shown})"
            return None

        if answer_type in (ChecklistItemAnswerType.NUMBER, ChecklistItemAnswerType.SCALE):
            if answer_type == ChecklistItemAnswerType.SCALE:
                if isinstance(value, bool) or not isinstance(value, int):
                    return f"answer_type이 scale이면 value는 정수여야 합니다. (받은 값: {shown})"
            elif isinstance(value, bool) or not isinstance(value, (int, float)):
                return f"answer_type이 number이면 value는 숫자여야 합니다. 문자열이나 true/false로 보내지 마세요. (받은 값: {shown})"

            low, high = options.get("min"), options.get("max")
            if isinstance(low, (int, float)) and value < low:
                return f"value {shown}가 options의 min({low})보다 작습니다."
            if isinstance(high, (int, float)) and value > high:
                return f"value {shown}가 options의 max({high})보다 큽니다."
            return None

        if answer_type == ChecklistItemAnswerType.CHOICE:
            if not isinstance(value, str) or not value.strip():
                return f"answer_type이 choice이면 value는 options의 label 중 하나인 문자열이어야 합니다. (받은 값: {shown})"
            labels = options.get("label")
            if isinstance(labels, list) and value not in labels:
                allowed = json.dumps(labels, ensure_ascii=False)
                return f"value {shown}는 options의 label {allowed}에 없습니다. label 중 하나를 그대로 쓰세요."
            return None

        # text
        if not isinstance(value, str) or not value.strip():
            return f"answer_type이 text이면 value는 비어 있지 않은 문자열이어야 합니다. (받은 값: {shown})"
        return None


    # 5-3-2. 턴 처리·사후요약 공통: 출력이 없을 때(parsed is None) 재시도 사유 (순수 함수)
    # - 반환: 모델에 전달할 오류 목록. None이면 재시도하지 않는다.
    # - success(스키마 검증 실패): llm_call이 남긴 필드별 사유를 그대로 전달한다.
    # - truncated(max_tokens 잘림): 더 짧게 쓰라고 전달한다.
    # - timeout, error, refusal: 모델이 고칠 수 있는 문제가 아니므로 None
    #   (네트워크 계열은 SDK 재시도로 끝내고, 거절은 같은 입력이면 다시 거절될 가능성이 높다)
    @staticmethod
    def _parse_failure_errors(record: dict[str, Any]) -> list[str] | None:
        status = record.get("status")
        if status == llm_call.STATUS_SUCCESS:
            return [f"응답이 지정된 JSON 스키마를 따르지 않았습니다. {record.get('error') or ''}".strip()]
        if status == llm_call.STATUS_TRUNCATED:
            return ["출력이 길이 제한에서 잘려 JSON이 완성되지 않았습니다. 문장을 더 짧게 쓰고 스키마의 필드만 채우세요."]
        return None


    # 5-4. 턴 처리 내부: 재호출용 실패 사유 문구 (순수 함수)
    def _build_retry_feedback(self, errors: list[str]) -> str:
        lines = "\n".join(f"- {error}" for error in errors)
        return f"직전 응답에 다음 문제가 있었습니다. 모두 고쳐서 다시 응답하세요.\n{lines}"


    # 5-5. 턴 처리 내부: 모든 시도가 실패했을 때 프론트에 내려줄 오류 문구 (순수 함수)
    def _build_fallback_reply(self) -> str:
        return FALLBACK_REPLY_TEXT


    # 5-6. 턴 처리 내부: 체크리스트 상태 반영 + 질문 횟수 증가 (asked_item_ids)
    # - _validate_llm_output을 통과한 출력만 받는다. (item_id, 상태, 값은 검증이 끝난 상태)
    # - session_checklist.value 형식
    #   - done: {"message_id": 근거 사용자 메시지 id, "answer": answer_type에 맞춘 답변 값}
    #   - skipped: null
    # - evidence_message_id 컬럼에도 같은 메시지 id를 채운다. (value 안의 message_id와 둘 다)
    async def _apply_checklist_changes(
        self,
        session_id: int,
        output: LlmReplyOutput,
        checklists: list[SessionChecklist],
    ) -> None:
        checklist_by_item = {checklist.item_id: checklist for checklist in checklists}

        for change in output.checklist_changes:
            # 검증은 LLM 호출 전 스냅샷으로 했으므로, 그사이 다른 턴이 이 항목을 done으로 만들었을 수 있다.
            # 세션을 다시 잠근 뒤 읽은 현재 상태로 한 번 더 확인하고, done이면 덮어쓰지 않고 건너뛴다.
            checklist = checklist_by_item[change.item_id]
            if not self._is_allowed_transition(checklist.status):
                logger.warning(
                    "이미 done인 체크리스트 항목 변경을 건너뜁니다. (session_id=%s, item_id=%s)",
                    session_id, change.item_id,
                )
                continue

            evidence_message_id = None
            if change.evidence_seq is not None:
                evidence = await self.repo.find_message_by_seq(session_id, change.evidence_seq)
                evidence_message_id = evidence.id if evidence else None

            if change.status == "done":
                value = {"message_id": evidence_message_id, "answer": change.value}
                skip_reason = None
            else:
                value = None
                skip_reason = change.skip_reason

            await self.repo.record_checklist_change(
                checklist,
                to_status=ChecklistStatus(change.status),
                value=value,
                skip_reason=skip_reason,
                evidence_message_id=evidence_message_id,
            )

        # 같은 항목을 두 번 적었어도 이번 턴의 질문은 1회
        await self.repo.increment_ask_count(session_id, list(set(output.asked_item_ids)))


    # 5-7. 턴 처리 내부: 필수 항목이 모두 done 또는 허용된 skipped인지 (순수 함수)
    # - counsel_end는 요청일 뿐이고, 종료는 이 결과(DB 상태)와 MIN_USER_TURNS로 service가 확정한다.
    def _is_checklist_complete(self, checklists: list[SessionChecklist]) -> bool:
        return all(
            self._is_required_item_closed(checklist.status, checklist.skip_reason)
            for checklist in checklists
            if checklist.required
        )


    # 5-7-1. 종료 판정 공통: 필수 항목 1건이 끝났는지 (done, 또는 허용된 사유로 skipped)
    # - 검증(_end_blockers, 문자열 스냅샷)과 저장 시 최종 확인(_is_checklist_complete, ORM enum)이 같은 규칙을 쓴다.
    @staticmethod
    def _is_required_item_closed(
        status: ChecklistStatus | str, skip_reason: SessionChecklistSkipReason | str | None
    ) -> bool:
        status = ChecklistStatus(status)
        if status == ChecklistStatus.DONE:
            return True
        return (
            status == ChecklistStatus.SKIPPED
            and skip_reason is not None
            and SessionChecklistSkipReason(skip_reason) in ALLOWED_REQUIRED_SKIP_REASONS
        )


    # 5-8. 턴 처리 내부: 재전송 시 기존 AI 답변 조회
    # - 재전송된 사용자 메시지(seq) 뒤에 나온 첫 번째 ASSISTANT 메시지. 없으면 None
    async def _find_reply(self, session_id: int, user_message: Message) -> Message | None:
        return await self.repo.find_reply(session_id, user_message.seq)


    # 5-9. 턴 처리 내부: 트랜잭션 2 저장 묶음 (LLM 출력이 검증을 통과한 턴)
    # - 체크리스트 반영, 답변 메시지 저장, llm_calls 저장, 종료 처리, commit
    # - 답변 메시지가 먼저 생기므로 llm_calls는 message_id를 바로 채워 저장한다. (link_llm_calls 불필요)
    # - 체크리스트는 세션을 다시 잠근 상태에서 새로 읽어 반영한다.
    #   (LLM 호출 전에 읽은 ORM 객체는 만료됐거나 다른 요청 때문에 낡았을 수 있다)
    async def _save_turn_result(
        self,
        session: ChatSession,
        output: LlmReplyOutput,
        llm_call_records: list[dict[str, Any]],
    ) -> Message:
        # 1. 체크리스트 반영
        checklists = await self.repo.find_session_checklist(session.id)
        await self._apply_checklist_changes(session.id, output, checklists)

        # 2. 답변 메시지 저장
        reply = await self.repo.create_message(
            session.id,
            MessageRole.ASSISTANT,
            output.reply_text,
            expression=output.expression.value,
            system_record_status=MessageSystemRecordStatus.OK,
        )

        # 3. 호출 기록 저장
        for record in llm_call_records:
            await self.repo.create_llm_call(message_id=reply.id, **record)

        # 4. 종료 판정: counsel_end는 요청일 뿐. DB의 체크리스트 상태와 최소 턴 수로 확정한다.
        # - 종료 조건은 검증(_end_blockers)에서 이미 확인했다. 여기서는 잠근 상태의 DB로 한 번 더 확인한다. (최종 확인)
        # - 검증을 통과한 종료 요청은 end_reason="completed"뿐이다. ("safety"는 검증에서 거절)
        if output.counsel_end and output.end_reason == "completed":
            fresh_checklists = await self.repo.find_session_checklist(session.id)
            user_turns = await self.repo.count_user_messages(session.id)
            if self._is_checklist_complete(fresh_checklists) and user_turns >= MIN_USER_TURNS:
                await self.repo.update_session(
                    session,
                    status=ChatSessionStatus.ENDED,
                    end_reason=ChatSessionEndReason.COMPLETED,
                    ended_at=datetime.now(KST),
                    summary_status=ChatSessionSummaryStatus.PENDING,
                )

        await self.repo.commit()
        return reply


    # 5-10. 턴 처리 내부: 모든 시도가 실패한 턴
    # - 오류 문구는 messages에 저장하지 않고 502 응답의 detail로만 내려준다. (프론트가 채팅창에 표시)
    # - llm_calls.message_id는 "이 호출이 설명하는 assistant 메시지"이고 장애일 때는 null이다. (모델 주석)
    #   답변 메시지가 없으므로 이 턴의 호출 기록은 message_id=None으로 남긴다.
    # - 답변이 없으므로 같은 client_message_id로 재전송하면 409가 된다. (프론트는 새 id로 보내야 한다)
    async def _save_failed_turn(self, llm_call_records: list[dict[str, Any]]) -> NoReturn:
        try:
            for record in llm_call_records:
                await self.repo.create_llm_call(message_id=None, **record)
            await self.repo.commit()
        except Exception as error:
            await self._rollback_and_raise(error)

        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=self._build_fallback_reply()
        )


    # 5-11. 턴 처리 내부: 잠금 조회 (본인 세션, ACTIVE 상태 확인)
    # - 없거나 타인 세션이면 404, ACTIVE가 아니면 409
    # - 호출한 쪽이 commit 또는 rollback으로 잠금을 푼다.
    async def _get_locked_active_session(self, user_id: int, session_id: int) -> ChatSession:
        session = await self.repo.find_session_by_id(session_id, user_id=user_id, for_update=True)
        if session is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="세션이 존재하지 않습니다."
            )
        if session.status != ChatSessionStatus.ACTIVE:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="진행 중인 세션이 아닙니다."
            )
        return session


    # 6. 세션 사후요약 생성 (세션당 1건)
    # - 대상: status=ENDED이고 summary_status=PENDING인 세션. 대상이 아니면 아무것도 하지 않고 None
    # - 호출 위치: 세션 종료 응답 뒤 BackgroundTasks -> run_session_summary (새 AsyncSession으로 만든 LlmService)
    #   나중에 배치로 PENDING을 정리하게 되더라도 이 함수 하나로 처리한다.
    # - 저장 형식은 session_summaries 모델을 따른다.
    #   - context_summary(text), topics(JSONB, 키워드 배열), findings(JSONB, [{label, evidence}]): LLM 출력 그대로
    #   - chief_complaint(text): 사전 문진(INTAKE) 세션만 저장한다. 그 밖의 세션은 LLM이 보내도 None
    #   - mood_summary(JSONB, NOT NULL) = {dominant, distribution, intensity, evidence_seqs}
    #     dominant, distribution: 사용자 메시지의 감정분석 6종 평균으로 service가 계산한다. (_build_mood_summary)
    #     intensity, evidence_seqs: LLM 출력(mood)
    #     감정분석이 한 건도 없으면(감정분석 모델 적용 전) 네 값이 모두 비어 있는 구조로 저장한다. (NOT NULL이라)
    # - send_message처럼 LLM 호출을 잠금 밖에서 한다.
    #   [트랜잭션 1: 세션 잠금 -> 대상 확인 -> 원문 + 감정분석 조회 -> commit]
    #   [LLM 호출 / 검증 루프 (최대 MAX_LLM_ATTEMPTS회)]
    #   [트랜잭션 2: 재잠금 -> 호출 기록 저장 -> 대상 재확인 -> 요약 저장 + READY, 또는 FAILED -> commit]
    # - 동시에 두 번 불리면 LLM은 두 번 호출될 수 있지만, 트랜잭션 2의 재확인 때문에 요약 저장은 한 번만 된다.
    # - 실패하면 summary_status=FAILED만 남기고 요약은 저장하지 않는다. (FAILED 재시도는 아직 없다)
    # - 호출 기록은 llm_calls에 purpose=SUMMARY, message_id=None으로 남긴다.
    async def create_session_summary(self, session_id: int) -> SessionSummary | None:
        # --- 트랜잭션 1: 잠금 -> 대상 확인 -> 원문 + 감정분석 조회 ---
        try:
            session = await self.repo.find_session_by_id(session_id, for_update=True)
            if not self._is_summary_target(session):
                await self.repo.commit() # 변경 없이 잠금만 푼다.
                return None

            rows = await self.repo.find_messages_with_emotions(session_id)
            if not rows:
                # 요약할 대화가 없다.
                await self.repo.update_session(session, summary_status=ChatSessionSummaryStatus.SKIPPED)
                await self.repo.commit()
                return None

            session_type = session.session_type
            await self.repo.commit() # 잠금 해제. 이후 LLM 호출 동안 세션을 잠그지 않는다.
        except Exception as error:
            await self._rollback_and_raise(error)

        summary_input = self._build_summary_input(rows)
        has_emotion = any(item["emotion"] is not None for item in summary_input)

        # --- LLM 호출 / 검증 루프 ---
        output: LlmSummaryOutput | None = None
        llm_call_records: list[dict[str, Any]] = []
        feedback: str | None = None

        for attempt in range(1, MAX_LLM_ATTEMPTS + 1):
            parsed, record = await self._call_summary_llm(
                summary_input, session_type, has_emotion, feedback, attempt
            )

            if parsed is None:
                errors = self._parse_failure_errors(record)
                if errors is None:
                    # 타임아웃·네트워크·API 오류·거절: 다시 호출하지 않는다. (send_message와 같은 규칙)
                    record["validation_errors"] = None
                    record["parse_status"] = LlmCallParseStatus.FAILED
                    llm_call_records.append(record)
                    break
            else:
                errors = self._validate_summary_output(parsed, summary_input, has_emotion)

            # parse_status는 NOT NULL이라 모든 기록에 채운다. (send_message와 같은 규칙)
            record["validation_errors"] = errors or None
            if errors:
                record["parse_status"] = LlmCallParseStatus.FAILED
            else:
                record["parse_status"] = LlmCallParseStatus.OK if attempt == 1 else LlmCallParseStatus.RETRIED_OK
            llm_call_records.append(record)

            if not errors:
                output = parsed
                break
            feedback = self._build_retry_feedback(errors)

        # --- 트랜잭션 2: 재잠금 -> 호출 기록 저장 -> 대상 재확인 -> 요약 저장 또는 FAILED ---
        try:
            session = await self.repo.find_session_by_id(session_id, for_update=True)

            # 호출 기록은 요약 저장 여부와 관계없이 남긴다. (실제로 호출했으므로)
            for record in llm_call_records:
                await self.repo.create_llm_call(message_id=None, **record)

            if not self._is_summary_target(session):
                # 그 사이 다른 호출이 먼저 처리했다.
                await self.repo.commit()
                return None

            if output is None:
                await self.repo.update_session(session, summary_status=ChatSessionSummaryStatus.FAILED)
                await self.repo.commit()
                return None

            summary = await self.repo.upsert_session_summary(
                session_id,
                context_summary=output.context_summary,
                mood_summary=self._build_mood_summary(summary_input, output, has_emotion),
                topics=output.topics,
                chief_complaint=(
                    output.chief_complaint if session_type == ChatSessionType.INTAKE else None
                ),
                findings=[finding.model_dump() for finding in output.findings],
            )
            await self.repo.update_session(session, summary_status=ChatSessionSummaryStatus.READY)
            await self.repo.commit()
            return summary
        except Exception as error:
            await self._rollback_and_raise(error)


    # 6-1. 사후요약 내부: 요약 대상인지 (순수 함수)
    @staticmethod
    def _is_summary_target(session: ChatSession | None) -> bool:
        return (
            session is not None
            and session.status == ChatSessionStatus.ENDED
            and session.summary_status == ChatSessionSummaryStatus.PENDING
        )


    # 6-2. 사후요약 내부: LLM 입력 만들기 (순수 함수)
    # - 메시지마다 seq, role, content를 넣는다. role이 있어야 환자의 말과 AI의 답변을 구분할 수 있다.
    # - emotion: 감정분석 6종 점수(Decimal -> float). 감정분석이 없는 메시지(AI 답변, 분석 전)는 None
    @staticmethod
    def _build_summary_input(
        rows: list[tuple[Message, EmotionAnalysis | None]],
    ) -> list[dict[str, Any]]:
        summary_input: list[dict[str, Any]] = []
        for message, emotion in rows:
            summary_input.append({
                "seq": message.seq,
                "role": MessageRole(message.role).value,
                "content": message.content,
                "emotion": (
                    {field: float(getattr(emotion, field)) for field in EMOTION_FIELDS}
                    if emotion is not None else None
                ),
            })
        return summary_input


    # 6-3. 사후요약 내부: LLM 호출 1회 (재시도 루프는 create_session_summary가 돈다)
    # - _call_llm과 같은 계약: 예외를 던지지 않고 (파싱된 출력 또는 None, 호출 기록)을 반환한다.
    # - 호출 기록 필드: purpose=LlmCallPurpose.SUMMARY, model, prompt_version, attempt, tokens_in, tokens_out,
    #   latency_ms, status, error (prompt_version, status는 NOT NULL. parse_status와 validation_errors는 호출한 쪽이 채운다)
    # - session_type이 INTAKE일 때만 chief_complaint를 요청하고, 아니면 null로 내라고 지시한다.
    # - has_emotion이 False면 mood를 null로 내라고 지시한다.
    # - 프롬프트: app/llm_call/prompts/session_summary_system_prompt.txt (출력은 LlmSummaryOutput 스키마)
    async def _call_summary_llm(
        self,
        summary_input: list[dict[str, Any]],
        session_type: ChatSessionType,
        has_emotion: bool,
        feedback: str | None,
        attempt: int,
    ) -> tuple[LlmSummaryOutput | None, dict[str, Any]]:
        conversation = [
            ConversationMessage(
                seq=item["seq"],
                role=item["role"],
                content=item["content"],
                emotion=EmotionScores(**item["emotion"]) if item["emotion"] is not None else None,
            )
            for item in summary_input
        ]
        return await llm_call.call_summary(
            SummaryContext(
                conversation=conversation,
                session_type=ChatSessionType(session_type).value,
                has_emotion=has_emotion,
                feedback=feedback,
                attempt=attempt,
            )
        )


    # 6-4. 사후요약 내부: LLM 출력 검증 (오류 목록 반환, 비어 있으면 통과)
    # - context_summary가 비어 있지 않은지, findings의 label이 비어 있지 않은지
    # - 감정분석이 있는데 mood가 없으면 재요청한다. (감정분석이 없을 때 온 mood는 저장할 때 버린다)
    # - mood.evidence_seqs는 이 세션의 사용자 메시지 seq여야 한다.
    @staticmethod
    def _validate_summary_output(
        output: LlmSummaryOutput,
        summary_input: list[dict[str, Any]],
        has_emotion: bool,
    ) -> list[str]:
        errors: list[str] = []

        if not output.context_summary.strip():
            errors.append("context_summary가 비어 있습니다.")
        for index, finding in enumerate(output.findings):
            if not finding.label.strip():
                errors.append(f"findings[{index}]의 label이 비어 있습니다.")

        if has_emotion:
            if output.mood is None:
                errors.append("감정분석 데이터가 있으므로 mood가 필요합니다.")
            else:
                user_seqs = {
                    item["seq"] for item in summary_input if item["role"] == MessageRole.USER.value
                }
                for seq in output.mood.evidence_seqs:
                    if seq not in user_seqs:
                        errors.append(f"mood.evidence_seqs의 {seq}는 이 세션의 사용자 메시지가 아닙니다.")

        return errors


    # 6-5. 사후요약 내부: mood_summary 만들기 (순수 함수)
    # - distribution: 감정분석이 있는 메시지들의 6종 평균 (소수 셋째 자리 반올림)
    # - dominant: distribution에서 가장 높은 감정
    # - intensity, evidence_seqs: LLM 출력
    # - 감정분석이 없으면 모두 비운 구조를 돌려준다. (mood_summary는 NOT NULL)
    @staticmethod
    def _build_mood_summary(
        summary_input: list[dict[str, Any]],
        output: LlmSummaryOutput,
        has_emotion: bool,
    ) -> dict[str, Any]:
        scores = [item["emotion"] for item in summary_input if item["emotion"] is not None]
        if not has_emotion or not scores or output.mood is None:
            return {"dominant": None, "distribution": None, "intensity": None, "evidence_seqs": []}

        distribution = {
            field: round(sum(score[field] for score in scores) / len(scores), 3)
            for field in EMOTION_FIELDS
        }
        return {
            "dominant": max(distribution, key=distribution.get),
            "distribution": distribution,
            "intensity": output.mood.intensity,
            "evidence_seqs": output.mood.evidence_seqs,
        }


    # 7. 세션 마감 정리: 한 번도 시작하지 않고 expires_at이 지난 예정 세션 -> MISSED
    # - 세션 종료 조건은 세 가지다.
    #   (1) LLM 출력의 정상 종료 신호 -> send_message의 종료 판정 (ENDED / COMPLETED)
    #   (2) 상담 중 이탈 -> 프론트가 end API 호출, end_session (ENDED / USER)
    #   (3) 시작한 적 없이 expires_at이 지남 -> 이 함수 (MISSED)
    # - 스케줄러 없이 "요청이 들어올 때" 처리한다. 세션 상태를 프론트에 보여 줘야 하는 요청의 맨 앞에서 부른다.
    #   지금은 get_main_session(환자 상담 메인)에서 부른다. 세션 목록·상태를 보여 주는 API(예: 의사용 세션 조회)가
    #   생기면 그 앞에서도 부른다. (의사 화면이면 user_id=환자 id)
    # - 기능상 접속은 이 함수 없이도 막힌다. (start_session이 expires_at을 400으로 막고, 상담 메인 조회도 마감 지난 세션을 제외)
    #   이 함수는 DB의 status를 실제 상태와 맞추는 역할이다.
    # - user_id를 주면 그 환자의 세션만 정리한다. None이면 전체 (나중에 배치로 돌릴 때)
    # - 변경한 세션 수를 반환한다.
    async def process_session_timeouts(self, user_id: int | None = None) -> int:
        try:
            count = await self.repo.expire_scheduled_sessions(datetime.now(KST), user_id=user_id)
            await self.repo.commit()
        except Exception as error:
            await self._rollback_and_raise(error)
        return count


    # 방치 세션 처리 (예외 상황, 지금은 다루지 않는다)
    # - 설계: AI 상담은 접속하면 끝날 때까지 진행하고, 중간에 나가면 세션을 종료한다. (end_session)
    # - expires_at(필수)은 최초 접속(start_session) 때 한 번만 검증한다. 시작한 뒤에는 expires_at을 다시 검증하지 않는다.
    # - 세션 상태(status)는 외부 표시용이다. 접속 가능 여부의 검증 기준으로 쓰지 않는다.
    # TODO: 이탈 통지가 오지 않는 경우(앱 강제 종료, 네트워크 끊김)는 아직 처리하지 않는다.
    #   그런 세션은 ACTIVE로 남아, 환자당 ACTIVE 1개 제약(exists_active_session, uix_chat_sessions_user_active) 때문에
    #   다음 세션 시작(start_session)이 409가 된다. expires_at은 시작할 때만 검증하므로 7번에서도 잡히지 않는다.
    #   처리 방안 후보: 마지막 메시지 이후 일정 시간이 지나면 ENDED / TIMEOUT으로 정리,
    #   또는 start_session에서 남아 있는 ACTIVE 세션을 먼저 닫고 새 세션을 연다.



# 세션 사후요약 백그라운드 실행 (세션 종료 응답을 보낸 뒤 BackgroundTasks로 호출)
# - 요청의 DB 세션은 응답 후 닫히므로, 새 AsyncSession으로 LlmService를 따로 만든다.
# - 사후요약은 감정분석·RAG를 쓰지 않는다. (rag_service는 생성자 인자라 넘기기만 한다)
# - 백그라운드 작업이라 예외를 밖으로 던지지 않고 로그만 남긴다.
#   LLM 실패는 create_session_summary가 summary_status=FAILED로 남긴다. 그 밖의 오류(DB 등)면 PENDING으로 남는다.
async def run_session_summary(session_id: int) -> None:
    try:
        async with AsyncSessionLocal() as db:
            service = LlmService(LlmRepository(db), RagService(RagRepository(db)))
            await service.create_session_summary(session_id)
    except Exception:
        logger.exception("세션 사후요약 생성 실패 (session_id=%s)", session_id)


# 레포지토리 주입 팩토리 함수
def get_llm_service(
    repo: LlmRepository = Depends(get_llm_repository),
    rag_service: RagService = Depends(get_rag_service),
    emotion_analyzer: EmotionAnalyzer | None = Depends(get_emotion_analyzer),
    care_notice_service: CareNoticeService = Depends(get_care_notice_service),
):
    return LlmService(repo, rag_service, emotion_analyzer, care_notice_service)
