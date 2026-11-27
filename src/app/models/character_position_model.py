from datetime import datetime, timedelta, timezone
from sqlalchemy import BigInteger, Float, DateTime, ForeignKey, CheckConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

# PostgreSQL의 Base를 상속받아서 서버가 켜질 때 동기화시킨다.
from app.infrastructure.postgresql import Base
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.character_model import Character

KST = timezone(timedelta(hours=9)) # UTC+9

# 마이룸 안 캐릭터 위치 - characters와 1:1 (캐릭터 1개당 1줄)
class CharacterPosition(Base):
    __tablename__ = "character_positions"

    character_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("characters.character_id", ondelete="CASCADE"), primary_key=True
    )  # 캐릭터 ID
    x: Mapped[float] = mapped_column(Float, nullable=False)  # 가로 위치 (방 너비 대비 비율 0.0~1.0)
    y: Mapped[float] = mapped_column(Float, nullable=False)  # 세로 위치 (방 높이 대비 비율 0.0~1.0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(KST),
        onupdate=lambda: datetime.now(KST)
    )  # 마지막으로 옮긴 시각

    __table_args__ = (
        CheckConstraint("x >= 0 AND x <= 1", name="ck_character_positions_x_ratio"),
        CheckConstraint("y >= 0 AND y <= 1", name="ck_character_positions_y_ratio"),
    )

    # 관계
    character: Mapped["Character"] = relationship("Character", back_populates="position")
