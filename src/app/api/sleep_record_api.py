from fastapi import APIRouter, Depends, Query, status
from app.service.sleep_record_service import get_sleep_record_service, SleepRecordService
from app.schema.user_schema import UserClaimDTO
from app.schema.common_schema import ApiResponseDTO, PageRequestDTO, PageResponseDTO
from app.schema.sleep_record_schema import SleepRecordCreateDTO, SleepRecordUpdateDTO, SleepRecordResponseDTO
from app.dependencie.auth_dependency import get_current_user


router = APIRouter()


# 수면 기록 작성
@router.post(
    "",
    summary="수면 기록 작성",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponseDTO[SleepRecordResponseDTO]
)
async def create_sleep_record(
    dto: SleepRecordCreateDTO,
    current_user: UserClaimDTO = Depends(get_current_user),
    service: SleepRecordService = Depends(get_sleep_record_service)
):
    data = await service.create_sleep_record(current_user.id, dto)
    return ApiResponseDTO(success=True, message="수면 기록 작성 완료", data=data)


# 수면 기록 목록 (기상한 날 최신순)
@router.get(
    "",
    summary="수면 기록 목록",
    response_model=ApiResponseDTO[PageResponseDTO[SleepRecordResponseDTO]]
)
async def get_sleep_records(
    month: str | None = Query(default=None, description="YYYY-MM 형식, 없으면 전체"),
    page: PageRequestDTO = Depends(),
    current_user: UserClaimDTO = Depends(get_current_user),
    service: SleepRecordService = Depends(get_sleep_record_service)
):
    data = await service.get_sleep_records(current_user.id, page, month)
    return ApiResponseDTO(success=True, message="수면 기록 목록 조회 완료", data=data)


# 수면 기록 상세
@router.get(
    "/{sleep_id}",
    summary="수면 기록 상세",
    response_model=ApiResponseDTO[SleepRecordResponseDTO]
)
async def get_sleep_record(
    sleep_id: int,
    current_user: UserClaimDTO = Depends(get_current_user),
    service: SleepRecordService = Depends(get_sleep_record_service)
):
    data = await service.get_sleep_record(current_user.id, sleep_id)
    return ApiResponseDTO(success=True, message="수면 기록 조회 완료", data=data)


# 수면 기록 수정 (보낸 값만 수정)
@router.patch(
    "/{sleep_id}",
    summary="수면 기록 수정",
    response_model=ApiResponseDTO[SleepRecordResponseDTO]
)
async def update_sleep_record(
    sleep_id: int,
    dto: SleepRecordUpdateDTO,
    current_user: UserClaimDTO = Depends(get_current_user),
    service: SleepRecordService = Depends(get_sleep_record_service)
):
    data = await service.update_sleep_record(current_user.id, sleep_id, dto)
    return ApiResponseDTO(success=True, message="수면 기록 수정 완료", data=data)


# 수면 기록 삭제
@router.delete(
    "/{sleep_id}",
    summary="수면 기록 삭제",
    response_model=ApiResponseDTO
)
async def delete_sleep_record(
    sleep_id: int,
    current_user: UserClaimDTO = Depends(get_current_user),
    service: SleepRecordService = Depends(get_sleep_record_service)
):
    await service.delete_sleep_record(current_user.id, sleep_id)
    return ApiResponseDTO(success=True, message="수면 기록 삭제 완료")
