from datetime import datetime
from sqlalchemy import select,func
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends
from app.infrastructure.postgresql import get_postgresql_db

#모델, ORM
from app.models.user_model import User
from app.models.point_history_model import PointHistory
from app.enums.point_history_enum import PointHistoryType

#포인트 DB접근(조회/저장만 담당, 비지니스 규칙은 service에서 처리
#- 다른 repository와 달리 함수 안에서 commit하지 않는다. (flush까지만)
# - 잔액 + 내역 (+미션 완료 기록)을 한 번에 저장해야 해서, commit 시점은 service가 commit()으로 정한다.
class PointRepository:
    #생성자 주입
    def __init__(self, db:AsyncSession):
        self.db=db

    async def commit(self)->None:
        await self.db.commit()

    #1. 회원 조회(잔액 보기용, 잠금 없음) - 탈퇴한 회원 제외
    async def find_user(self,user_id:int) -> User|None:
        query = select(User).where(User.id == user_id, User.deleted_at.is_(None))
        result = await self.db.execute(query)
        return result.scalar_one_or_none()


    #2. 회원 조회 + 잠금 (잔액을 바꾸는 동안 회원의 다른 요청은 기다린다 -> 이중 지급 및 차감 방지)
    async def lock_user(self, user_id: int) -> User|None:
        query = (
            select(User)
            .where(User.id == user_id, User.deleted_at.is_(None))
            .with_for_update()
        )
        result = await self.db.execute(query)
        return result.scalar_one_or_none()

    #3. 내역 1줄 추가 (flush하면 history_id가 채워진다)
    async def insert_history(self, history: PointHistory) -> PointHistory:
        self.db.add(history)
        await self.db.flush()
        return history

    # 4. 기간 [start, end) 안에서 한 종류(얻음·씀·취소)의 합계
    async def sum_amount(
        self, user_id: int, history_type: PointHistoryType, start: datetime, end: datetime
    ) -> int:
        query = select(func.coalesce(func.sum(PointHistory.amount), 0)).where(
            PointHistory.user_id == user_id,
            PointHistory.history_type == history_type,
            PointHistory.base_date >= start,
            PointHistory.base_date < end
        )
        result = await self.db.execute(query)
        return result.scalar_one()


    # 5. 내역 목록 (최신순). cursor가 있으면 그보다 이전 것만. 다음 페이지 확인용으로 size+1개 가져온다
    async def find_histories(
        self, user_id: int, types: list[PointHistoryType], cursor: int | None, size: int
    ) -> list[PointHistory]:
        query = select(PointHistory).where(
            PointHistory.user_id == user_id,
            PointHistory.history_type.in_(types)
        )
        if cursor is not None:
            query = query.where(PointHistory.history_id < cursor)

        query = query.order_by(PointHistory.history_id.desc()).limit(size + 1)
        result = await self.db.execute(query)
        return list(result.scalars().all())


def get_point_repository(db: AsyncSession = Depends(get_postgresql_db)) -> PointRepository:
    return PointRepository(db)
        

    