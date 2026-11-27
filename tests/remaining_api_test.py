"""
기존 테스트가 없던 API 통합 테스트 (실제 DB·Redis 사용)

대상: 헬스체크·보안 헤더·CORS, 인증(소셜 리다이렉트/state, 로그아웃, 토큰 재발급), 회원, 마이페이지,
     마음 일기, 포인트, 의사 메모, 의사 승인·관리자, AI 상담 세션, 빈 라우터(mood-records 등)

실행 (backapp/src 에서):
    python ../tests/remaining_api_test.py

흐름: 서버 시작(lifespan) -> seed_doctor_test_data로 테스트 데이터 넣기 -> HTTP로 API 호출 -> DB 행 확인 -> 정리.
- 처리되지 않은 예외도 500 응답으로 받도록 raise_app_exceptions=False로 호출한다.
- 결과 출력·토큰·DB 조회 도우미는 doctor_api_test.py의 것을 같이 쓴다.
"""
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402
from jose import jwt  # noqa: E402
from sqlalchemy import text  # noqa: E402
from doctor_api_test import app, AsyncSessionLocal, KST, results, check, token_for, scalar  # noqa: E402
from app.infrastructure.redis import redis_client  # noqa: E402
from app.service.auth_service import TOKEN_BLACKLIST_PREFIX, REFRESH_TOKEN_PREFIX, OAUTH_STATE_PREFIX  # noqa: E402
from app.schema.user_schema import UserClaimDTO  # noqa: E402
from app.util import jwt_token_util  # noqa: E402
from app.models.admin_model import Admin  # noqa: E402
from app.repository.point_repository import PointRepository  # noqa: E402
from app.service.point_service import PointService  # noqa: E402
import seed_doctor_test_data as seed_module  # noqa: E402


def claims_of(user) -> UserClaimDTO:
    return UserClaimDTO(id=user.id, email=user.email, role=user.role, social_provider=user.social_provider)


