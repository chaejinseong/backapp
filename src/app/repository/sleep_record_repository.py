from datetime import datetime
from sqlalchemy import select, update, delete, func
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends
from app.infrastructure.postgresql import get_postgresql_db

# 모델, ORM
from app.models.sleep_record_model import SleepRecord
from app.enums.sleep_record_enum import SleepRecordSource


# 수면 기록 DB 접근 (조회/저장만 담당, 비즈니스 규칙은 service에서 처리)
class SleepRecordRepository:

    # 생성자 주입
    def __init__(self, db: AsyncSession):
        self.db = db


    # 1. 수면 기록 작성
    async def create_sleep_record(
        self, user_id: int, record_date: datetime, sleep_at: datetime, wake_at: datetime,
        sleep_minutes: int, source: SleepRecordSource
    ) -> SleepRecord:
        record = SleepRecord(
            user_id=user_id,
            record_date=record_date,
            sleep_at=sleep_at,
            wake_at=wake_at,
            sleep_minutes=sleep_minutes,
            source=source
        )
        self.db.add(record)
        await self.db.commit()
        await self.db.refresh(record)
        return record


    # 2. 수면 기록 목록 - 기간(start 이상 end 미만) 선택, 기상한 날 최신순
    async def find_sleep_records(
        self, user_id: int, offset: int, limit: int,
        start: datetime | None = None, end: datetime | None = None
    ) -> tuple[list[SleepRecord], int]:
        conditions = [SleepRecord.user_id == user_id]
        if start is not None:
            conditions.append(SleepRecord.record_date >= start)
        if end is not None:
            conditions.append(SleepRecord.record_date < end)

        total = await self.db.scalar(select(func.count()).select_from(SleepRecord).where(*conditions))
        query = (
            select(SleepRecord)
            .where(*conditions)
            .order_by(SleepRecord.record_date.desc(), SleepRecord.sleep_id.desc())
            .offset(offset)
            .limit(limit)
        )
        result = await self.db.execute(query)
        return list(result.scalars().all()), total or 0


    # 3. 수면 기록 단건 조회 - 내 기록만
    async def find_sleep_record(self, user_id: int, sleep_id: int) -> SleepRecord | None:
        query = select(SleepRecord).where(
            SleepRecord.sleep_id == sleep_id,
            SleepRecord.user_id == user_id
        )
        result = await self.db.execute(query)
        return result.scalar_one_or_none()


    # 4. 같은 날(기상한 날) 기록 조회 - 하루 1개 중복 검사용
    async def find_by_record_date(self, user_id: int, record_date: datetime) -> SleepRecord | None:
        query = select(SleepRecord).where(
            SleepRecord.user_id == user_id,
            SleepRecord.record_date == record_date
        ).limit(1)
        result = await self.db.execute(query)
        return result.scalar_one_or_none()


    # 5. 수면 기록 수정
    async def update_sleep_record(self, user_id: int, sleep_id: int, update_data: dict) -> SleepRecord | None:
        if not update_data:
            return await self.find_sleep_record(user_id, sleep_id)

        query = (
            update(SleepRecord)
            .where(SleepRecord.sleep_id == sleep_id, SleepRecord.user_id == user_id)
            .values(**update_data)
            .returning(SleepRecord)
        )
        result = await self.db.execute(query)
        updated = result.scalar_one_or_none()
        await self.db.commit()
        return updated


    # 6. 수면 기록 삭제 (hard delete: 모델에 deleted_at이 없다.)
    async def delete_sleep_record(self, user_id: int, sleep_id: int) -> bool:
        query = delete(SleepRecord).where(
            SleepRecord.sleep_id == sleep_id,
            SleepRecord.user_id == user_id
        )
        result = await self.db.execute(query)
        await self.db.commit()
        return result.rowcount > 0



def get_sleep_record_repository(db: AsyncSession = Depends(get_postgresql_db)) -> SleepRecordRepository:
    return SleepRecordRepository(db)
