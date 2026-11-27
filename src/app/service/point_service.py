from datetime import date
from fastapi import Depends, HTTPException, status
from app.repository.point_repository import PointRepository, get_point_repository
from app.models.point_history_model import PointHistory
from app.enums.point_history_enum import PointHistoryType
from app.util.date_util import today_kst, now_kst, kst_day_range, add_months
from app.schema.point_schema import (
    PointSummaryResponseDTO,
    PointHistoryResponseDTO,
    PointHistoryListResponseDTO,
)

# 포인트 기록 화면 탭 → 보여 줄 내역 종류 (획득 탭에는 취소도 같이 보인다)
TAB_TYPES = {
    "all": [PointHistoryType.EARN, PointHistoryType.SPEND, PointHistoryType.CANCEL],
    "earn": [PointHistoryType.EARN, PointHistoryType.CANCEL],
    "spend": [PointHistoryType.SPEND],
}

# point_histories.reason 컬럼 길이 (String(50))
REASON_MAX_LENGTH = 50


# 포인트 비즈니스 로직, 예외 처리
# - earn / spend 는 다른 서비스(오늘 할 일, 포인트샵, 자장가)가 부른다.
# - 이 두 함수는 commit하지 않는다. 부른 서비스가 자기 일까지 끝낸 뒤 한 번에 commit한다.
class PointService:

    def __init__(self, repo: PointRepository):
        self.repo = repo


    # 잔액을 바꾸기 전에 회원을 잠가서 가져온다. 없으면 404
    async def _lock_user_or_404(self, user_id: int):
        user = await self.repo.lock_user(user_id)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="회원이 존재하지 않습니다."
            )
        return user


    # 금액·사유 검증 - 다른 서비스가 넘기는 값이라 잘못되면 코드 오류(ValueError)로 본다
    @staticmethod
    def _validate(amount: int, reason: str) -> None:
        if amount <= 0:
            raise ValueError(f"포인트 금액은 1 이상이어야 합니다: {amount}")
        if len(reason) > REASON_MAX_LENGTH:
            raise ValueError(f"사유는 {REASON_MAX_LENGTH}자 이하여야 합니다: {reason}")


    # 1. 포인트 얻기 (일일목표 완료, 모두 완료 보너스)
    # - 회원을 잠그고 잔액 증가 + 내역 추가까지 flush한다. commit은 부른 서비스가 한다.
    # - 돌려주는 내역의 history_id를 미션 완료 기록(user_mission_logs.history_id)에 연결한다.
    async def earn(self, user_id: int, amount: int, mission_id: int | None, reason: str) -> PointHistory:
        self._validate(amount, reason)
        user = await self._lock_user_or_404(user_id)
        user.point_balance += amount

        return await self.repo.insert_history(PointHistory(
            user_id=user_id,
            history_type=PointHistoryType.EARN,
            amount=amount,
            balance_after=user.point_balance,
            mission_id=mission_id,
            reason=reason,
            base_date=now_kst()
        ))


    # 2. 포인트 쓰기 (포인트샵 구매, 자장가) - 잔액이 모자라면 400
    # - 회원을 잠근 뒤 잔액을 확인하므로, 동시에 두 번 사도 잔액이 음수가 되지 않는다.
    # - commit은 부른 서비스가 아이템 지급까지 끝낸 뒤 한다.
    async def spend(self, user_id: int, amount: int, item_id: int | None, reason: str) -> PointHistory:
        self._validate(amount, reason)
        user = await self._lock_user_or_404(user_id)
        if user.point_balance < amount:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="포인트가 부족합니다."
            )
        user.point_balance -= amount

        return await self.repo.insert_history(PointHistory(
            user_id=user_id,
            history_type=PointHistoryType.SPEND,
            amount=amount,
            balance_after=user.point_balance,
            item_id=item_id,
            reason=reason,
            base_date=now_kst()
        ))

    # 3. 그날 모은 포인트 = 얻음 − 취소 (60P 상한 확인용)
    async def earned_on(self, user_id: int, day: date) -> int:
        start, end = kst_day_range(day)
        earned = await self.repo.sum_amount(user_id, PointHistoryType.EARN, start, end)
        canceled = await self.repo.sum_amount(user_id, PointHistoryType.CANCEL, start, end)
        return earned - canceled


    # 4. 내 포인트 요약
    async def get_summary(self, user_id: int) -> PointSummaryResponseDTO:
        user = await self.repo.find_user(user_id)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="회원이 존재하지 않습니다."
            )

        today = today_kst()
        month_start = date(today.year, today.month, 1)
        start, _ = kst_day_range(month_start)
        end, _ = kst_day_range(add_months(month_start, 1))
        earned = await self.repo.sum_amount(user_id, PointHistoryType.EARN, start, end)
        canceled = await self.repo.sum_amount(user_id, PointHistoryType.CANCEL, start, end)

        return PointSummaryResponseDTO(
            point_balance=user.point_balance,
            earned_today=await self.earned_on(user_id, today),
            earned_this_month=earned - canceled
        )


    # 5. 포인트 기록 (전체·획득·사용 탭, 최신순)
    async def get_histories(
        self, user_id: int, tab: str, cursor: int | None, size: int
    ) -> PointHistoryListResponseDTO:
        rows = await self.repo.find_histories(user_id, TAB_TYPES[tab], cursor, size)
        has_next = len(rows) > size
        rows = rows[:size]

        return PointHistoryListResponseDTO(
            items=[self._to_response(row) for row in rows],
            next_cursor=rows[-1].history_id if has_next else None
        )


    # ORM -> 응답 DTO (얻음은 +, 씀·취소는 −)
    @staticmethod
    def _to_response(history: PointHistory) -> PointHistoryResponseDTO:
        sign = 1 if history.history_type == PointHistoryType.EARN else -1
        return PointHistoryResponseDTO(
            history_id=history.history_id,
            history_type=history.history_type,
            amount=sign * history.amount,
            balance_after=history.balance_after,
            reason=history.reason,
            mission_id=history.mission_id,
            item_id=history.item_id,
            created_at=history.created_at
        )


def get_point_service(repo: PointRepository = Depends(get_point_repository)) -> PointService:
    return PointService(repo)