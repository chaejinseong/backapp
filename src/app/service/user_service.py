import logging
from fastapi import Depends, HTTPException, status
from app.repository.user_repository import UserRepository, get_user_repository
from app.service.care_notice_service import CareNoticeService, get_care_notice_service
from app.models.user_model import User
from app.enums.user_enum import UserRole, UserSocialProvider
from app.schema.user_schema import UserCreateDTO, UserClaimDTO, UserResponseDTO, UserUpdateDTO


logger = logging.getLogger(__name__)


# 예외처리, 서비스가 드러나도록 이름을 선정해서 제작
# 트랜잭션 관리
class UserService:

    # care_notice_service: 의사 탈퇴 시 담당 환자에게 전달할 진료 변경 안내 저장 (Redis)
    def __init__(self, repo: UserRepository, care_notice_service: CareNoticeService):
        self.repo = repo
        self.care_notice_service = care_notice_service


    # 회원 조회(id)
    async def get_user_by_id(self, id: int) -> UserResponseDTO:
        found_user = await self.repo.find_user_by_id(id)

        if not found_user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="회원이 존재하지 않습니다."
            )

        return UserResponseDTO.model_validate(found_user)


    # 회원 조회(social_provider, social_id) - 없으면 None
    async def get_user_by_social(self, social_provider: UserSocialProvider, social_id: str) -> User | None:
        return await self.repo.find_user_by_social(social_provider, social_id)


    # 소셜 회원가입
    async def join(self, user: UserCreateDTO) -> UserClaimDTO:
        return await self.repo.create_user(user)


    # 회원 정보 수정
    async def update_user(self, id: int, user: UserUpdateDTO) -> UserResponseDTO:
        # dto -> dict
        # exclude_unset=True: 요청에 보내지 않은 컬럼은 제외
        update_data = user.model_dump(exclude_unset=True)
        updated_user = await self.repo.update_user(id, update_data)

        if not updated_user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="회원이 존재하지 않습니다."
            )

        return UserResponseDTO.model_validate(updated_user)


    # 회원 탈퇴 (환자 / 의사)
    # - JWT의 role은 로그인 시점 값이라 오래됐을 수 있으므로 DB에서 회원을 조회해 role을 확인한다.
    # - 환자: withdraw_patient (진료 요청·담당 관계·예약·의사 인증 신청 정리)
    # - 의사: withdraw_doctor (담당 관계·예약·지정 진료 요청 정리, doctor_profiles 삭제)
    #   DB commit이 끝난 뒤 영향받은 환자별 안내를 Redis에 저장한다. (다음 상담 대화에서 캐릭터가 전달)
    #   Redis 저장이 실패해도 탈퇴는 이미 끝났으므로 성공으로 처리하고 로그만 남긴다.
    async def withdraw(self, id: int) -> None:
        found_user = await self.repo.find_user_by_id(id)

        if not found_user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="회원이 존재하지 않습니다."
            )

        if found_user.role == UserRole.DOCTOR:
            results = await self.repo.withdraw_doctor(id)
            is_withdrawn = results is not None

            if results:
                try:
                    await self.care_notice_service.save_doctor_withdrawn_notice(results)
                except Exception:
                    logger.exception("의사 탈퇴 안내 저장 실패 (doctor_id=%s, patient_ids=%s)", id, list(results))
        else:
            is_withdrawn = await self.repo.withdraw_patient(id)

        if not is_withdrawn:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="회원이 존재하지 않습니다."
            )



# 레포지토리 주입 팩토리 함수
def get_user_service(
    repo: UserRepository = Depends(get_user_repository),
    care_notice_service: CareNoticeService = Depends(get_care_notice_service)
):
    return UserService(repo, care_notice_service)
