"""
회원 탈퇴(soft delete) 통합 테스트 - 환자 / 의사 / 의사 탈퇴 안내의 캐릭터 대화 전달 (실제 DB·Redis 사용)

실행 (backapp/src 에서):
    python ../tests/withdraw_api_test.py

흐름: 서버 시작(lifespan) -> seed_doctor_test_data로 테스트 데이터 넣기 -> 탈퇴용 데이터 추가 -> HTTP로 API 호출
     -> DB·Redis 확인 -> 정리 (reset + 이 테스트가 만든 Redis 키 삭제)
- 결과 출력·토큰·DB 조회 도우미는 doctor_api_test.py의 것을 같이 쓴다.
- LLM 호출은 실제 API 대신 llm_call.call_reply를 바꿔 끼워서 입력(ReplyContext)을 확인하고 결과를 정한다.
  (실패 1번 -> 안내 유지, 성공 1번 -> 안내 삭제)
"""
import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402
from sqlalchemy import text  # noqa: E402
from doctor_api_test import app, AsyncSessionLocal, KST, results, check, token_for, scalar  # noqa: E402
from app.infrastructure.redis import redis_client  # noqa: E402
from app.service.auth_service import TOKEN_BLACKLIST_PREFIX  # noqa: E402
from app.service.care_notice_service import CARE_NOTICE_PREFIX, CareNoticeService  # noqa: E402
from app.service.user_service import UserService  # noqa: E402
from app.service.llm_service import LlmService  # noqa: E402
from app.repository.user_repository import UserRepository  # noqa: E402
from app.repository.care_request_repository import CareRequestRepository  # noqa: E402
from app.llm_call import llm_call  # noqa: E402
from app.models.appointment_model import Appointment  # noqa: E402
from app.models.schedule_model import Schedule  # noqa: E402
from app.models.care_request_model import CareRequest  # noqa: E402
from app.models.doctor_verification_model import DoctorVerification  # noqa: E402
from app.models.chat_session_model import ChatSession  # noqa: E402
from app.schema.llm_schema import LlmReplyOutput  # noqa: E402
import seed_doctor_test_data as seed_module  # noqa: E402


# Redis 저장이 항상 실패하는 가짜 저장소 (의사 탈퇴 후 안내 저장 실패 -> 탈퇴는 성공인지 확인)
class BrokenRedisRepository:
    async def set(self, *args, **kwargs):
        raise ConnectionError("redis down (test)")

    async def get(self, *args, **kwargs):
        raise ConnectionError("redis down (test)")

    async def delete(self, *args, **kwargs):
        raise ConnectionError("redis down (test)")


# llm_call.call_reply 대신 쓰는 가짜 LLM: 받은 입력을 기록하고, 정해 둔 결과를 순서대로 돌려준다.
class FakeLlm:
    def __init__(self):
        self.contexts = []
        self.succeed: list[bool] = []

    def _record(self, status: str, attempt: int) -> dict:
        return {
            "purpose": llm_call.LlmCallPurpose.REPLY, "model": "fake-model", "prompt_version": "counsel-test",
            "attempt": attempt, "tokens_in": None, "tokens_out": None, "latency_ms": 0, "status": status, "error": None,
        }

    async def __call__(self, context):
        self.contexts.append(context)
        if self.succeed.pop(0):
            output = LlmReplyOutput(
                reply_text="말씀해 주셔서 고마워요. 전해드릴 이야기가 있어요.", expression="default",
                checklist_changes=[], asked_item_ids=[], counsel_end=False, end_reason=None,
            )
            return output, self._record(llm_call.STATUS_SUCCESS, context.attempt)
        return None, self._record(llm_call.STATUS_ERROR, context.attempt)


