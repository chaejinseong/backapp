from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends
from app.infrastructure.postgresql import get_postgresql_db
from datetime import date
from sqlalchemy import select, func
# 모델, ORM
from app.models.mission_model import Mission
from app.models.user_mission_log_model import UserMissionLog
from app.enums.mission_enum import MissionType


# 오늘의 작은 돌봄 / 나만의 돌봄 (미션과 같은 개념으로 묶어서 처리) DB 접근
class CareTaskRepository:

    # 생성자 주입
    def __init__(self, db: AsyncSession):
        self.db = db

    # TODO: 조회/저장 쿼리 추가
    # 함수 내에서 commit하지 않는다. flush까지만
    # 포인트 잔액 + 포인트 내역 + 완료 기록을 한 번에 저장해야 해서, commit 시점은 service가 정함
    async def commit(self) -> None:
        await self.db.commit()

    # 같은 미션 완료가 동시에 들어와 유니크 제약에 걸렸을 때 되돌린다
    async def rollback(self) -> None:
        await self.db.rollback()

    #1. 사용 중인 목표 목록 (표시 순서대로) - 일일목표 또는 보너스
    async def find_active_missions(self, mission_type: MissionType) -> list[Mission]:
         query = (
              select(Mission)
              .where(Mission.mission_type == mission_type, Mission.is_active.is_(True))
              .order_by(Mission.sort_order, Mission.mission_id)
         )
         result = await self.db.execute(query)
         return list(result.scalars().all())

    #2. 목표 1개
    async def find_mission(self, mission_id: int) -> Mission | None:
         query = select(Mission).where(Mission.mission_id == mission_id)
         result = await self.db.execute(query)
         return result.scalar_one_or_none()
         
    #3. 사용 중인 모두 완료 보너스 1개
    async def find_bonus_mission(self) -> Mission|None:
         missions = await self.find_active_missions(MissionType.ALL_CLEAR_BONUS)
         return missions[0] if missions else None

    #4. 그날 내 수행 기록 전부 (오늘 할 일 목록에 완료 여부를 붙일 때)
    async def find_logs_on(self, user_id: int, day:date) -> list[UserMissionLog]:
         query = select(UserMissionLog).where(
              UserMissionLog.user_id == user_id,
              UserMissionLog.log_date == day
         )
         result = await self.db.execute(query)
         return list(result.scalars().all())

    #5. 그날 목표 1개의 수행 기록(하루1줄)
    async def find_log(self, user_id: int, mission_id: int, day: date) -> UserMissionLog | None:
        query = select(UserMissionLog).where(
            UserMissionLog.user_id == user_id,
            UserMissionLog.mission_id == mission_id,
            UserMissionLog.log_date == day
        )
        result = await self.db.execute(query)
        return result.scalar_one_or_none()

    #6. 수행 기록 새로 만들기(flush하면 log_id가 채워진다)
    # 취소가 없으므로 기록은 한 번 만들면 바꾸지 않는다. (user_id, mission_id, log_date) 유니크라 중복 완료는 DB가 막는다.
    async def add_log(self, log: UserMissionLog) -> UserMissionLog:
         self.db.add(log)
         await self.db.flush()
         return log
    
    #7. 기간 [start,end] 날짜별 완료 개수 - 취소한 것 제외, mission_ids 안의 목표만(이번주 돌아보기)
    async def count_completed_by_day(
              self, user_id: int, mission_ids: list[int], start:date, end:date
    ) -> dict[date,int]:
         if not mission_ids:
              return{}
         query = (
              select(UserMissionLog.log_date, func.count())
              .where(
                   UserMissionLog.user_id == user_id,
                   UserMissionLog.mission_id.in_(mission_ids),
                   UserMissionLog.log_date>=start,
                   UserMissionLog.log_date<end,
                   UserMissionLog.completed_at.is_not(None),
                   UserMissionLog.canceled_at.is_(None)
              )
              .group_by(UserMissionLog.log_date)
         )
         result = await self.db.execute(query)
         return {log_date: count for log_date, count in result.all()}
        


def get_care_task_repository(db: AsyncSession = Depends(get_postgresql_db)) -> CareTaskRepository:
    return CareTaskRepository(db)
