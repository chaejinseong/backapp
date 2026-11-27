from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends
from app.infrastructure.postgresql import get_postgresql_db
from datetime import date
from sqlalchemy import select

#모델, ORM
from app.models.emotion_record_model import EmotionRecord
from app.models.user_setting_model import UserSetting
from app.models.mission_model import Mission
from app.models.user_mission_log_model import UserMissionLog
from app.enums.mission_enum import MissionType

# 마음 기록 (마음 날씨/키워드/한 줄) DB 접근
class MoodRecordRepository:

    # 생성자 주입
    def __init__(self, db: AsyncSession):
        self.db = db

    # TODO: 조회/저장 쿼리 추가

    #1. 그날 내 기록 1개
    #include_deleted=True면 지운 기록도 찾는다(지운 날 기록할 때 되살리려고)
    async def find_by_date(
            self, user_id: int, day: date, include_deleted: bool = False
    ) -> EmotionRecord | None:
        query = select(EmotionRecord).where(
            EmotionRecord.user_id == user_id,
            EmotionRecord.record_date == day
        )
        if not include_deleted:
            query= query.where(EmotionRecord.deleted_at.is_(None))
        result = await self.db.execute(query)
        return result.scalar_one_or_none()

    #2. 번호로 내 기록 1개 (안 지운 것만) - 남의 기록 번호면 None => 서비스가 404
    async def find_by_id(self, user_id: int, emotion_id: int) -> EmotionRecord | None:
        query = select(EmotionRecord).where(
            EmotionRecord.emotion_id == emotion_id,
            EmotionRecord.user_id == user_id,
            EmotionRecord.deleted_at.is_(None)
        )
        result = await self.db.execute(query)
        return result.scalar_one_or_none()

    #3. 내 정보 공유 설정의 '마음 리포트 공유' 값 (설정 줄이 없으면 None)
    async def find_share_report(self, user_id: int) -> bool | None:
        query = select(UserSetting.share_report).where(UserSetting.user_id == user_id)
        result = await self.db.execute(query)
        return result.scalar_one_or_none()

    #4. 그날 완료한 일일목표 이름 (상세 화면 '이날 한 돌봄')
    async def find_done_mission_names(self, user_id: int, day: date) -> list[str]:
        query = (
            select(Mission.name)
            .join(UserMissionLog, UserMissionLog.mission_id == Mission.mission_id)
            .where(
                UserMissionLog.user_id == user_id,
                UserMissionLog.log_date == day,
                UserMissionLog.completed_at.is_not(None),
                UserMissionLog.canceled_at.is_(None),
                Mission.mission_type == MissionType.DAILY_GOAL
            )
            .order_by(Mission.sort_order, Mission.mission_id)
        )
        result = await self.db.execute(query)
        return list(result.scalars().all())

    
    #5. 새 기록 넣고 저장
    async def insert(self, record: EmotionRecord) -> EmotionRecord:
        self.db.add(record)
        await self.db.commit()
        await self.db.refresh(record)
        return record

    #6.바꾼 기록 저장(수정, 지우기, 되돌리기) - 서비스가 값을 바꾼 뒤 부른다
    async def save(self, record:EmotionRecord) -> EmotionRecord:
        await self.db.commit()
        await self.db.refresh(record)
        return record




def get_mood_record_repository(db: AsyncSession = Depends(get_postgresql_db)) -> MoodRecordRepository:
    return MoodRecordRepository(db)
