from datetime import date, datetime, timedelta, timezone
from sqlalchemy import BigInteger, Integer, Date, DateTime, Identity, ForeignKey, Index, Enum as SQLEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.enums.care_action_log_enum import CareActionLogActionType

# PostgreSQL의 Base를 상속받아서 서버가 켜질 때 동기화시킨다.
from app.infrastructure.postgresql import Base
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.user_model import User
    from app.models.item_model import Item

KST = timezone(timedelta(hours=9)) # UTC+9

class CareActionLog(Base):
    __tablename__ = "care_action_logs"

    log_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)  # 기록 ID
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )  # 회원 ID
    item_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("items.item_id", ondelete="SET NULL")
    )  # 간식·씻기기처럼 아이템을 쓴 경우
    action_type: Mapped[CareActionLogActionType] = mapped_column(
        SQLEnum(CareActionLogActionType, native_enum=False, values_callable=lambda e: [m.value for m in e]),
        nullable=False
    )  # snack=먹이기, wash=씻기기, play=놀아주기(하루 3번)
    action_date: Mapped[date] = mapped_column(Date, nullable=False)  # 하루 1번 확인용 ('하루' = 자정)
    exp_gain: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # 오른 경험치
    point_spent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # 쓴 포인트
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(KST)
    )  # 기록 시각

    __table_args__ = (
        # 그날 상호작용 횟수 조회용 (놀아주기 하루 3번 제한은 서비스에서 캐릭터를 잠근 뒤 개수를 세어 확인한다)
        Index("ix_care_action_logs_user_action_date", "user_id", "action_type", "action_date"),
    )

    # 관계
    user: Mapped["User"] = relationship("User", back_populates="care_action_logs")
    item: Mapped["Item | None"] = relationship("Item")
