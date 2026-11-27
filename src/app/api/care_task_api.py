from fastapi import APIRouter, Depends
from app.service.care_task_service import get_care_task_service, CareTaskService
from app.schema.user_schema import UserClaimDTO
from app.schema.common_schema import ApiResponseDTO
from app.schema.care_task_schema import TodayCareTaskResponseDTO
from app.dependencie.auth_dependency import get_current_user


router = APIRouter()

# 오늘의 일일목표 (미션 목록 + 미션별 오늘 완료 여부 is_completed + 모두 완료 보너스)
# - 미션 달성 처리는 API가 아니라 각 기능 서비스(일기, 상담 등)가 complete_mission을 불러서 한다.
@router.get(
    "/today",
    summary="오늘의 일일목표",
    response_model=ApiResponseDTO[TodayCareTaskResponseDTO]
)
async def get_today_care_tasks(
    current_user: UserClaimDTO = Depends(get_current_user),
    service: CareTaskService = Depends(get_care_task_service)
):
    data = await service.get_today(current_user.id)
    return ApiResponseDTO(success=True, message="오늘의 일일목표 조회 완료", data=data)