async def add_withdraw_data(users: dict) -> dict:
    """탈퇴 확인용 데이터 추가 (seed 데이터 위에)"""
    a, p1, p4, p5 = users["doctor_a"], users["p1"], users["p4"], users["p5"]
    now = datetime.now(KST)
    async with AsyncSessionLocal() as db:
        h1 = (await db.execute(text("select hospital_id from doctor_profiles where user_id = :i"), {"i": a.id})).scalar()

        # [환자 P5] 다가오는 예약 + 연결된 일정, 진행 중인 진료 요청, 대기 중인 의사 인증 신청
        p5_appt = Appointment(
            patient_id=p5.id, doctor_id=a.id, hospital_id=h1, scheduled_at=now + timedelta(days=2),
            end_at=now + timedelta(days=2, minutes=30), status="booked", kind="treatment",
            booking_source="doctor", created_by=a.id, contact_method="phone"
        )
        db.add(p5_appt)
        await db.flush()
        db.add_all([
            Schedule(user_id=p5.id, created_by=a.id, appointment_id=p5_appt.appointment_id, schedule_type="appointment",
                     title="진료 예약", start_at=p5_appt.scheduled_at, created_at=now),
            CareRequest(patient_id=p5.id, request_mode="nearby", request_type="consult", status="requested"),
            DoctorVerification(user_id=p5.id, hospital_id=h1, license_number="TEST-WD-1", status="pending"),
        ])

        # [의사 A] P1 내일 예약에 연결된 일정, A를 지정한 P4의 진료 요청 (P4는 의사 B의 환자)
        p1_tomorrow = (await db.execute(text(
            "select appointment_id, scheduled_at from appointments where patient_id = :p and kind = 'psych_test'"),
            {"p": p1.id})).first()
        db.add_all([
            Schedule(user_id=p1.id, created_by=a.id, appointment_id=p1_tomorrow[0], schedule_type="exam",
                     title="심리검사 예약", start_at=p1_tomorrow[1], created_at=now),
            CareRequest(patient_id=p4.id, target_doctor_id=a.id, request_mode="direct", request_type="treatment",
                        status="contacting"),
        ])
        await db.commit()
        return {"h1": h1, "p5_appt": p5_appt.appointment_id, "p1_tomorrow": p1_tomorrow[0]}


