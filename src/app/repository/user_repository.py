from datetime import datetime, timedelta, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from app.infrastructure.postgresql import get_postgresql_db
from fastapi import Depends

# 모델, ORM
from app.models.user_model import User
from app.models.care_request_model import CareRequest
from app.models.care_relation_model import CareRelation
from app.models.appointment_model import Appointment
from app.models.appointment_change_model import AppointmentChange
from app.models.schedule_model import Schedule
from app.models.doctor_verification_model import DoctorVerification
from app.models.doctor_profile_model import DoctorProfile
from app.enums.user_enum import UserSocialProvider
from app.enums.care_request_enum import CareRequestStatus
from app.enums.appointment_enum import AppointmentStatus
from app.enums.appointment_change_enum import AppointmentChangeType
from app.enums.common_enum import RequestedBy
from app.enums.doctor_verification_enum import DoctorVerificationStatus
from app.repository.care_request_repository import ACTIVE_STATUSES
from app.schema.user_schema import UserCreateDTO, UserClaimDTO
from sqlalchemy import insert, select, update, delete, func

KST = timezone(timedelta(hours=9)) # UTC+9

# 탈퇴 시 users에 남기는 값
WITHDRAWN_SOCIAL_ID_PREFIX = "withdrawn_"  # social_id = "withdrawn_{id}" (NOT NULL + 재가입 시 unique 충돌 회피)
WITHDRAWN_NAME = "탈퇴 회원"

# 탈퇴 시 취소하는 예약 상태 (아직 진료 전인 예약)
CANCELLABLE_APPOINTMENT_STATUSES = (AppointmentStatus.BOOKED, AppointmentStatus.WAITING)


