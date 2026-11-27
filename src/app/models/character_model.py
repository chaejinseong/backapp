from datetime import date, datetime, timedelta, timezone
from sqlalchemy import BigInteger, String, Integer, SmallInteger, Date, DateTime, Identity, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

# PostgreSQL의 Base를 상속받아서 서버가 켜질 때 동기화시킨다.
from app.infrastructure.postgresql import Base
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.user_model import User
    from app.models.flower_model import Flower
    from app.models.expression_model import Expression
    from app.models.character_position_model import CharacterPosition

KST = timezone(timedelta(hours=9)) # UTC+9

class Character(Base):
    __tablename__ = "characters"

    character_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)  # 캐릭터 ID
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )  # 주인 회원 ID
    current_flower_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("flowers.flower_id", ondelete="SET NULL")
    )  # 지금 키우는 꽃 (16종 다 피우면 마지막 꽃 그대로)
    mood_expression_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("expressions.expression_id", ondelete="SET NULL")
    )  # 오늘의 기분 (category=mood 중 랜덤)
    # 의상·소품·배경 슬롯은 폐기된 컨셉. 마이룸 가구는 myroom_entities, 캐릭터 위치는 character_positions에 둔다.
    name: Mapped[str | None] = mapped_column(String(30))  # 캐릭터 이름
    dirtiness: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)  # 꼬질함 0~100. 2일마다 +10
    hunger: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)  # 배고픔 0~100. 하루마다 +10
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        onupdate=lambda: datetime.now(KST)
    )  # 상태 변경 시각
    mood_date: Mapped[date | None] = mapped_column(Date)  # 오늘과 다르면 다음 상호작용 때 새로 정함
    level: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)  # 1 새싹 2 생장 3 꽃봉오리 4 만개
    exp: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # 경험치 (단계별 필요 값 미정)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(KST)
    )  # 만든 시각
    dirtiness_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(KST)
    )  # 마지막 씻김·꼬질함 증가 시각
    hunger_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(KST)
    )  # 마지막 간식·배고픔 증가 시각

    # 관계
    user: Mapped["User"] = relationship("User", back_populates="characters")
    current_flower: Mapped["Flower | None"] = relationship("Flower")
    mood_expression: Mapped["Expression | None"] = relationship("Expression")
    position: Mapped["CharacterPosition | None"] = relationship(
        "CharacterPosition", back_populates="character", uselist=False,
        cascade="all, delete-orphan", passive_deletes=True
    )  # 마이룸 안 캐릭터 위치 (1:1)