async def run(c: httpx.AsyncClient, users: dict, ids: dict) -> None:
    a, b = users["doctor_a"], users["doctor_b"]
    p1, p2, p4, p5 = users["p1"], users["p2"], users["p4"], users["p5"]

    async def call(method, path, user, body=None):
        r = await c.request(method, path, cookies={"access_token": token_for(user)}, json=body)
        return r.status_code

    # ================= 1. 환자 탈퇴 (P5) =================
    p5_access = token_for(p5, extra_seconds=7)
    r = await c.delete("/users/me", cookies={"access_token": p5_access})
    check("[환자] 탈퇴 -> 200", r.status_code == 200, r.text)
    r = await c.get("/users/me", cookies={"access_token": p5_access})
    check("[환자] 같은 토큰 -> 401 (블랙리스트)", r.status_code == 401, r.status_code)
    check("[환자] 새 토큰 GET /users/me -> 404", await call("GET", "/users/me", p5) == 404)
    check("[환자] 새 토큰으로 다시 탈퇴 -> 404", await call("DELETE", "/users/me", p5) == 404)
    await redis_client.delete(f"{TOKEN_BLACKLIST_PREFIX}{p5_access}")

    status = await scalar("select status from appointments where appointment_id = :i", i=ids["p5_appt"])
    check("[환자] 다가오는 예약 cancelled", status == "cancelled", status)
    change = await scalar(
        "select reason || '/' || requested_by || '/' || change_type from appointment_changes "
        "where appointment_id = :i and changed_by = :u", i=ids["p5_appt"], u=p5.id)
    check("[환자] appointment_changes 기록 (회원 탈퇴/patient/cancel)", change == "회원 탈퇴/patient/cancel", change)
    n = await scalar("select count(*) from schedules where appointment_id = :i", i=ids["p5_appt"])
    check("[환자] 예약에 연결된 일정 삭제", n == 0, n)
    n = await scalar("select count(*) from care_relations where patient_id = :i and ended_at is null", i=p5.id)
    check("[환자] care_relations.ended_at 기록", n == 0, n)
    status = await scalar("select status from care_requests where patient_id = :i", i=p5.id)
    check("[환자] 진행 중인 진료 요청 cancelled", status == "cancelled", status)
    row = (await _first("select status, reject_reason, reviewed_at from doctor_verifications where user_id = :i", i=p5.id))
    check("[환자] 대기 중인 의사 인증 신청 rejected (회원 탈퇴)",
          row and row[0] == "rejected" and row[1] == "회원 탈퇴" and row[2] is not None, row)
    row = await _first("select social_id, name, email, deleted_at from users where id = :i", i=p5.id)
    check("[환자] social_id = withdrawn_{id}", row[0] == f"withdrawn_{p5.id}", row[0])
    check("[환자] name = 탈퇴 회원, email 유지, deleted_at 기록",
          row[1] == "탈퇴 회원" and row[2] == p5.email and row[3] is not None, row)
    n = await scalar("select count(*) from doctor_profiles where user_id = :i", i=a.id)
    check("[환자] 담당 의사의 doctor_profiles는 그대로", n == 1, n)

    # ================= 2. 의사 탈퇴 (A) =================
    now = datetime.now(KST)
    async with AsyncSessionLocal() as db:
        expected_appts = (await db.execute(text(
            "select appointment_id, patient_id from appointments where doctor_id = :d "
            "and status in ('booked', 'waiting') and scheduled_at >= :now"), {"d": a.id, "now": now})).all()
        active_patients = set((await db.execute(text(
            "select patient_id from care_relations where doctor_id = :d and ended_at is null"), {"d": a.id})).scalars())
        before = await CareRequestRepository(db).find_doctor_ids_by_hospital([ids["h1"]])
    notifications_before = await scalar("select count(*) from notifications")
    check("[의사] 준비: 병원 연결 후보에 의사 A 포함", a.id in before.get(ids["h1"], []), before)
    check("[의사] 준비: 다가오는 예약이 있음 (P1 내일 검사 포함)",
          any(appt_id == ids["p1_tomorrow"] for appt_id, _ in expected_appts), expected_appts)

    a_access = token_for(a, extra_seconds=8)
    r = await c.delete("/users/me", cookies={"access_token": a_access})
    check("[의사] 탈퇴 -> 200", r.status_code == 200, r.text)
    r = await c.get("/users/me", cookies={"access_token": a_access})
    check("[의사] 같은 토큰 -> 401", r.status_code == 401, r.status_code)
    await redis_client.delete(f"{TOKEN_BLACKLIST_PREFIX}{a_access}")

    appt_ids = [appt_id for appt_id, _ in expected_appts]
    n = await scalar("select count(*) from appointments where appointment_id = any(:ids) and status = 'cancelled'", ids=appt_ids)
    check(f"[의사] 담당 환자 다가오는 예약 {len(appt_ids)}건 모두 cancelled", n == len(appt_ids), (n, len(appt_ids)))
    n = await scalar("select count(*) from appointment_changes where appointment_id = any(:ids) and changed_by = :d "
                     "and change_type = 'cancel' and requested_by = 'doctor' and reason = '담당 의사 탈퇴'", ids=appt_ids, d=a.id)
    check("[의사] appointment_changes 기록 (예약마다 1건)", n == len(appt_ids), (n, len(appt_ids)))
    n = await scalar("select count(*) from schedules where appointment_id = :i", i=ids["p1_tomorrow"])
    check("[의사] 취소된 예약의 일정 삭제", n == 0, n)
    n = await scalar("select count(*) from appointments where doctor_id = :d and status = 'completed'", d=a.id)
    check("[의사] 지난 진료 기록(completed)은 그대로", n >= 1, n)
    n = await scalar("select count(*) from care_relations where doctor_id = :d and ended_at is null", d=a.id)
    check("[의사] care_relations.ended_at 기록", n == 0, n)
    status = await scalar("select status from care_requests where target_doctor_id = :d", d=a.id)
    check("[의사] 이 의사를 지정한 진료 요청 cancelled", status == "cancelled", status)
    n = await scalar("select count(*) from doctor_profiles where user_id = :d", d=a.id)
    check("[의사] doctor_profiles 행 삭제", n == 0, n)
    n = await scalar("select count(*) from doctor_memos where doctor_id = :d", d=a.id)
    check("[의사] 의사 메모 기록은 그대로", n == 2, n)
    notifications_after = await scalar("select count(*) from notifications")
    check("[의사] notifications 새로 생성 안 됨", notifications_after == notifications_before,
          (notifications_before, notifications_after))
    row = await _first("select social_id, name from users where id = :i", i=a.id)
    check("[의사] social_id / name 변경", row == (f"withdrawn_{a.id}", "탈퇴 회원"), row)

    async with AsyncSessionLocal() as db:
        after = await CareRequestRepository(db).find_doctor_ids_by_hospital([ids["h1"]])
    check("[의사] find_doctor_ids_by_hospital 결과에서 제외", a.id not in after.get(ids["h1"], []), after)

    # Redis 안내: 담당 환자(P1, P2) + 지정 요청 환자(P4)
    notice_service = CareNoticeService(_RedisRepo())
    expected_patients = active_patients | {patient_id for _, patient_id in expected_appts} | {p4.id}
    for patient_id in sorted(expected_patients):
        check(f"[의사] Redis {CARE_NOTICE_PREFIX}{patient_id} 저장",
              await redis_client.exists(f"{CARE_NOTICE_PREFIX}{patient_id}") == 1, patient_id)
    p1_notice = await notice_service.get_notice(p1.id)
    p1_expected = []
    for appt_id, patient_id in expected_appts:
        if patient_id == p1.id:
            row = await _first("select scheduled_at from appointments where appointment_id = :i", i=appt_id)
            p1_expected.append(row[0].astimezone(KST))
    p1_expected.sort()
    check("[의사] P1 안내: type, 취소된 예약 일시 목록, 진료 요청 취소 없음",
          p1_notice and p1_notice["type"] == "doctor_withdrawn" and p1_notice["cancelled_request"] is False
          and [datetime.fromisoformat(v) for v in p1_notice["cancelled_appointments"]] == p1_expected, (p1_notice, p1_expected))
    p4_notice = await notice_service.get_notice(p4.id)
    check("[의사] P4 안내: 예약 없음, 진료 요청 취소 true",
          p4_notice and p4_notice["cancelled_appointments"] == [] and p4_notice["cancelled_request"] is True, p4_notice)
    check("[의사] 탈퇴한 환자 P5는 안내 없음 (담당 관계가 먼저 끝남)",
          await redis_client.exists(f"{CARE_NOTICE_PREFIX}{p5.id}") == 0)

    # 안내 합치기: 이미 있는 안내에 다른 의사 탈퇴 결과가 더해지면 예약은 합치고 요청 취소는 OR
    await notice_service.save_doctor_withdrawn_notice(
        {p4.id: {"cancelled_appointments": [datetime(2026, 12, 1, 14, 0, tzinfo=KST)], "cancelled_request": False}})
    merged = await notice_service.get_notice(p4.id)
    check("[안내] 기존 안내와 합치기 (예약 합침, cancelled_request OR)",
          merged["cancelled_appointments"] == ["2026-12-01T14:00:00+09:00"] and merged["cancelled_request"] is True, merged)
    ttl = await redis_client.ttl(f"{CARE_NOTICE_PREFIX}{p4.id}")
    check("[안내] TTL 30일", 60 * 60 * 24 * 29 < ttl <= 60 * 60 * 24 * 30, ttl)

    # Redis 저장 실패: 탈퇴는 성공 (의사 B, 담당 환자 P4)
    async with AsyncSessionLocal() as db:
        service = UserService(UserRepository(db), CareNoticeService(BrokenRedisRepository()))
        try:
            await service.withdraw(b.id)
            ok = True
        except Exception as e:  # noqa: BLE001
            ok = repr(e)
    deleted = await scalar("select deleted_at from users where id = :i", i=b.id)
    check("[의사] Redis 저장 실패해도 탈퇴 성공 (예외 없음, deleted_at 기록)", ok is True and deleted is not None, ok)

    # ================= 3. 대화: 안내 전달 =================
    text_ = LlmService._to_care_notice_text(merged)
    check("[대화] 안내 문장 (M월 D일 HH:MM, 요청 취소, 새 병원 안내)",
          text_ == "담당 선생님이 더 이상 진료를 이어갈 수 없게 되었습니다. 12월 1일 14:00 예약이 취소되었습니다. "
                   "진행 중이던 진료 요청도 취소되었습니다. 새 병원 연결은 진료 요청 화면에서 다시 할 수 있습니다.", text_)
    text_ = LlmService._to_care_notice_text({"cancelled_appointments": ["2026-12-01T05:30:00+00:00"], "cancelled_request": False})
    check("[대화] 안내 문장: UTC 값도 KST로 표시, 요청 취소 문장 없음",
          "12월 1일 14:30 예약이 취소되었습니다." in text_ and "진료 요청도" not in text_, text_)

    async with AsyncSessionLocal() as db:
        now = datetime.now(KST)
        session = ChatSession(user_id=p1.id, status="active", session_type="counseling",
                              scheduled_at=now - timedelta(minutes=1), started_at=now, expires_at=now + timedelta(hours=1))
        db.add(session)
        await db.commit()
        sid = session.id

    fake = FakeLlm()
    original_call_reply = llm_call.call_reply
    llm_call.call_reply = fake
    try:
        key = f"{CARE_NOTICE_PREFIX}{p1.id}"

        # 모든 시도 실패 (502) -> 안내 유지
        fake.succeed = [False]
        s = await call("POST", f"/chat/sessions/{sid}/messages", p1, {"clientMessageId": "wd-1", "content": "안녕하세요"})
        check("[대화] LLM 실패 -> 502", s == 502, s)
        check("[대화] 프롬프트 입력 ReplyContext.care_notice 채워짐",
              fake.contexts and fake.contexts[-1].care_notice and "담당 선생님이" in fake.contexts[-1].care_notice
              and "취소되었습니다" in fake.contexts[-1].care_notice,
              fake.contexts[-1].care_notice if fake.contexts else None)
        prompt = llm_call.build_reply_user_content(fake.contexts[-1])
        check("[대화] 프롬프트에 <care_notice> 태그 포함", "<care_notice>\n담당 선생님이" in prompt, prompt[:500])
        check("[대화] 실패한 턴에서는 Redis 안내 유지", await redis_client.exists(key) == 1)

        # 성공 -> 답변 저장 후 안내 삭제
        fake.succeed = [True]
        s = await call("POST", f"/chat/sessions/{sid}/messages", p1, {"clientMessageId": "wd-2", "content": "네 말씀하세요"})
        check("[대화] 답변 저장 성공 -> 200", s == 200, s)
        check("[대화] 두 번째 호출에도 care_notice 전달", fake.contexts[-1].care_notice is not None)
        check("[대화] 답변 저장 성공 후 Redis 안내 삭제", await redis_client.exists(key) == 0)
        n = await scalar("select count(*) from messages where session_id = :i and role = 'assistant'", i=sid)
        check("[대화] 답변 메시지 저장", n == 1, n)

        # 안내가 없으면 "제공되지 않음"
        fake.succeed = [True]
        s = await call("POST", f"/chat/sessions/{sid}/messages", p1, {"clientMessageId": "wd-3", "content": "알겠어요"})
        check("[대화] 안내 삭제 후 다음 턴은 care_notice=None",
              s == 200 and fake.contexts[-1].care_notice is None
              and f"<care_notice>\n{llm_call.NOT_PROVIDED}" in llm_call.build_reply_user_content(fake.contexts[-1]), s)

        # 재전송 경로: 안내를 다시 넣어도 지우지 않는다 (LLM 호출 없음)
        await notice_service.save_doctor_withdrawn_notice({p1.id: {"cancelled_appointments": [], "cancelled_request": True}})
        calls = len(fake.contexts)
        s = await call("POST", f"/chat/sessions/{sid}/messages", p1, {"clientMessageId": "wd-2", "content": "네 말씀하세요"})
        check("[대화] 재전송 -> 200, LLM 호출 없음, 안내 유지",
              s == 200 and len(fake.contexts) == calls and await redis_client.exists(key) == 1, (s, len(fake.contexts), calls))
    finally:
        llm_call.call_reply = original_call_reply

    # 세션 사후요약 경로는 기존처럼 care_notice_service 없이 생성된다 (run_session_summary)
    check("[대화] LlmService는 care_notice_service 없이도 생성 가능",
          LlmService(None, None).care_notice_service is None)