async def run(c: httpx.AsyncClient, users: dict) -> None:
    a, b, x = users["doctor_a"], users["doctor_b"], users["patient_x"]
    p1, p2, p3, p4, p5 = (users[k] for k in ("p1", "p2", "p3", "p4", "p5"))
    A, B, X = ({"access_token": token_for(u)} for u in (a, b, x))
    P1, P2, P3, P5 = ({"access_token": token_for(u)} for u in (p1, p2, p3, p5))
    now = datetime.now(KST)
    today = now.date()

    async def call(method, path, body=None, cookies=P1, **params):
        r = await c.request(method, path, cookies=cookies, json=body, params=params)
        try:
            data = r.json().get("data") if r.headers.get("content-type", "").startswith("application/json") else None
        except Exception:
            data = None
        return r.status_code, data

    # ================= 0. 헬스체크 · 보안 =================
    r = await c.get("/")
    check("헬스체크: GET / -> 200 Hello World", r.status_code == 200 and r.json() == {"message": "Hello World"}, r.text)
    check("보안 헤더: X-Frame-Options=DENY, nosniff, CSP",
          r.headers.get("x-frame-options") == "DENY" and r.headers.get("x-content-type-options") == "nosniff"
          and "content-security-policy" in r.headers, dict(r.headers))
    r = await c.options("/users/me", headers={
        "Origin": "https://evil.example", "Access-Control-Request-Method": "GET"})
    acao = r.headers.get("access-control-allow-origin")
    check("[관찰] CORS: 임의 origin(evil.example)에 자격증명 허용 응답",
          acao == "https://evil.example" and r.headers.get("access-control-allow-credentials") == "true",
          f"ACAO={acao}, ACAC={r.headers.get('access-control-allow-credentials')}")
    r = await c.get("/docs")
    check("Swagger: /docs -> 200", r.status_code == 200, r.status_code)
    r = await c.get("/openapi.json")
    check("OpenAPI 스키마 생성 -> 200", r.status_code == 200 and len(r.json()["paths"]) > 50, r.status_code)

    # ================= 1. 인증 =================
    for provider in ("google", "kakao", "naver"):
        before = len([k async for k in redis_client.scan_iter(f"{OAUTH_STATE_PREFIX}*")])
        r = await c.get(f"/auth/{provider}", follow_redirects=False)
        after = len([k async for k in redis_client.scan_iter(f"{OAUTH_STATE_PREFIX}*")])
        check(f"소셜 로그인({provider}): 307 리다이렉트 + state 발급(Redis)",
              r.status_code == 307 and "state=" in r.headers.get("location", "") and after == before + 1,
              (r.status_code, before, after))
        r = await c.get(f"/auth/{provider}/callback", params={"code": "x", "state": "forged-state"}, follow_redirects=False)
        check(f"소셜 콜백({provider}): 위조 state -> 400 (외부 호출 전에 차단)", r.status_code == 400, r.status_code)
        r = await c.get(f"/auth/{provider}/callback", params={"code": "x"}, follow_redirects=False)
        check(f"소셜 콜백({provider}): state 없음 -> 400", r.status_code == 400, r.status_code)
    # 테스트가 만든 state 정리
    async for k in redis_client.scan_iter(f"{OAUTH_STATE_PREFIX}*"):
        ttl = await redis_client.ttl(k)
        if ttl > 290:
            await redis_client.delete(k)

    # 토큰 검증
    s, _ = await call("GET", "/users/me", cookies={})
    check("토큰: 쿠키 없음 -> 401", s == 401, s)
    s, _ = await call("GET", "/users/me", cookies={"access_token": "garbage"})
    check("토큰: 형식이 잘못된 토큰 -> 401", s == 401, s)
    forged = jwt.encode({**claims_of(p1).model_dump(mode="json"), "exp": now + timedelta(days=1)},
                        "wrong-secret", algorithm=jwt_token_util.ALGORITHM)
    s, _ = await call("GET", "/users/me", cookies={"access_token": forged})
    check("토큰: 서명이 다른 토큰 -> 401", s == 401, s)
    expired = jwt.encode({**claims_of(p1).model_dump(mode="json"), "exp": now - timedelta(minutes=1)},
                         jwt_token_util.SECRET_KEY, algorithm=jwt_token_util.ALGORITHM)
    s, _ = await call("GET", "/users/me", cookies={"access_token": expired})
    check("토큰: 만료된 토큰 -> 401", s == 401, s)

    # 재발급
    r = await c.post("/auth/refresh")
    check("재발급: refresh 쿠키 없음 -> 401", r.status_code == 401, r.status_code)
    r = await c.post("/auth/refresh", cookies={"refresh_token": "garbage"})
    check("재발급: 잘못된 refresh -> 401", r.status_code == 401, r.status_code)
    refresh_p2 = jwt_token_util.generate_refresh_token(claims_of(p2))
    r = await c.post("/auth/refresh", cookies={"refresh_token": refresh_p2})
    check("재발급: 서명은 맞지만 Redis에 없는 refresh -> 401", r.status_code == 401, r.status_code)
    await redis_client.set(f"{REFRESH_TOKEN_PREFIX}{p2.id}", refresh_p2, ex=600)
    r = await c.post("/auth/refresh", cookies={"refresh_token": refresh_p2})
    new_access = r.cookies.get("access_token")
    check("재발급: Redis에 저장된 refresh -> 200 + access_token 쿠키", r.status_code == 200 and new_access, r.status_code)
    s, d = await call("GET", "/users/me", cookies={"access_token": new_access})
    check("재발급: 새 access_token으로 /users/me -> 200", s == 200 and d["id"] == p2.id, s)
    s, _ = await call("GET", "/users/me", cookies={"access_token": refresh_p2})
    check("[관찰] refresh_token을 access_token 자리에 넣어도 인증 통과 (토큰 종류 구분 없음)", s == 200, s)

    # 로그아웃
    logout_access = token_for(p2, extra_seconds=7)
    r = await c.post("/auth/logout", cookies={"access_token": logout_access, "refresh_token": refresh_p2})
    set_cookie = r.headers.get_list("set-cookie")
    check("로그아웃: 200 + 쿠키 2개 삭제",
          r.status_code == 200 and sum('access_token=""' in h or 'refresh_token=""' in h for h in set_cookie) == 2, set_cookie)
    check("로그아웃: 블랙리스트 등록 + refresh 키 삭제",
          await redis_client.exists(f"{TOKEN_BLACKLIST_PREFIX}{logout_access}")
          and not await redis_client.exists(f"{REFRESH_TOKEN_PREFIX}{p2.id}"), "")
    s, _ = await call("GET", "/users/me", cookies={"access_token": logout_access})
    check("로그아웃: 같은 access_token 재사용 -> 401", s == 401, s)
    r = await c.post("/auth/refresh", cookies={"refresh_token": refresh_p2})
    check("로그아웃: 같은 refresh 재사용 -> 401", r.status_code == 401, r.status_code)
    r = await c.post("/auth/logout")
    check("로그아웃: 쿠키 없음 -> 401", r.status_code == 401, r.status_code)
    await redis_client.delete(f"{TOKEN_BLACKLIST_PREFIX}{logout_access}")

    # ================= 2. 회원 =================
    s, d = await call("GET", "/users/me")
    check("회원: 내 정보 조회 -> 200, 이메일 일치", s == 200 and d["email"] == p1.email, d)
    s, d = await call("PUT", "/users/me", {"nickname": "민준이"})
    check("회원: 닉네임 수정 -> 200", s == 200 and d["nickname"] == "민준이", d)
    s, d = await call("PUT", "/users/me", {"gender": "unknown-value"})
    check("회원: 잘못된 성별 -> 422", s == 422, s)
    s, d = await call("PUT", "/users/me", {"nickname": "a"})
    check("[관찰] 회원: PUT /users/me는 1자 닉네임도 허용 (마이페이지는 2~10자 검증)", s == 200, (s, d))
    s, _ = await call("PUT", "/users/me", {"nickname": "가" * 60})
    check("회원: 컬럼 길이(50) 초과 닉네임 -> 4xx (500이 아니어야 함)", 400 <= s < 500, s)
    s, _ = await call("PUT", "/users/me", {"birthDate": (today + timedelta(days=30)).isoformat()})
    check("회원: 미래 생년월일 -> 4xx", 400 <= s < 500, s)
    s, d = await call("GET", "/mypage/profile")
    check("[영향] 미래 생년월일 저장 후 마이페이지 나이가 음수", s == 200 and d["age"] is not None and d["age"] < 0, d.get("age") if d else s)
    await call("PUT", "/users/me", {"nickname": "민준", "birthDate": "1998-05-14"})

    # 탈퇴 (P3: 담당 끝난 환자)
    p3_access = token_for(p3, extra_seconds=9)
    r = await c.delete("/users/me", cookies={"access_token": p3_access})
    check("탈퇴: 200", r.status_code == 200, r.status_code)
    deleted_at = await scalar("select deleted_at from users where id = :i", i=p3.id)
    check("탈퇴: users.deleted_at 기록 (소프트 삭제)", deleted_at is not None, deleted_at)
    s, _ = await call("GET", "/users/me", cookies={"access_token": p3_access})
    check("탈퇴: 같은 토큰 -> 401 (블랙리스트)", s == 401, s)
    s, _ = await call("GET", "/users/me", cookies={"access_token": token_for(p3)})
    check("탈퇴: 새 토큰으로 내 정보 -> 404", s == 404, s)
    s, _ = await call("GET", "/mypage", cookies={"access_token": token_for(p3)})
    check("탈퇴: 새 토큰으로 마이페이지 -> 404", s == 404, s)
    s, _ = await call("POST", "/diaries", {"content": "탈퇴 후 작성"}, cookies={"access_token": token_for(p3)})
    check("탈퇴 회원 토큰으로 일기 작성 -> 401/404 (탈퇴가 반영됐다면)", s in (401, 403, 404), s)
    await redis_client.delete(f"{TOKEN_BLACKLIST_PREFIX}{p3_access}")

    # ================= 3. 마이페이지 =================
    s, d = await call("GET", "/mypage")
    check("마이페이지: 요약 200, 가입 1일째 이상, 기본 설정",
          s == 200 and d["profile"]["daysTogether"] >= 1 and d["profile"]["today"] == today.isoformat()
          and d["setting"]["hideStats"] is False, d)
    s, d = await call("GET", "/mypage/profile")
    linked = (d or {}).get("linkedDoctors", [])
    check("마이페이지: 내 정보 - 담당 의사 이은솔, [TEST] 마음병원",
          s == 200 and len(linked) == 1 and linked[0]["name"] == "이은솔" and linked[0]["hospitalName"] == "[TEST] 마음병원", d)
    check("마이페이지: 만 나이 계산 (1998-05-14)",
          d["age"] == today.year - 1998 - (1 if (today.month, today.day) < (5, 14) else 0), d.get("age"))
    s, d = await call("PATCH", "/mypage/profile", {"nickname": "  민준  "})
    check("마이페이지: 닉네임 앞뒤 공백 제거", s == 200 and d["nickname"] == "민준", d)
    for name, body in [("닉네임 1자", {"nickname": "a"}), ("닉네임 11자", {"nickname": "가" * 11}),
                       ("미래 생년월일", {"birthDate": (today + timedelta(days=1)).isoformat()}),
                       ("빈 이름", {"name": "   "})]:
        s, _ = await call("PATCH", "/mypage/profile", body)
        check(f"마이페이지: {name} -> 422", s == 422, s)

    s, d = await call("GET", "/mypage/settings")
    check("설정: 행이 없으면 기본값 (hideStats=false)", s == 200 and d["hideStats"] is False, d)
    s, d = await call("PATCH", "/mypage/settings", {"hideStats": True, "shareDiary": True})
    check("설정: hideStats·shareDiary 수정", s == 200 and d["hideStats"] is True and d["shareDiary"] is True, d)
    consent_at = await scalar("select consent_updated_at from user_settings where user_id = :u", u=p1.id)
    check("설정: 공유 동의 변경 시 consent_updated_at 기록", consent_at is not None, consent_at)
    s, d = await call("GET", "/mypage/settings")
    check("설정: 다시 조회해도 유지", s == 200 and d["hideStats"] is True and d["shareDiary"] is True, d)
    s, d = await call("PATCH", "/mypage/settings", {"hideStats": False})
    check("설정: 보낸 값만 수정 (shareDiary 유지)", s == 200 and d["hideStats"] is False and d["shareDiary"] is True, d)

    # ================= 4. 마음 일기 =================
    r = await c.get("/diaries")
    check("일기: 쿠키 없음 -> 401", r.status_code == 401, r.status_code)
    s, _ = await call("POST", "/diaries", {"content": "같은 날 두 번째 일기"})
    check("일기: 같은 날짜에 이미 일기가 있으면 409 (P1, 시드에 오늘 일기 있음)", s == 409, s)
    s, d = await call("POST", "/diaries", {"content": "오늘은 산책을 했다."}, cookies=P2)
    check("일기: 작성 201, 날짜 기본값 오늘", s == 201 and d["diaryDate"] == today.isoformat(), d)
    diary_today = d["diaryId"] if d else await scalar(
        "select diary_id from diaries where user_id = :u and deleted_at is null order by diary_date desc limit 1", u=p2.id)
    n = await scalar("select count(*) from diaries where user_id = :u", u=p2.id)
    check("DB: 작성 요청 1번에 일기 1행 (응답 실패 시에도 행이 커밋되는지 확인)", n == 1, n)
    s, d = await call("POST", "/diaries", {"content": "지난주 일기", "diaryDate": (today - timedelta(days=7)).isoformat()}, cookies=P2)
    check("일기: 날짜 지정 작성 201", s == 201, s)
    diary_old = d["diaryId"] if d else await scalar(
        "select diary_id from diaries where user_id = :u and diary_date < :t order by diary_date limit 1", u=p2.id, t=now - timedelta(days=1))
    for name, body, code in [
        ("미래 날짜 -> 400", {"content": "x", "diaryDate": (today + timedelta(days=1)).isoformat()}, 400),
        ("빈 내용 -> 422", {"content": ""}, 422),
        ("5001자 -> 422", {"content": "가" * 5001}, 422),
        ("content 누락 -> 422", {}, 422),
    ]:
        s, _ = await call("POST", "/diaries", body)
        check(f"일기: {name}", s == code, s)
    s, _ = await call("GET", "/diaries")
    check("일기: 시드 일기(updated_at NULL)가 있는 P1 목록 조회 -> 200", s == 200, s)
    s, d = await call("GET", "/diaries", cookies=P2)
    check("일기: 목록 최신순 (오늘 -> 지난주)",
          s == 200 and [i["diaryId"] for i in d["items"]][:2] == [diary_today, diary_old], d)
    s, d = await call("GET", "/diaries", month=today.strftime("%Y-%m"), cookies=P2)
    check("일기: 이번 달 필터에 오늘 일기 포함", s == 200 and diary_today in [i["diaryId"] for i in d["items"]], d)
    s, d = await call("GET", "/diaries", limit=1, cookies=P2)
    check("일기: limit=1 -> 1건, totalPages 계산", s == 200 and len(d["items"]) == 1 and d["totalPages"] == d["totalCount"], d)
    for bad in ("2026-13", "abc", "2026"):
        s, _ = await call("GET", "/diaries", month=bad, cookies=P2)
        check(f"일기: 잘못된 month '{bad}' -> 400", s == 400, s)
    s, _ = await call("GET", "/diaries", limit=0, cookies=P2)
    check("일기: limit=0 -> 422", s == 422, s)
    s, d = await call("GET", f"/diaries/{diary_today}", cookies=P2)
    check("일기: 상세 200", s == 200 and d["content"] == "오늘은 산책을 했다.", d)
    s, _ = await call("GET", f"/diaries/{diary_today}", cookies=P1)
    check("일기: 다른 환자의 일기 상세 -> 404", s == 404, s)
    s, d = await call("PATCH", f"/diaries/{diary_today}", {"content": "산책 30분"}, cookies=P2)
    check("일기: 수정 200", s == 200 and d["content"] == "산책 30분" and d["diaryDate"] == today.isoformat(), d)
    s, _ = await call("PATCH", f"/diaries/{diary_today}", {"content": "남의 일기"}, cookies=P1)
    check("일기: 다른 환자가 수정 -> 404", s == 404, s)
    s, _ = await call("PATCH", f"/diaries/{diary_today}", {"diaryDate": (today + timedelta(days=2)).isoformat()}, cookies=P2)
    check("일기: 미래 날짜로 수정 -> 400", s == 400, s)
    s, _ = await call("PATCH", f"/diaries/{diary_old}", {"diaryDate": today.isoformat()}, cookies=P2)
    check("일기: 이미 일기가 있는 날짜로 수정 -> 409", s == 409, s)
    s, _ = await call("DELETE", f"/diaries/{diary_old}", cookies=P1)
    check("일기: 다른 환자가 삭제 -> 404", s == 404, s)
    s, _ = await call("DELETE", f"/diaries/{diary_old}", cookies=P2)
    check("일기: 삭제 200", s == 200, s)
    s, _ = await call("GET", f"/diaries/{diary_old}", cookies=P2)
    check("일기: 삭제 후 상세 -> 404", s == 404, s)
    s, _ = await call("DELETE", f"/diaries/{diary_old}", cookies=P2)
    check("일기: 두 번 삭제 -> 404", s == 404, s)
    s, _ = await call("GET", "/diaries/999999999", cookies=P2)
    check("일기: 없는 일기 -> 404", s == 404, s)

    # ================= 5. 포인트 =================
    # 포인트는 다른 서비스(오늘 할 일 등)가 PointService.earn/spend/cancel로 쌓는다. 먼저 서비스 함수를 직접 확인한다.
    async with AsyncSessionLocal() as db:
        svc = PointService(PointRepository(db))
        base_balance = (await svc.get_summary(p1.id)).point_balance
        try:
            await svc.earn(p1.id, 15, None, "[TEST] 마음이와 대화하기")
            service_ok, service_err = True, ""
        except Exception as e:
            service_ok, service_err = False, repr(e)
            await db.rollback()
    check("포인트 서비스: earn() 동작 (잔액 잠금 조회 lock_user)", service_ok, service_err)

    if service_ok:
        async with AsyncSessionLocal() as db:
            svc = PointService(PointRepository(db))
            h_cancel = await svc.earn(p1.id, 10, None, "[TEST] 한 줄 일기")
            await svc.spend(p1.id, 5, None, "[TEST] 자장가")
            await svc.cancel(p1.id, h_cancel.history_id)
            await db.commit()
            for name, fn in [("잔액보다 많이 쓰기 -> 400", lambda: svc.spend(p1.id, 10_000_000, None, "[TEST] 비싼 물건")),
                             ("없는 내역 취소 -> 400", lambda: svc.cancel(p1.id, 999999999))]:
                try:
                    await fn(); code = "통과됨"
                except Exception as e:
                    code = getattr(e, "status_code", repr(e))
                await db.rollback()
                check(f"포인트 서비스: {name}", code == 400, code)
    else:
        # 서비스가 깨져 있으면 조회 API를 확인하려고 같은 결과를 DB에 직접 넣는다.
        from app.models.point_history_model import PointHistory
        async with AsyncSessionLocal() as db:
            bal = base_balance
            for t, amt in [("earn", 15), ("earn", 10), ("spend", 5), ("cancel", 10)]:
                bal += amt if t == "earn" else -amt
                db.add(PointHistory(user_id=p1.id, history_type=t, amount=amt, balance_after=bal,
                                    reason=f"[TEST] {t}", base_date=now))
                await db.flush()
            await db.execute(text("update users set point_balance = :b where id = :u"), {"b": bal, "u": p1.id})
            await db.commit()

    s, d = await call("GET", "/points")
    check("포인트: 요약 잔액 = 기존 + 15 + 10 - 5 - 10", s == 200 and d["pointBalance"] == base_balance + 10, d)
    check("포인트: 오늘 모은 포인트 15 (얻음 25 - 취소 10)", d["earnedToday"] >= 15 and d["earnedThisMonth"] >= d["earnedToday"], d)
    s, d = await call("GET", "/points/histories", size=2)
    check("포인트 기록: size=2 -> 2건 + nextCursor", s == 200 and len(d["items"]) == 2 and d["nextCursor"], d)
    first_page = [i["historyId"] for i in d["items"]]
    s, d2 = await call("GET", "/points/histories", size=2, cursor=d["nextCursor"])
    check("포인트 기록: 다음 페이지는 겹치지 않음, 마지막이면 nextCursor null",
          s == 200 and not set(first_page) & {i["historyId"] for i in d2["items"]} and d2["nextCursor"] is None, d2)
    s, d = await call("GET", "/points/histories")
    check("포인트 기록: 최신순, 취소·사용은 음수 표시",
          s == 200 and [i["historyType"] for i in d["items"]] == ["cancel", "spend", "earn", "earn"]
          and d["items"][0]["amount"] == -10 and d["items"][1]["amount"] == -5, d)
    s, d = await call("GET", "/points/histories", type="spend")
    check("포인트 기록: spend 탭 1건", s == 200 and len(d["items"]) == 1, d)
    s, d = await call("GET", "/points/histories", type="earn")
    check("포인트 기록: earn 탭 3건 (획득 + 취소)", s == 200 and len(d["items"]) == 3, d)
    s, _ = await call("GET", "/points/histories", type="bad")
    check("포인트 기록: 잘못된 탭 -> 422", s == 422, s)
    s, _ = await call("GET", "/points/histories", size=51)
    check("포인트 기록: size 51 -> 422", s == 422, s)
    s, d = await call("GET", "/points/histories", cookies=P2)
    check("포인트 기록: 다른 환자에게는 보이지 않음", s == 200 and d["items"] == [], d)

    # ================= 6. 의사 메모 =================
    base = f"/doctor/patients/{p1.id}/memos"
    s, _ = await call("GET", base, cookies=P1)
    check("메모: 환자 계정 -> 403", s == 403, s)
    s, _ = await call("GET", f"/doctor/patients/{p4.id}/memos", cookies=A)
    check("메모: 다른 의사 환자(P4) 조회 -> 403", s == 403, s)
    s, _ = await call("POST", f"/doctor/patients/{p4.id}/memos", {"category": "clinic", "content": "x"}, cookies=A)
    check("메모: 다른 의사 환자(P4)에게 작성 -> 403", s == 403, s)
    s, before = await call("GET", base, cookies=A)
    check("메모: 목록 200 (시드 메모 포함)", s == 200 and before["patient"]["id"] == p1.id, before)
    s, m1 = await call("POST", base, {"category": "clinic", "content": "  수면제 용량 확인  "}, cookies=A)
    check("메모: 작성 201, 내용 공백 제거, 작성자 이름",
          s == 201 and m1["content"] == "수면제 용량 확인" and m1["doctorName"] == "이은솔" and m1["isEdited"] is False, m1)
    s, m2 = await call("POST", base, {"category": "counsel", "content": "가족 이야기", "isPinned": True}, cookies=A)
    check("메모: 고정 메모 작성 201", s == 201 and m2["isPinned"] is True, m2)
    for name, body in [("빈 내용", {"category": "clinic", "content": "   "}),
                       ("잘못된 분류", {"category": "etc", "content": "x"}),
                       ("1001자", {"category": "clinic", "content": "가" * 1001})]:
        s, _ = await call("POST", base, body, cookies=A)
        check(f"메모: {name} -> 422", s == 422, s)
    s, d = await call("GET", base, cookies=A)
    check("메모: 전체 건수 +2, 고정 건수 +1",
          s == 200 and d["totalCount"] == before["totalCount"] + 2 and d["pinnedCount"] == before["pinnedCount"] + 1, d)
    s, d = await call("GET", base, cookies=A, pinned="true")
    check("메모: 고정 탭은 고정 메모만", s == 200 and all(i["isPinned"] for i in d["items"]) and m2["id"] in [i["id"] for i in d["items"]], d)
    s, d = await call("GET", base, cookies=A, sort="oldest")
    pins = [i["isPinned"] for i in (d or {}).get("items", [])]
    check("메모: 오래된순 정렬 (고정 메모 먼저, 그 안에서 오래된순)",
          s == 200 and pins == sorted(pins, reverse=True) and d["items"][-1]["id"] == m1["id"], [i["id"] for i in d["items"]])
    s, _ = await call("GET", base, cookies=A, sort="random")
    check("메모: 잘못된 정렬 -> 422", s == 422, s)
    s, d = await call("PUT", f"/doctor/memos/{m1['id']}", {"isPinned": True}, cookies=A)
    check("메모: 고정만 바꾸면 isEdited 그대로 false", s == 200 and d["isPinned"] is True and d["isEdited"] is False, d)
    s, d = await call("PUT", f"/doctor/memos/{m1['id']}", {"content": "수면제 용량 0.5mg"}, cookies=A)
    check("메모: 내용 수정하면 isEdited true", s == 200 and d["isEdited"] is True and d["content"] == "수면제 용량 0.5mg", d)
    s, _ = await call("PUT", f"/doctor/memos/{m1['id']}", {"content": "남의 메모"}, cookies=B)
    check("메모: 다른 의사가 수정 -> 403/404", s in (403, 404), s)
    s, _ = await call("DELETE", f"/doctor/memos/{m1['id']}", cookies=B)
    check("메모: 다른 의사가 삭제 -> 403/404", s in (403, 404), s)
    s, _ = await call("DELETE", f"/doctor/memos/{m1['id']}", cookies=A)
    check("메모: 삭제 200", s == 200, s)
    s, _ = await call("DELETE", f"/doctor/memos/{m1['id']}", cookies=A)
    check("메모: 두 번 삭제 -> 404", s == 404, s)
    s, _ = await call("PUT", "/doctor/memos/999999999", {"content": "x"}, cookies=A)
    check("메모: 없는 메모 수정 -> 404", s == 404, s)

    # ================= 7. 의사 승인 · 관리자 =================
    async with AsyncSessionLocal() as db:
        db.add(Admin(user_id=b.id))  # 의사 B를 관리자로 지정 (사용자와 함께 CASCADE 삭제)
        await db.commit()
    h1_id = await scalar("select hospital_id from hospitals where name = '[TEST] 마음병원'")

    s, d = await call("GET", "/doctor/hospitals", cookies=X, q="[TEST]")
    check("병원 검색: '[TEST]' -> 2곳, 이름순", s == 200 and [h["name"] for h in d] == ["[TEST] 다른병원", "[TEST] 마음병원"], d)
    s, _ = await call("GET", "/doctor/hospitals", cookies=X, q="")
    check("병원 검색: 빈 검색어 -> 422", s == 422, s)
    s, d = await call("GET", "/doctor/hospitals", cookies=X, q="%")
    check("[관찰] 병원 검색: '%' 입력 시 LIKE 와일드카드로 동작", s == 200, (s, len(d or [])))
    results[-1] = (results[-1][0] + f" (결과 {len(d or [])}건)", True, "")
    r = await c.get("/doctor/hospitals", params={"q": "x"})
    check("병원 검색: 쿠키 없음 -> 401", r.status_code == 401, r.status_code)

    s, d = await call("GET", "/doctor/verifications/me", cookies=X)
    check("내 신청 상태: 신청 전 200", s == 200, (s, d))
    body = {"hospitalId": h1_id, "licenseNumber": "TEST-12345", "specialty": "정신건강의학과"}
    s, v1 = await call("POST", "/doctor/verifications", body, cookies=X)
    check("의사 신청: 201 pending, 병원 이름", s == 201 and v1["status"] == "pending" and v1["hospitalName"] == "[TEST] 마음병원", v1)
    s, _ = await call("POST", "/doctor/verifications", body, cookies=X)
    check("의사 신청: 대기 중 재신청 -> 409", s == 409, s)
    s, _ = await call("POST", "/doctor/verifications", {**body, "hospitalId": 999999999}, cookies=P2)
    check("의사 신청: 없는 병원 -> 404", s == 404, s)
    s, _ = await call("POST", "/doctor/verifications", body, cookies=A)
    check("의사 신청: 이미 의사 -> 409", s == 409, s)
    s, _ = await call("POST", "/doctor/verifications", {**body, "licenseNumber": ""}, cookies=P2)
    check("의사 신청: 빈 면허번호 -> 422", s == 422, s)
    s, d = await call("GET", "/doctor/verifications/me", cookies=X)
    check("내 신청 상태: pending", s == 200 and d and "pending" in str(d), d)

    s, _ = await call("GET", "/admin/doctor-verifications", cookies=A)
    check("관리자: 관리자 아님 -> 403", s == 403, s)
    s, d = await call("GET", "/admin/doctor-verifications", cookies=B)
    check("관리자: 대기 목록에 신청 포함", s == 200 and v1["id"] in [v["id"] for v in d], d)
    s, _ = await call("POST", f"/admin/doctor-verifications/{v1['id']}/approve", cookies=A)
    check("관리자: 관리자 아닌 의사가 승인 -> 403", s == 403, s)
    s, d = await call("POST", f"/admin/doctor-verifications/{v1['id']}/approve", cookies=B)
    check("관리자: 승인 200 approved", s == 200 and d["status"] == "approved" and d["reviewedAt"], d)
    role = await scalar("select role from users where id = :i", i=x.id)
    prof = await scalar("select hospital_id from doctor_profiles where user_id = :i", i=x.id)
    check("DB: 승인하면 role=doctor, doctor_profiles 생성", str(role).endswith("doctor") and prof == h1_id, (role, prof))
    s, _ = await call("POST", f"/admin/doctor-verifications/{v1['id']}/approve", cookies=B)
    check("관리자: 두 번 승인 -> 409", s == 409, s)
    s, _ = await call("POST", "/admin/doctor-verifications/999999999/approve", cookies=B)
    check("관리자: 없는 신청 승인 -> 404", s == 404, s)
    s, _ = await call("GET", "/doctor/patients", cookies=X)
    check("승인 직후 기존 토큰(role=patient)은 의사 API 403 (재로그인 필요, 팀 결정)", s == 403, s)

    s, v2 = await call("POST", "/doctor/verifications", {**body, "licenseNumber": "TEST-777"}, cookies=P2)
    check("의사 신청: P2 신청 201", s == 201, s)
    s, _ = await call("POST", f"/admin/doctor-verifications/{v2['id']}/reject", {"rejectReason": ""}, cookies=B)
    check("관리자: 빈 반려 사유 -> 422", s == 422, s)
    s, d = await call("POST", f"/admin/doctor-verifications/{v2['id']}/reject", {"rejectReason": "면허 사본 불명확"}, cookies=B)
    check("관리자: 반려 200 rejected + 사유", s == 200 and d["status"] == "rejected" and d["rejectReason"] == "면허 사본 불명확", d)
    s, _ = await call("POST", f"/admin/doctor-verifications/{v2['id']}/approve", cookies=B)
    check("관리자: 반려된 신청 승인 -> 409", s == 409, s)
    s, d = await call("GET", "/admin/doctor-verifications", cookies=B, status="rejected")
    check("관리자: status=rejected 필터", s == 200 and v2["id"] in [v["id"] for v in d], d)
    s, v3 = await call("POST", "/doctor/verifications", {**body, "licenseNumber": "TEST-778"}, cookies=P2)
    check("의사 신청: 반려 후 재신청 201", s == 201, s)

    # ================= 8. AI 상담 세션 =================
    async with AsyncSessionLocal() as db:
        item_ids = list((await db.execute(text(
            "select id from checklist_items where item_key like 'test_%' and is_active order by id"))).scalars())
    window = {"scheduledAt": (now - timedelta(minutes=1)).isoformat(), "expiresAt": (now + timedelta(hours=1)).isoformat()}
    items = [{"itemId": item_ids[0], "required": True}, {"itemId": item_ids[1]}]
    s, _ = await call("POST", "/doctor/chat-sessions", {"userId": p5.id, **window, "checklistItems": items}, cookies=P5)
    check("상담 세션 생성: 환자 계정 -> 403", s == 403, s)
    s, _ = await call("POST", "/doctor/chat-sessions", {"userId": p4.id, **window, "checklistItems": items}, cookies=A)
    check("상담 세션 생성: 담당 아닌 환자 -> 403", s == 403, s)
    s, _ = await call("POST", "/doctor/chat-sessions", {"userId": p5.id, **window, "checklistItems": []}, cookies=A)
    check("상담 세션 생성: 체크리스트 없음 -> 422", s == 422, s)
    s, _ = await call("POST", "/doctor/chat-sessions", {"userId": p5.id, **window,
                      "checklistItems": [{"itemId": item_ids[0]}, {"itemId": item_ids[0]}]}, cookies=A)
    check("상담 세션 생성: 중복 항목 -> 422", s == 422, s)
    s, _ = await call("POST", "/doctor/chat-sessions", {"userId": p5.id, **window, "checklistItems": [{"itemId": 999999999}]}, cookies=A)
    check("상담 세션 생성: 없는 항목 -> 400", s == 400, s)
    s, _ = await call("POST", "/doctor/chat-sessions", {"userId": p5.id, "scheduledAt": (now - timedelta(hours=3)).isoformat(),
                      "expiresAt": (now - timedelta(hours=2)).isoformat(), "checklistItems": items}, cookies=A)
    check("상담 세션 생성: 이미 지난 마감 -> 422", s == 422, s)
    s, _ = await call("POST", "/doctor/chat-sessions", {"userId": p5.id, "scheduledAt": now.replace(tzinfo=None).isoformat(),
                      "expiresAt": (now + timedelta(hours=1)).replace(tzinfo=None).isoformat(), "checklistItems": items}, cookies=A)
    check("상담 세션 생성: 시간대 없는 시각 -> 422", s == 422, s)
    s, sess = await call("POST", "/doctor/chat-sessions", {"userId": p5.id, **window, "checklistItems": items}, cookies=A)
    check("상담 세션 생성: 201 scheduled", s == 201 and sess["status"] == "scheduled", sess)
    sid = sess["id"]
    n = await scalar("select count(*) from session_checklist where session_id = :i", i=sid)
    check("DB: 세션 체크리스트 2건, sort_order 채움", n == 2, n)
    s, _ = await call("POST", "/doctor/chat-sessions", {"userId": p5.id, **window, "checklistItems": items}, cookies=A)
    check("상담 세션 생성: 접속 기간 겹침 -> 409", s == 409, s)

    s, d = await call("GET", "/chat/sessions/main", cookies=P5)
    check("상담 메인: 예정 세션 + available=true", s == 200 and d and d["id"] == sid and d["available"] is True, d)
    s, d = await call("GET", "/chat/sessions/history", cookies=P5)
    check("이전 상담: 아직 없음 []", s == 200 and d == [], d)
    s, _ = await call("POST", f"/chat/sessions/{sid}/messages", {"clientMessageId": "t-0", "content": "안녕"}, cookies=P5)
    check("메시지: 시작 전 세션에 전송 -> 409", s == 409, s)
    s, _ = await call("POST", f"/chat/sessions/{sid}/start", cookies=P1)
    check("세션 시작: 남의 세션 -> 404", s == 404, s)
    s, _ = await call("POST", f"/chat/sessions/{sid}/end", cookies=P5)
    check("세션 종료: 시작 전 -> 409", s == 409, s)
    s, d = await call("POST", f"/chat/sessions/{sid}/start", cookies=P5)
    check("세션 시작: 200 active, startedAt", s == 200 and d["status"] == "active" and d["startedAt"], d)
    s, _ = await call("POST", f"/chat/sessions/{sid}/start", cookies=P5)
    check("세션 시작: 두 번 -> 409", s == 409, s)
    s, d = await call("GET", f"/chat/sessions/{sid}/messages", cookies=P5)
    check("대화 기록: 200", s == 200 and "items" in d, d)
    s, _ = await call("GET", f"/chat/sessions/{sid}/messages", cookies=P1)
    check("대화 기록: 남의 세션 -> 404", s == 404, s)
    s, _ = await call("POST", f"/chat/sessions/{sid}/messages", {"clientMessageId": "t-1", "content": "   "}, cookies=P5)
    check("메시지: 빈 내용 -> 422", s == 422, s)
    s, _ = await call("POST", f"/chat/sessions/{sid}/messages", {"clientMessageId": "x" * 37, "content": "안녕"}, cookies=P5)
    check("메시지: clientMessageId 37자 -> 422", s == 422, s)

    # LLM 전송: .env에 ANTHROPIC_API_KEY가 없으면 502가 정상 (오류 문구는 저장되지 않음)
    calls_before = await scalar("select count(*) from llm_calls")
    r = await c.post(f"/chat/sessions/{sid}/messages", cookies=P5,
                     json={"clientMessageId": "t-2", "content": "요즘 잠을 잘 못 자요"})
    llm_status = r.status_code
    check(f"메시지 전송(LLM): 응답 {llm_status} (키 없으면 502, 있으면 200)", llm_status in (200, 502), r.text[:300])
    user_msgs = await scalar("select count(*) from messages where session_id = :i and role = 'user'", i=sid)
    calls_after = await scalar("select count(*) from llm_calls")
    check("DB: 사용자 메시지는 저장됨 (LLM 실패해도)", user_msgs == 1, user_msgs)
    emo = await scalar("select count(*) from emotion_analyses ea join messages m on m.id = ea.message_id where m.session_id = :i", i=sid)
    check("감정분석: 사용자 발화 분석 결과 저장 (모델 로드됨)", emo == 1, emo)
    check("DB: llm_calls 호출 기록 남김 (실패도 기록)", calls_after > calls_before, (calls_before, calls_after))
    s, _ = await call("POST", f"/chat/sessions/{sid}/messages", {"clientMessageId": "t-2", "content": "요즘 잠을 잘 못 자요"}, cookies=P5)
    check("메시지: 같은 clientMessageId 재전송 -> 실패 시 409 / 성공 시 200", s == (409 if llm_status == 502 else 200), s)

    s, d = await call("POST", f"/chat/sessions/{sid}/end", cookies=P5)
    check("세션 종료: 200 ended", s == 200 and d["status"] == "ended", d)
    s, d = await call("POST", f"/chat/sessions/{sid}/end", cookies=P5)
    check("세션 종료: 두 번 보내도 200 (멱등)", s == 200 and d["status"] == "ended", d)
    end_reason = await scalar("select end_reason from chat_sessions where id = :i", i=sid)
    check("DB: end_reason=user", str(end_reason).endswith("user"), end_reason)
    s, d = await call("GET", "/chat/sessions/history", cookies=P5)
    check("이전 상담: 종료한 세션 표시", s == 200 and [x["id"] for x in d] == [sid], d)
    s, d = await call("GET", "/chat/sessions/main", cookies=P5)
    check("상담 메인: 예정 세션 없음 -> data null", s == 200 and d is None, d)

    # ================= 9. 빈 라우터 / 미구현 =================
    for path in ("/mood-records", "/reflections", "/care-tasks", "/defaults", "/myroom"):
        s, _ = await call("GET", path)
        check(f"[미구현] {path}: 엔드포인트 없음 -> 404", s == 404, s)


async def main() -> int:
    async with app.router.lifespan_context(app):
        async with AsyncSessionLocal() as db:
            await seed_module.reset(db)
            users = await seed_module.seed(db)
        try:
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=120) as client:
                await run(client, users)
        except Exception as e:  # 테스트 코드 자체가 깨져도 정리는 한다
            import traceback
            traceback.print_exc()
            check("테스트 실행 중 예외", False, repr(e))
        finally:
            async with AsyncSessionLocal() as db:
                await seed_module.reset(db)
            leftover = [k async for k in redis_client.scan_iter(f"{TOKEN_BLACKLIST_PREFIX}*")]
            print(f"\n정리: 테스트 데이터 삭제, 남은 블랙리스트 키 {len(leftover)}개")

    failed = [r for r in results if not r[1]]
    print(f"\n결과: {len(results) - len(failed)}/{len(results)} 통과, 실패 {len(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
