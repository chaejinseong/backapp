from datetime import date
from fastapi import Depends
from sqlalchemy.exc import IntegrityError
from app.repository.care_task_repository import CareTaskRepository, get_care_task_repository
from app.service.point_service import PointService, get_point_service
from app.models.user_mission_log_model import UserMissionLog
from app.enums.mission_enum import MissionType
from app.schema.care_task_schema import (
    MissionCompleteResponseDTO,
    CareTaskItemResponseDTO,
    TodayCareTaskResponseDTO,
)
from app.util.date_util import today_kst, now_kst


# 완료 판정 - 완료 시각이 있고 취소되지 않음 (calendar_service.is_completed와 같은 기준)
def _is_completed(log: UserMissionLog) -> bool:
    return log.completed_at is not None and log.canceled_at is None


# 오늘의 작은 돌봄 / 나만의 돌봄 (미션과 같은 개념으로 묶어서 처리) 비즈니스 로직, 예외 처리, 트랜잭션 관리
class CareTaskService:

    def __init__(self, repo: CareTaskRepository, point_service: PointService):
        self.repo = repo
        self.point_service = point_service


    # 포인트 지급 + 완료 기록 1줄 (flush까지만, commit은 complete_mission이 한다)
    async def _earn_and_log(self, user_id: int, mission_id: int, reward_point: int, reason: str, today: date) -> None:
        history = await self.point_service.earn(user_id, reward_point, mission_id, reason)
        await self.repo.add_log(UserMissionLog(
            user_id=user_id,
            mission_id=mission_id,
            history_id=history.history_id,
            log_date=today,
            progress_count=1,
            completed_at=now_kst()
        ))


    # 1. 오늘의 일일목표 (미션 목록 + 오늘 완료 여부)
    # - 사용 중인 일일목표 전체와 오늘(KST) 내 기록을 각각 한 번씩 조회해서 mission_id로 맞춘다.
    # - 오늘 완료한 기록이 있는 미션은 is_completed=True, 없으면 False
    async def get_today(self, user_id: int) -> TodayCareTaskResponseDTO:
        today = today_kst()
        missions = await self.repo.find_active_missions(MissionType.DAILY_GOAL)
        logs = await self.repo.find_logs_on(user_id, today)
        completed_at = {log.mission_id: log.completed_at for log in logs if _is_completed(log)}

        items = [
            CareTaskItemResponseDTO(
                mission_id=m.mission_id,
                name=m.name,
                description=m.description,
                category=m.category,
                reward_point=m.reward_point,
                is_completed=m.mission_id in completed_at,
                completed_at=completed_at.get(m.mission_id)
            )
            for m in missions
        ]
        bonus = await self.repo.find_bonus_mission()

        return TodayCareTaskResponseDTO(
            today=today,
            missions=items,
            completed_count=sum(item.is_completed for item in items),
            total_count=len(items),
            bonus_point=bonus.reward_point if bonus else None,
            bonus_received=bonus is not None and bonus.mission_id in completed_at,
            earned_today=await self.point_service.earned_on(user_id, today)
        )


    # 2. 미션 달성 처리
    # - 기능 서비스(일기, 상담 등)가 자기 작업을 commit한 뒤 부른다. 이 함수도 끝에서 commit한다.
    # - 순서: 미션 검증 -> 포인트 지급(회원 잠금) + 완료 기록 -> 모두 완료 확인 -> 보너스 지급 + 기록 -> commit
    # - earn이 회원을 잠그므로 같은 회원의 미션 완료는 하나씩 처리된다.
    #   그래서 "모두 완료" 확인을 earn 뒤에 하면, 동시에 마지막 미션 두 개를 끝내도 보너스는 정확히 한 번 나간다.
    async def complete_mission(self, user_id: int, mission_id: int) -> MissionCompleteResponseDTO | None:
        # 미션 검증 - 통과한 미션만 포인트 지급으로 넘어간다
        mission = await self.repo.find_mission(mission_id)
        if mission is None or mission.mission_type != MissionType.DAILY_GOAL:
            # 기능 서비스가 코드에 적어 넘기는 값이라, 틀리면 코드 오류로 본다
            raise ValueError(f"일일목표 미션이 아닙니다: {mission_id}")
        if not mission.is_active:
            # 기능 자체는 이미 성공했으므로 예외를 내지 않는다
            return None
        reward_point, reason = mission.reward_point, mission.name

        today = today_kst()
        earned_point = 0
        newly_completed = False
        bonus_completed = False

        if await self.repo.find_log(user_id, mission_id, today) is None:
            try:
                await self._earn_and_log(user_id, mission_id, reward_point, reason, today)
                earned_point += reward_point
                newly_completed = True
            except IntegrityError:
                # 잠금을 기다리는 사이 다른 요청이 같은 미션을 먼저 완료했다.
                # rollback하면 방금 올린 잔액·내역도 같이 되돌아가므로 이중 지급은 없다.
                await self.repo.rollback()

        # 오늘 완료한 화면 미션 수 (회원 잠금을 잡은 뒤라 다른 요청이 commit한 기록까지 보인다)
        daily_ids = {m.mission_id for m in await self.repo.find_active_missions(MissionType.DAILY_GOAL)}
        logs = await self.repo.find_logs_on(user_id, today)
        completed_count = len(daily_ids & {log.mission_id for log in logs if _is_completed(log)})

        # 모두 완료 보너스 - 이번 요청으로 미션을 완료했고, 화면 미션이 다 끝났고, 보너스 기록이 아직 없을 때만
        bonus = await self.repo.find_bonus_mission()
        all_cleared = bool(daily_ids) and completed_count == len(daily_ids)
        if newly_completed and bonus and all_cleared and bonus.mission_id not in {log.mission_id for log in logs}:
            await self._earn_and_log(user_id, bonus.mission_id, bonus.reward_point, bonus.name, today)
            earned_point += bonus.reward_point
            bonus_completed = True

        await self.repo.commit()

        return MissionCompleteResponseDTO(
            mission_id=mission_id,
            newly_completed=newly_completed,
            completed_count=completed_count,
            total_count=len(daily_ids),
            bonus_completed=bonus_completed,
            earned_point=earned_point
        )


def get_care_task_service(
    repo: CareTaskRepository = Depends(get_care_task_repository),
    point_service: PointService = Depends(get_point_service)
) -> CareTaskService:
    return CareTaskService(repo, point_service)
