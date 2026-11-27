from fastapi import APIRouter, Depends, Response
from app.service.user_service import get_user_service, UserService
from app.service.auth_service import get_auth_service, AuthService
from app.schema.user_schema import UserClaimDTO, UserResponseDTO, UserUpdateDTO
from app.schema.auth_schema import JwtTokenDTO
from app.schema.common_schema import ApiResponseDTO
from app.dependencie.auth_dependency import get_current_user, get_tokens


router = APIRouter()


# access_token -> 내 정보 조회
@router.get(
    "/me",
    summary="Access Token으로 내 정보 조회",
    response_model=ApiResponseDTO[UserResponseDTO],
    responses={
        401: {
            "description": "로그인하지 않았거나 토큰이 만료됨",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "get_current_user 로그인 세션이 만료되었습니다."
                    }
                }
            }
        },
        404: {
            "description": "탈퇴했거나 존재하지 않는 회원",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "회원이 존재하지 않습니다."
                    }
                }
            }
        }
    }
)
async def me(
    current_user: UserClaimDTO = Depends(get_current_user),
    user_service: UserService = Depends(get_user_service)
):
    found_user = await user_service.get_user_by_id(current_user.id)

    return ApiResponseDTO(
        success=True,
        message="회원 정보 조회 완료",
        data=found_user
    )


# 내 정보 수정 (보낸 값만 수정)
@router.put(
    "/me",
    summary="내 정보 수정",
    response_model=ApiResponseDTO[UserResponseDTO]
)
async def update_me(
    user: UserUpdateDTO,
    current_user: UserClaimDTO = Depends(get_current_user),
    user_service: UserService = Depends(get_user_service)
):
    updated_user = await user_service.update_user(current_user.id, user)

    return ApiResponseDTO(
        success=True,
        message="회원 정보 수정 완료",
        data=updated_user
    )


# 회원 탈퇴 -> 토큰 무효화 + 쿠키 삭제
# - 환자/의사는 서비스가 DB의 role로 구분해서 진행 중인 데이터(예약, 진료 요청, 담당 관계 등)를 함께 정리한다.
@router.delete(
    "/me",
    summary="회원 탈퇴",
    response_model=ApiResponseDTO,
    responses={
        404: {
            "description": "이미 탈퇴했거나 존재하지 않는 회원",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "회원이 존재하지 않습니다."
                    }
                }
            }
        }
    }
)
async def withdraw(
    response: Response,
    current_user: UserClaimDTO = Depends(get_current_user),
    tokens: JwtTokenDTO = Depends(get_tokens),
    user_service: UserService = Depends(get_user_service),
    auth_service: AuthService = Depends(get_auth_service)
):
    await user_service.withdraw(current_user.id)
    await auth_service.logout(current_user.id, tokens)

    response.delete_cookie("access_token")
    response.delete_cookie("refresh_token")

    return ApiResponseDTO(
        success=True,
        message="회원 탈퇴 완료"
    )