class UserRepository:

    # 생성자 주입
    def __init__(self, db: AsyncSession):
        self.db = db


    # 순수 함수
    # 1. 회원 정보 조회(id) - 탈퇴한 회원 제외
    async def find_user_by_id(self, id: int) -> User | None:
        query = (
            select(User)
            .where(
                User.id == id,
                User.deleted_at.is_(None)
            )
        )

        result = await self.db.execute(query)
        found_user = result.scalar_one_or_none()
        return found_user


    # 2. 회원 정보 조회(social_provider, social_id) - 소셜 로그인용
    async def find_user_by_social(self, social_provider: UserSocialProvider, social_id: str) -> User | None:
        query = (
            select(User)
            .where(
                User.social_provider == social_provider,
                User.social_id == social_id,
                User.deleted_at.is_(None)
            )
        )

        result = await self.db.execute(query)
        found_user = result.scalar_one_or_none()
        return found_user


    # 3. 소셜 회원가입
    async def create_user(self, user: UserCreateDTO) -> UserClaimDTO:
        # pydantic -> dict
        new_user = user.model_dump()
        query = (
            insert(User)
            .values(**new_user)
        ).returning(
            User.id,
            User.email,
            User.role,
            User.social_provider
        )

        result = await self.db.execute(query)
        # 필요한 값을 리턴(id, email, role, social_provider)
        id, email, role, social_provider = result.first()

        payload = {
            "id": id,
            "email": email,
            "role": role,
            "social_provider": social_provider
        }

        # dict -> pydantic
        claim_dto = UserClaimDTO.model_validate(payload)

        await self.db.commit()
        return claim_dto


    # 4. 회원 정보 수정
    async def update_user(self, id: int, update_data: dict) -> User | None:
        if not update_data:
            return await self.find_user_by_id(id)

        query = (
            update(User)
            .where(
                User.id == id,
                User.deleted_at.is_(None)
            )
            .values(**update_data)
            .returning(User)
        )

        result = await self.db.execute(query)
        updated_user = result.scalar_one_or_none()
        await self.db.commit()
        return updated_user


    # 5. 회원 탈퇴 (soft delete)
    # - 연관 데이터(일기, 상담 기록 등)를 보존하기 위해 행은 남기고 deleted_at만 기록한다.
    # - 행을 지우지 않으므로 FK의 ondelete=CASCADE가 동작하지 않는다. 진행 중인 데이터는 아래 메서드에서 직접 정리한다.
    # - 탈퇴 한 번의 DB 변경은 하나의 트랜잭션(commit 1번)으로 처리한다. 예외가 나면 rollback 후 다시 던진다.
    # - now 하나를 만들어 모든 테이블에 같은 값으로 기록한다.

    # 5-0. 공통: users 업데이트
    # - social_id는 NOT NULL이라 None 대신 "withdrawn_{id}"로 바꾼다. (같은 소셜 계정으로 재가입 가능)
    # - email은 남긴다. (테스트 정리 reset()이 email 도메인으로 테스트 회원을 찾는다)
    #   nickname, gender, birth_date도 건드리지 않는다.
    # - 반환: 업데이트된 행이 있으면 True. 없으면(회원 없음 또는 이미 탈퇴) rollback 후 False
    async def _soft_delete_user(self, id: int, now: datetime) -> bool:
        result = await self.db.execute(
            update(User)
            .where(
                User.id == id,
                User.deleted_at.is_(None)
            )
            .values(
                deleted_at=now,
                social_id=func.concat(WITHDRAWN_SOCIAL_ID_PREFIX, User.id),
                name=WITHDRAWN_NAME
            )
        )

        if result.rowcount == 0:
            await self.db.rollback()
            return False
        return True


    # 5-0-1. 공통: 다가오는 예약 취소 (예약 변경 기록 + 일정 삭제)
    # - doctor_repository.cancel_appointment와 같은 패턴 (status=CANCELLED, appointment_changes 기록, schedules 삭제)
    # - condition: 대상 예약 조건 (Appointment.patient_id == id 또는 Appointment.doctor_id == id)
    # - 반환: 취소한 예약 목록 [(patient_id, scheduled_at), ...] (일시 순)
    async def _cancel_upcoming_appointments(
        self, condition, now: datetime, reason: str, requested_by: RequestedBy, changed_by: int
    ) -> list[tuple[int, datetime]]:
        result = await self.db.execute(
            select(Appointment.appointment_id, Appointment.patient_id, Appointment.scheduled_at)
            .where(
                condition,
                Appointment.status.in_(CANCELLABLE_APPOINTMENT_STATUSES),
                Appointment.scheduled_at >= now
            )
            .order_by(Appointment.scheduled_at)
            .with_for_update()
        )
        appointments = result.all()
        if not appointments:
            return []

        appointment_ids = [appointment_id for appointment_id, _, _ in appointments]

        # 예약마다 취소 기록 남기기
        await self.db.execute(
            insert(AppointmentChange),
            [
                {
                    "appointment_id": appointment_id,
                    "from_at": scheduled_at,
                    "change_type": AppointmentChangeType.CANCEL,
                    "reason": reason,
                    "requested_by": requested_by,
                    "changed_by": changed_by,
                    "created_at": now
                }
                for appointment_id, _, scheduled_at in appointments
            ]
        )
        await self.db.execute(
            update(Appointment)
            .where(Appointment.appointment_id.in_(appointment_ids))
            .values(status=AppointmentStatus.CANCELLED)
        )
        await self.db.execute(delete(Schedule).where(Schedule.appointment_id.in_(appointment_ids)))

        return [(patient_id, scheduled_at) for _, patient_id, scheduled_at in appointments]


    # 5-1. 환자 탈퇴
    # 1) users 업데이트  2) 진행 중인 진료 요청 취소  3) 담당 관계 종료
    # 4) 다가오는 예약 취소  5) 대기 중인 의사 인증 신청 반려  6) commit
    async def withdraw_patient(self, id: int) -> bool:
        now = datetime.now(KST)

        try:
            # 1) users 업데이트 (없으면 정리 작업 없이 실패)
            if not await self._soft_delete_user(id, now):
                return False

            # 2) 진행 중인 진료 요청 취소
            await self.db.execute(
                update(CareRequest)
                .where(
                    CareRequest.patient_id == id,
                    CareRequest.status.in_(ACTIVE_STATUSES)
                )
                .values(status=CareRequestStatus.CANCELLED)
            )

            # 3) 담당 관계 종료
            await self.db.execute(
                update(CareRelation)
                .where(
                    CareRelation.patient_id == id,
                    CareRelation.ended_at.is_(None)
                )
                .values(ended_at=now)
            )

            # 4) 다가오는 예약 취소
            await self._cancel_upcoming_appointments(
                Appointment.patient_id == id, now,
                reason="회원 탈퇴", requested_by=RequestedBy.PATIENT, changed_by=id
            )

            # 5) 대기 중인 의사 인증 신청 반려
            await self.db.execute(
                update(DoctorVerification)
                .where(
                    DoctorVerification.user_id == id,
                    DoctorVerification.status == DoctorVerificationStatus.PENDING
                )
                .values(
                    status=DoctorVerificationStatus.REJECTED,
                    reject_reason="회원 탈퇴",
                    reviewed_at=now
                )
            )

            # 6) commit
            await self.db.commit()
            return True

        except Exception:
            await self.db.rollback()
            raise


    # 5-2. 의사 탈퇴
    # 1) users 업데이트  2) 담당 관계 종료  3) 다가오는 예약 취소  4) 이 의사를 지정한 진료 요청 취소
    # 5) doctor_profiles 삭제 (병원 연결 후보에서 빠진다. 반드시 2) 이후)  6) commit
    # - Notification은 만들지 않는다. (환자 안내는 서비스가 Redis에 저장해 캐릭터 대화로 전달)
    # - doctor_verifications, doctor_memos, risk_events.reviewed_by 등 기록은 그대로 둔다.
    # - 반환: 실패 시 None, 성공 시 영향받은 환자별 결과
    #   {patient_id: {"cancelled_appointments": [scheduled_at, ...], "cancelled_request": bool}}
    #   (담당 관계만 끝난 환자도 빈 목록으로 포함)
    async def withdraw_doctor(self, id: int) -> dict[int, dict] | None:
        now = datetime.now(KST)
        results: dict[int, dict] = {}

        def patient_result(patient_id: int) -> dict:
            return results.setdefault(patient_id, {"cancelled_appointments": [], "cancelled_request": False})

        try:
            # 1) users 업데이트 (없으면 정리 작업 없이 실패)
            if not await self._soft_delete_user(id, now):
                return None

            # 2) 담당 관계 종료
            result = await self.db.execute(
                update(CareRelation)
                .where(
                    CareRelation.doctor_id == id,
                    CareRelation.ended_at.is_(None)
                )
                .values(ended_at=now)
                .returning(CareRelation.patient_id)
            )
            for patient_id in result.scalars().all():
                patient_result(patient_id)

            # 3) 다가오는 예약 취소
            cancelled = await self._cancel_upcoming_appointments(
                Appointment.doctor_id == id, now,
                reason="담당 의사 탈퇴", requested_by=RequestedBy.DOCTOR, changed_by=id
            )
            for patient_id, scheduled_at in cancelled:
                patient_result(patient_id)["cancelled_appointments"].append(scheduled_at)

            # 4) 이 의사를 지정한 진행 중인 진료 요청 취소
            result = await self.db.execute(
                update(CareRequest)
                .where(
                    CareRequest.target_doctor_id == id,
                    CareRequest.status.in_(ACTIVE_STATUSES)
                )
                .values(status=CareRequestStatus.CANCELLED)
                .returning(CareRequest.patient_id)
            )
            for patient_id in result.scalars().all():
                patient_result(patient_id)["cancelled_request"] = True

            # 5) doctor_profiles 삭제 (병원 연결 쿼리가 doctor_profiles 기준이라 후보에서 자동으로 빠진다)
            await self.db.execute(delete(DoctorProfile).where(DoctorProfile.user_id == id))

            # 6) commit
            await self.db.commit()
            return results

        except Exception:
            await self.db.rollback()
            raise



# 주입 팩토리 메서드
def get_user_repository(db: AsyncSession = Depends(get_postgresql_db)):
    return UserRepository(db)
