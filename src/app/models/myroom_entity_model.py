from datetime import datetime, timedelta, timezone
from sqlalchemy import BigInteger, Float, DateTime, ForeignKey, CheckConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

# PostgreSQL의 Base를 상속받아서 서버가 켜질 때 동기화시킨다.
from app.infrastructure.postgresql import Base
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.user_model import User
    from app.models.item_model import Item

KST = timezone(timedelta(hours=9)) # UTC+9

# 마이룸에 배치한 가구 - (회원, 아이템)이 PK라 같은 가구는 회원당 1개만 놓인다.
# 가진 아이템인지(user_items)는 배치할 때 서비스에서 확인한다.
class MyroomEntity(Base):
    __tablename__ = "myroom_entities"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )  # 회원 ID
    holding_item_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("items.item_id", ondelete="RESTRICT"), primary_key=True
    )  # 배치한 아이템 ID
    x: Mapped[float] = mapped_column(Float, nullable=False)  # 가로 위치 (방 너비 대비 비율 0.0~1.0)
    y: Mapped[float] = mapped_column(Float, nullable=False)  # 세로 위치 (방 높이 대비 비율 0.0~1.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(KST)
    )  # 배치한 시각
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        onupdate=lambda: datetime.now(KST)
    )  # 마지막으로 옮긴 시각

    __table_args__ = (
        CheckConstraint("x >= 0 AND x <= 1", name="ck_myroom_entities_x_ratio"),
        CheckConstraint("y >= 0 AND y <= 1", name="ck_myroom_entities_y_ratio"),
    )

    # 관계
    user: Mapped["User"] = relationship("User", back_populates="myroom_entities")
    item: Mapped["Item"] = relationship("Item")