async def _first(sql: str, **params):
    async with AsyncSessionLocal() as db:
        return (await db.execute(text(sql), params)).first()


def _RedisRepo():
    from app.repository.redis_repository import RedisRepository
    return RedisRepository(redis_client)


async def main() -> int:
    created_keys: list[str] = []
    async with app.router.lifespan_context(app):
        async with AsyncSessionLocal() as db:
            await seed_module.reset(db)
            users = await seed_module.seed(db)
        test_user_ids = [u.id for u in users.values()]
        created_keys = [f"{CARE_NOTICE_PREFIX}{i}" for i in test_user_ids]
        await redis_client.delete(*created_keys)  # 이전 실행에서 남은 키
        try:
            ids = await add_withdraw_data(users)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                await run(client, users, ids)
        except Exception as e:  # 테스트 코드 자체가 깨져도 정리는 한다
            import traceback
            traceback.print_exc()
            check("테스트 실행 중 예외", False, repr(e))
        finally:
            async with AsyncSessionLocal() as db:
                await seed_module.reset(db)
            await redis_client.delete(*created_keys)
            leftover = [k async for k in redis_client.scan_iter(f"{TOKEN_BLACKLIST_PREFIX}*")]
            print(f"\n정리: 테스트 데이터·안내 키 삭제, 남은 블랙리스트 키 {len(leftover)}개")

    failed = [r for r in results if not r[1]]
    print(f"\n결과: {len(results) - len(failed)}/{len(results)} 통과, 실패 {len(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
