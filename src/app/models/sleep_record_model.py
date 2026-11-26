from datetime import datetime, timedelta, timezone
from sqlalchemy import BigInteger, Integer, DateTime, Identity, ForeignKey, UniqueConstraint, Enum as SQLEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.enums.sleep_record_enum import SleepRecordSource

# PostgreSQL의 Base를 상속받아서 서버가 켜질 때 동기화시킨다.
from app.infrastructure.postgresql import Base
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.user_model import User

KST = timezone(timedelta(hours=9)) # UTC+9

class SleepRecord(Base):
    __tablename__ = "sleep_records"

    sleep_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)  # 수면 기록 ID
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )  # 회원 ID
    record_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)  # 기상한 날
    sleep_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # 잠든 시각
    wake_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # 일어난 시각
    sleep_minutes: Mapped[int | None] = mapped_column(Integer)  # 수면 시간(분)
    source: Mapped[SleepRecordSource] = mapped_column(
        SQLEnum(SleepRecordSource, native_enum=False, values_callable=lambda e: [m.value for m in e]),
        nullable=False
    )  # 입력 방식
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(KST)
    )  # 작성 시각
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        onupdate=lambda: datetime.now(KST)
    )  # 수정 시각

    __table_args__ = (
        # 한 사용자는 같은 날(기상한 날)에 수면 기록을 하나만 가진다.
        UniqueConstraint("user_id", "record_date", name="uq_sleep_records_user_id_record_date"),
    )

    # 관계
    user: Mapped["User"] = relationship("User", back_populates="sleep_records")
