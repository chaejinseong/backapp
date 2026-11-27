from fastapi import Depends
from app.repository.mood_record_repository import MoodRecordRepository, get_mood_record_repository
from datetime import date, timedelta
from fastapi import HTTPException, status
from app.models.emotion_record_model import EmotionRecord
from app.util.date_util import today_kst, now_kst
from app.schema.mood_record_schema import (
    MoodRecordCreateDTO,
    MoodRecordUpdateDTO,
    MoodRecordResponseDTO,
    MoodRecordDetailResponseDTO,
)

PAST_DAYS_LIMIT = 7   # 지난 날짜는 7일 전까지 기록 가능 (일기와 같게)

# 마음 기록 (마음 날씨/키워드/한 줄) 비즈니스 로직, 예외 처리, 트랜잭션 관리
class MoodRecordService:

    def __init__(self, repo: MoodRecordRepository):
        self.repo = repo

    # TODO: 서비스 메서드 추가
    
    #날짜 확인: 미래 X, 7일 보다 지난 날 X
    @staticmethod
    def _check_date(day:date) -> None:
        today = today_kst()
        if day>today: 
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail = "미래 날짜는 기록할 수 없습니다"           
            )
        if day < today - timedelta(days=PAST_DAYS_LIMIT):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{PAST_DAYS_LIMIT}일 전까지만 기록할 수 있습니다."
            )
        

    #ORM -> 응답 DTO (칼럼 이름과 DTO 필드 이름이 같아서 그대로 옮겨짐)
    @staticmethod
    def _to_response(record: EmotionRecord) -> MoodRecordResponseDTO:
        return MoodRecordResponseDTO.model_validate(record)

    #번호로 내 기록 찾기. 없으면(남은 거, 지운 거. 포함) 404
    async def _get_or_404(self, user_id:int, emotion_id: int) -> EmotionRecord:
        record = await self.repo.find_by_id(user_id, emotion_id)
        if record is None:
            raise HTTPException(
                status_code = status.HTTP_404_NOT_FOUND,
                detail = "마음 기록을 찾을 수 없습니다."
            )
        return record

    #----기능-----
    #1. 날짜로 내 기록 1개(없으면 None)
    async def get_by_date(self, user_id: int, day: date) -> MoodRecordResponseDTO | None:
        record = await self.repo.find_by_date(user_id,day)
        return self._to_response(record) if record else None
    
    #2. 오늘 기록 ('오늘 마음 날씨'카드)
    async def get_today(self,user_id: int) -> MoodRecordResponseDTO | None:
        return await self.get_by_date(user_id, today_kst())

    #3. 날짜별 상세 (기록 + 이날 한 돌봄)
    async def get_detail(self,user_id: int, day:date) -> MoodRecordDetailResponseDTO:
        return MoodRecordDetailResponseDTO(
            record=await self.get_by_date(user_id, day),
            done_missions=await self.repo.find_done_mission_names(user_id,day)
        )
    #4. 기록하기 - 하루 1건, 지운 날이면 지운 기록을 되살려 덮어쓴다
    async def create(self, user_id: int, dto: MoodRecordCreateDTO) -> MoodRecordResponseDTO:
        day= dto.record_date or today_kst()
        self._check_date(day)

        existing = await self.repo.find_by_date(user_id, day, include_deleted=True)
        if existing and existing.deleted_at is None:
            raise HTTPException(
                status_code = status.HTTP_409_CONFLICT,
                detail = "이미 마음을 기록한 날입니다. 수정해 주세요"
            )
        #선생님께 보여주기 처음 값 = 내 정보 '마음 리포트 공유'설정(설정이 없으면 공유)
        share_report = await self.repo.find_share_report(user_id)
        is_shared = True if share_report is None else share_report

        if existing:
            # 지운 기록 되살리기 (하루 1줄 규칙 때문에 새로 넣지 않고 덮어쓴다)
            existing.mood_score = dto.mood_score
            existing.mood_tag = dto.mood_tag
            existing.memo = dto.memo
            existing.is_shared = is_shared
            existing.deleted_at = None
            existing.created_at = now_kst()
            record = await self.repo.save(existing)
        else:
            record = await self.repo.insert(EmotionRecord(
                user_id=user_id,
                record_date=day,
                mood_score=dto.mood_score,
                mood_tag=dto.mood_tag,
                memo=dto.memo,
                is_shared=is_shared
            ))
        #TODO : 오늘 기록이면 '마음 기록' 목표 자동 완료

        return self._to_response(record)

    # 5. 수정하기 · 선생님께 보여 주기 - 보낸 값만 바꾼다
    async def update(
        self, user_id: int, emotion_id: int, dto: MoodRecordUpdateDTO
    ) -> MoodRecordResponseDTO:
        record = await self._get_or_404(user_id, emotion_id)

        changes = dto.model_dump(exclude_unset=True)   # 앱이 실제로 보낸 값만
        if not changes:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="바꿀 내용이 없습니다."
            )
        if changes.get("mood_score", 0) is None or changes.get("is_shared", False) is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="마음 날씨와 공유 설정은 비울 수 없습니다."
            )

        for field, value in changes.items():
            setattr(record, field, value)

        return self._to_response(await self.repo.save(record))


    # 6. 지우기 - 실제로 지우지 않고 지운 시각만 남긴다
    async def delete(self, user_id: int, emotion_id: int) -> None:
        record = await self._get_or_404(user_id, emotion_id)
        record.deleted_at = now_kst()
        await self.repo.save(record)




def get_mood_record_service(repo: MoodRecordRepository = Depends(get_mood_record_repository)) -> MoodRecordService:
    return MoodRecordService(repo)
