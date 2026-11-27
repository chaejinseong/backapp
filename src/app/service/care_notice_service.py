import json
import os
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from fastapi import Depends
from app.repository.redis_repository import get_redis_repository, RedisRepository

load_dotenv()
CARE_NOTICE_PREFIX = os.getenv("CARE_NOTICE_PREFIX", "care_notice:")

KST = timezone(timedelta(hours=9)) # UTC+9

# 안내 종류
NOTICE_TYPE_DOCTOR_WITHDRAWN = "doctor_withdrawn"

# 안내 보관 기간: 30일 (그 안에 상담 대화를 하지 않으면 사라진다)
CARE_NOTICE_TTL_SECONDS = 60 * 60 * 24 * 30


# 진료 변경 안내 (환자별 1건, Redis)
# - 의사 탈퇴처럼 환자에게 알려야 하는 진료 변경을 저장해 두고, 다음 상담 대화에서 캐릭터가 전달한다.
# - key: care_notice:{patient_id}
# - value: JSON 문자열 (Redis가 decode_responses=True라 get은 str을 돌려준다)
#   {"type": "doctor_withdrawn", "created_at": ISO 문자열,
#    "cancelled_appointments": [ISO 문자열, ...], "cancelled_request": true/false}
# - 답변 저장에 성공하면 llm_service가 delete_notice로 지운다.
class CareNoticeService:

    def __init__(self, redis_repo: RedisRepository):
        self.redis_repo = redis_repo


    @staticmethod
    def _key(patient_id: int) -> str:
        return f"{CARE_NOTICE_PREFIX}{patient_id}"


    @staticmethod
    def _to_iso(value: datetime | str) -> str:
        return value.isoformat() if isinstance(value, datetime) else value


    # 1. 의사 탈퇴 안내 저장 (환자별)
    # - results: UserRepository.withdraw_doctor의 반환값
    #   {patient_id: {"cancelled_appointments": [scheduled_at, ...], "cancelled_request": bool}}
    # - 이미 안내가 있으면 (예: 아직 대화하지 않은 사이 다른 담당 의사도 탈퇴)
    #   cancelled_appointments는 합치고(중복 제거, 일시 순), cancelled_request는 OR로 합친다.
    async def save_doctor_withdrawn_notice(self, results: dict[int, dict]) -> None:
        created_at = datetime.now(KST).isoformat()

        for patient_id, result in results.items():
            appointments = [self._to_iso(at) for at in result.get("cancelled_appointments", [])]
            cancelled_request = bool(result.get("cancelled_request", False))

            existing = await self.get_notice(patient_id)
            if existing:
                appointments = list(existing.get("cancelled_appointments", [])) + appointments
                cancelled_request = cancelled_request or bool(existing.get("cancelled_request", False))

            notice = {
                "type": NOTICE_TYPE_DOCTOR_WITHDRAWN,
                "created_at": created_at,
                "cancelled_appointments": sorted(set(appointments)),
                "cancelled_request": cancelled_request,
            }
            await self.redis_repo.set(
                self._key(patient_id), json.dumps(notice, ensure_ascii=False), ex=CARE_NOTICE_TTL_SECONDS
            )


    # 2. 안내 조회 - 없으면 None
    async def get_notice(self, patient_id: int) -> dict | None:
        value = await self.redis_repo.get(self._key(patient_id))
        if not value:
            return None
        return json.loads(value)


    # 3. 안내 삭제 (상담 답변으로 전달한 뒤)
    async def delete_notice(self, patient_id: int) -> None:
        await self.redis_repo.delete(self._key(patient_id))



# 레포지토리 주입 팩토리 함수
def get_care_notice_service(redis_repo: RedisRepository = Depends(get_redis_repository)):
    return CareNoticeService(redis_repo)
