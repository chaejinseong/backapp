from fastapi import APIRouter, Depends
from app.service.mood_record_service import get_mood_record_service, MoodRecordService
from app.schema.user_schema import UserClaimDTO
from app.dependencie.auth_dependency import get_current_user
from fastapi import status
from datetime import date
from app.schema.common_schema import ApiResponseDTO
from app.schema.mood_record_schema import (
    MoodRecordCreateDTO,
    MoodRecordUpdateDTO,
    MoodRecordResponseDTO,
    MoodRecordDetailResponseDTO,

)

router = APIRouter()

# TODO: 마음 기록 (마음 날씨/키워드/한 줄) 엔드포인트 추가

# 1. 오늘 기록 ('오늘 마음 날씨' 카드) - 없으면 data가 null
@router.get(
    "/today",
    summary="오늘 마음 기록",
    response_model=ApiResponseDTO[MoodRecordResponseDTO]
)
async def get_today_mood_record(
    current_user: UserClaimDTO = Depends(get_current_user),
    service: MoodRecordService = Depends(get_mood_record_service)
):
    data = await service.get_today(current_user.id)
    return ApiResponseDTO(success=True, message="오늘 마음 기록 조회 완료", data=data)


# 2. 날짜별 상세 (기록 + 이날 한 돌봄)
@router.get(
    "/days/{day}",
    summary="날짜별 마음 기록 상세",
    response_model=ApiResponseDTO[MoodRecordDetailResponseDTO]
)
async def get_mood_record_detail(
    day: date,
    current_user: UserClaimDTO = Depends(get_current_user),
    service: MoodRecordService = Depends(get_mood_record_service)
):
    data = await service.get_detail(current_user.id, day)
    return ApiResponseDTO(success=True, message="마음 기록 상세 조회 완료", data=data)


# 3. 기록하기 (하루 1건)
@router.post(
    "",
    summary="마음 기록하기",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponseDTO[MoodRecordResponseDTO]
)
async def create_mood_record(
    dto: MoodRecordCreateDTO,
    current_user: UserClaimDTO = Depends(get_current_user),
    service: MoodRecordService = Depends(get_mood_record_service)
):
    data = await service.create(current_user.id, dto)
    return ApiResponseDTO(success=True, message="마음 기록 완료", data=data)


# 4. 수정하기 · 선생님께 보여 주기
@router.patch(
    "/{emotion_id}",
    summary="마음 기록 수정",
    response_model=ApiResponseDTO[MoodRecordResponseDTO]
)
async def update_mood_record(
    emotion_id: int,
    dto: MoodRecordUpdateDTO,
    current_user: UserClaimDTO = Depends(get_current_user),
    service: MoodRecordService = Depends(get_mood_record_service)
):
    data = await service.update(current_user.id, emotion_id, dto)
    return ApiResponseDTO(success=True, message="마음 기록 수정 완료", data=data)


# 5. 지우기
@router.delete(
    "/{emotion_id}",
    summary="마음 기록 삭제",
    response_model=ApiResponseDTO
)
async def delete_mood_record(
    emotion_id: int,
    current_user: UserClaimDTO = Depends(get_current_user),
    service: MoodRecordService = Depends(get_mood_record_service)
):
    await service.delete(current_user.id, emotion_id)
    return ApiResponseDTO(success=True, message="마음 기록 삭제 완료")