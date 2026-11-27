from datetime import datetime, timedelta, timezone
from sqlalchemy import BigInteger, String, Integer, SmallInteger, Float, Boolean, DateTime, Identity, CheckConstraint, Enum as SQLEnum
from sqlalchemy.orm import Mapped, mapped_column
from app.enums.item_enum import ItemCategory, ItemOwnershipType
from app.enums.common_enum import Season

# PostgreSQL의 Base를 상속받아서 서버가 켜질 때 동기화시킨다.
from app.infrastructure.postgresql import Base

KST = timezone(timedelta(hours=9)) # UTC+9

class Item(Base):
    __tablename__ = "items"

    item_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)  # 아이템 ID
    name: Mapped[str] = mapped_column(String(50), nullable=False)  # 아이템 이름
    category: Mapped[ItemCategory] = mapped_column(
        SQLEnum(ItemCategory, native_enum=False, values_callable=lambda e: [m.value for m in e]),
        nullable=False
    )  # snack=간식, care=씻기기 등 돌봄 소모품
    price: Mapped[int] = mapped_column(Integer, nullable=False)  # 가격
    image_url: Mapped[str | None] = mapped_column(String(255))  # 이미지 주소
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)  # 판매 여부
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(KST)
    )  # 등록 시각
    ownership_type: Mapped[ItemOwnershipType] = mapped_column(
        SQLEnum(ItemOwnershipType, native_enum=False, values_callable=lambda e: [m.value for m in e]),
        nullable=False
    )  # stackable=개수가 쌓임, unique=한 번만 구매
    description: Mapped[str | None] = mapped_column(String(255))  # 설명
    season: Mapped[Season | None] = mapped_column(
        SQLEnum(Season, native_enum=False, values_callable=lambda e: [m.value for m in e])
    )  # 계절 아이템만. NULL이면 항상 판매
    # 상호작용 경험치 배율 (NULL이면 경험치 없음)
    # - 간식(snack)·돌봄(care): 실제로 줄인 배고픔·꼬질함 × 배율
    # - 놀아주기: 기본 경험치(무료 놀아주기 = 배율 1.0 기준) × 배율
    exp_gain: Mapped[float | None] = mapped_column(Float)
    hunger_restore: Mapped[int | None] = mapped_column(SmallInteger)  # 사용 시 배고픔 회복
    dirtiness_restore: Mapped[int | None] = mapped_column(SmallInteger)  # 사용 시 꼬질함 감소
    unlock_level: Mapped[int | None] = mapped_column(Integer)  # 계정 레벨 기준
    daily_refill: Mapped[int | None] = mapped_column(Integer)  # 기본 제공 아이템만. 매일 이 개수까지 채움
    # 마이룸 배치용 바닥 상자 크기 - 마이룸에 놓는 가구만 값이 있고, 간식·돌봄 소모품은 NULL
    # 바닥 영역 기준 비율이라 실제로 정사각형인 가구도 w와 h 값이 다를 수 있다 (w=바닥 너비 대비, h=바닥 깊이 대비)
    footprint_w: Mapped[float | None] = mapped_column(Float)  # 바닥 상자 가로 크기 (0 초과 1 이하)
    footprint_h: Mapped[float | None] = mapped_column(Float)  # 바닥 상자 세로(깊이) 크기 (0 초과 1 이하)

    __table_args__ = (
        # 둘 다 NULL이거나, 둘 다 (0, 1] 범위
        # (IS NOT NULL을 먼저 확인해야 한쪽만 NULL일 때 비교 결과가 NULL이 되어 CHECK를 통과해 버리는 것을 막는다)
        CheckConstraint(
            "(footprint_w IS NULL AND footprint_h IS NULL) OR "
            "(footprint_w IS NOT NULL AND footprint_h IS NOT NULL "
            "AND footprint_w > 0 AND footprint_w <= 1 AND footprint_h > 0 AND footprint_h <= 1)",
            name="ck_items_footprint"
        ),
    )

    # 관계
    # 기준 데이터 테이블이라 역방향 relationship은 두지 않는다.
