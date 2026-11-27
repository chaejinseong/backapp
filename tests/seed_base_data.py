import asyncio
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parent.parent/"src"))

from sqlalchemy import select   #noqa: E402

import app.models   #noqa: E402, F401 (모든 모델 등록)
from app.infrastructure.postgresql import engine, Base, AsyncSessionLocal    #noqa: E402
from app.enums.mission_enum import MissionType, MissionCategory     #noqa: E402
from app.models.mission_model import Mission  # noqa: E402

# 1단계: 일일목표(합계 60p)
#daily_limit(1)  is_active(True) target)count(1)  created_at은 모델 기본값을 쓴다.
MISSIONS = [
    dict(name="마음이와 대화하기", description = "마음이에게 오늘 이야기를 들려주세요",
         mission_type = MissionType.DAILY_GOAL, category = MissionCategory.MIND, reward_point=15, sort_order = 1),     
    dict(name="마음 기록", description = "오늘 마음 날씨를 남겨요",
         mission_type = MissionType.DAILY_GOAL, category = MissionCategory.MIND, reward_point=10, sort_order = 2),
    dict(name="한 줄 일기 쓰기", description = "한 줄만 써도 충분해요",
         mission_type = MissionType.DAILY_GOAL, category = MissionCategory.MIND, reward_point=10, sort_order = 3),
    dict(name="산책 10분", description = "햇빛 쬐며 가볍게 걷기",
         mission_type = MissionType.DAILY_GOAL, category = MissionCategory.BODY, reward_point=5, sort_order = 4),
    dict(name="비타민 or 약 챙겨 먹기", description = "오늘도 잘 챙겨줘서 고마워요",
         mission_type = MissionType.DAILY_GOAL, category = MissionCategory.HABIT, reward_point=5, sort_order = 5),
    dict(name="물 6잔 마시기", description = "한 잔 마실 때마다 눌러요",
         mission_type = MissionType.DAILY_GOAL, category = MissionCategory.HABIT, reward_point=5, sort_order = 6),
    dict(name="모두 완료 보너스", description = "대단해요! 오늘 목표를 모두 해냈어요",
         mission_type = MissionType.ALL_CLEAR_BONUS, category = None, reward_point=10, sort_order = 99),

]

async def seed_mission(db) -> int:
    #이미 들어가 있는 이름은 건너뛴다
    names = [m["name"]for m in MISSIONS]
    result = await db.execute(select(Mission.name).where(Mission.name.in_(names)))
    existing = set(result.scalars().all())

    new_rows = [Mission(**m) for m in MISSIONS if m["name"] not in existing]
    db.add_all(new_rows)
    return len(new_rows)

async def main() -> None:
    #서버 시작 때와 같이 테이블이 없으면 만든다.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as db:
        added = await seed_mission(db)
        await db.commit()

        print(f"missions: {added}줄 추가 (이미 있던 것은 건너뜀)")
        await engine.dispose()

if __name__ == "__main__":
    asyncio.run(main())
