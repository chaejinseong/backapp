"""
환자 수면 기록 API 통합 테스트 (실제 DB·Redis 사용)

실행 (backapp/src 에서):
    python ../tests/sleep_record_api_test.py

흐름: 서버 시작(lifespan) -> 테스트 데이터 넣기 -> HTTP로 API 호출 -> DB 행 확인 -> 테스트 데이터 정리.
결과 출력·토큰·DB 조회 도우미는 doctor_api_test.py의 것을 같이 쓴다.
테스트 사용자를 지우면 수면 기록은 FK의 ON DELETE CASCADE로 같이 지워진다.
"""
import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402
from doctor_api_test import app, AsyncSessionLocal, KST, results, check, token_for, scalar  # noqa: E402
import seed_doctor_test_data as seed_module  # noqa: E402


async def run(c: httpx.AsyncClient, users: dict) -> None:
    p1, p2 = users["p1"], users["p2"]
    P1 = {"access_token": token_for(p1)}
    P2 = {"access_token": token_for(p2)}
    now = datetime.now(KST)
    today = now.date()
    day = lambda offset: (today + timedelta(days=offset)).isoformat()
    # offset일 전 날짜의 hour시 (한국 시간, ISO 문자열)
    at = lambda offset, hour, minute=0: datetime(
        *(today + timedelta(days=offset)).timetuple()[:3], hour, minute, tzinfo=KST
    ).isoformat()

    async def call(method, path, body=None, cookies=P1, **params):
        r = await c.request(method, path, cookies=cookies, json=body, params=params)
        return r.status_code, r.json().get("data")

    # ===== 권한 =====
    r = await c.get("/sleep-records")
    check("권한: 쿠키 없음 -> 401", r.status_code == 401, r.status_code)

    # ===== 작성 =====
    s, d = await call("POST", "/sleep-records", {"sleepAt": at(-2, 23), "wakeAt": at(-1, 7, 30)})
    check("작성: 201, record_date = wake_at 날짜, 8시간 30분, manual",
          s == 201 and d["recordDate"] == day(-1) and d["sleepMinutes"] == 510 and d["source"] == "manual", d)
    first_id = d["sleepId"]
    row_date = await scalar("select record_date from sleep_records where sleep_id = :i", i=first_id)
    check("DB: record_date는 한국 시간 자정", row_date.astimezone(KST).hour == 0 and row_date.astimezone(KST).date().isoformat() == day(-1), row_date)

    s, d = await call("POST", "/sleep-records", {"sleepAt": at(-3, 1), "wakeAt": at(-3, 6), "recordDate": day(-5)})
    check("작성: record_date 직접 지정", s == 201 and d["recordDate"] == day(-5) and d["sleepMinutes"] == 300, d)
    second_id = d["sleepId"]

    s, _ = await call("POST", "/sleep-records", {"sleepAt": at(-2, 22), "wakeAt": at(-1, 6)})
    check("작성: 같은 날 두 번 -> 409", s == 409, s)
    s, _ = await call("POST", "/sleep-records", {"sleepAt": at(-2, 22), "wakeAt": at(-1, 6)}, cookies=P2)
    check("작성: 다른 환자는 같은 날 작성 가능 -> 201", s == 201, s)

    # 같은 날 동시 요청: 중복 검사를 둘 다 통과해도 유니크 제약으로 하나만 저장된다.
    n = await scalar("select count(*) from pg_constraint where conname = 'uq_sleep_records_user_id_record_date'")
    check("DB: (user_id, record_date) 유니크 제약 있음", n == 1, n)
    race = {"sleepAt": at(-8, 23), "wakeAt": at(-7, 7)}
    codes = sorted(x[0] for x in await asyncio.gather(*(call("POST", "/sleep-records", race) for _ in range(3))))
    check("작성: 같은 날 동시 3번 -> 201 하나, 나머지 409", codes == [201, 409, 409], codes)
    race_id = await scalar("select sleep_id from sleep_records where user_id = :u and record_date = :d",
                           u=p1.id, d=datetime(*(today - timedelta(days=7)).timetuple()[:3], tzinfo=KST))
    s, _ = await call("DELETE", f"/sleep-records/{race_id}")
    check("동시 요청으로 만든 기록 정리", s == 200, s)

    future = (now + timedelta(hours=2)).isoformat()
    for name, bad, code in [
        ("wake <= sleep -> 400", {"sleepAt": at(-10, 7), "wakeAt": at(-10, 7)}, 400),
        ("wake가 미래 -> 400", {"sleepAt": (now - timedelta(hours=1)).isoformat(), "wakeAt": future}, 400),
        ("24시간 초과 -> 400", {"sleepAt": at(-12, 6), "wakeAt": at(-11, 7)}, 400),
        ("시간대 없는 시각 -> 422", {"sleepAt": f"{day(-10)}T23:00:00", "wakeAt": f"{day(-9)}T07:00:00"}, 422),
        ("wake_at 누락 -> 422", {"sleepAt": at(-10, 23)}, 422),
    ]:
        s, _ = await call("POST", "/sleep-records", bad)
        check(f"작성: {name}", s == code, s)

    # ===== 목록 =====
    s, d = await call("GET", "/sleep-records")
    check("목록: 내 기록 2개, 기상한 날 최신순",
          s == 200 and d["totalCount"] == 2 and [x["sleepId"] for x in d["items"]] == [first_id, second_id], d)
    s, d = await call("GET", "/sleep-records", limit=1, offset=1)
    check("목록: 페이지네이션", d["totalPages"] == 2 and [x["sleepId"] for x in d["items"]] == [second_id], d)
    month = (today - timedelta(days=1)).strftime("%Y-%m")
    s, d = await call("GET", "/sleep-records", month=month)
    check("목록: month 필터 (어제가 속한 달)", s == 200 and first_id in [x["sleepId"] for x in d["items"]], d)
    s, d = await call("GET", "/sleep-records", month="2000-01")
    check("목록: 기록 없는 달 -> 빈 목록", s == 200 and d["totalCount"] == 0 and d["items"] == [], d)
    s, _ = await call("GET", "/sleep-records", month="2026-13")
    check("목록: month=2026-13 -> 400", s == 400, s)

    # ===== 상세 =====
    s, d = await call("GET", f"/sleep-records/{first_id}")
    check("상세: 200", s == 200 and d["sleepId"] == first_id, d)
    s, _ = await call("GET", f"/sleep-records/{first_id}", cookies=P2)
    check("상세: 남의 기록 -> 404", s == 404, s)
    s, _ = await call("GET", "/sleep-records/999999999")
    check("상세: 없는 기록 -> 404", s == 404, s)

    # ===== 수정 =====
    s, d = await call("PATCH", f"/sleep-records/{first_id}", {"sleepAt": at(-2, 22)})
    check("수정: 보낸 값만 (잠든 시각만 바꿈), 수면 시간 재계산, 날짜 유지",
          s == 200 and d["sleepMinutes"] == 570 and d["recordDate"] == day(-1) and d["updatedAt"] is not None, d)
    n = await scalar("select sleep_minutes from sleep_records where sleep_id = :i", i=first_id)
    check("DB: sleep_minutes 재계산", n == 570, n)
    s, _ = await call("PATCH", f"/sleep-records/{first_id}", {"wakeAt": at(-2, 21)})
    check("수정: 합친 값이 wake <= sleep -> 400", s == 400, s)
    s, _ = await call("PATCH", f"/sleep-records/{first_id}", {"sleepAt": at(-4, 6)})
    check("수정: 합친 값이 24시간 초과 -> 400", s == 400, s)
    s, _ = await call("PATCH", f"/sleep-records/{first_id}", {"recordDate": day(-5)})
    check("수정: 다른 기록과 같은 날로 변경 -> 409", s == 409, s)
    s, d = await call("PATCH", f"/sleep-records/{first_id}", {"recordDate": day(-1)})
    check("수정: 자기 날짜 그대로 -> 200", s == 200 and d["recordDate"] == day(-1), s)
    s, d = await call("PATCH", f"/sleep-records/{first_id}", {"recordDate": day(-6)})
    check("수정: 빈 날짜로 변경 -> 200", s == 200 and d["recordDate"] == day(-6), d)
    s, _ = await call("PATCH", f"/sleep-records/{first_id}", {"sleepAt": at(-2, 23)}, cookies=P2)
    check("수정: 남의 기록 -> 404", s == 404, s)

    # ===== 삭제 =====
    s, _ = await call("DELETE", f"/sleep-records/{first_id}", cookies=P2)
    check("삭제: 남의 기록 -> 404", s == 404, s)
    s, _ = await call("DELETE", f"/sleep-records/{first_id}")
    check("삭제: 내 기록 -> 200", s == 200, s)
    n = await scalar("select count(*) from sleep_records where sleep_id = :i", i=first_id)
    check("DB: 실제 삭제됨 (hard delete)", n == 0, n)
    s, _ = await call("DELETE", f"/sleep-records/{first_id}")
    check("삭제: 두 번 -> 404", s == 404, s)


async def main() -> int:
    async with app.router.lifespan_context(app):
        async with AsyncSessionLocal() as db:
            await seed_module.reset(db)
            users = await seed_module.seed(db)
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                await run(client, users)
        except Exception as e:  # 테스트 코드 자체가 깨져도 정리는 한다
            import traceback
            traceback.print_exc()
            check("테스트 실행 중 예외", False, repr(e))
        finally:
            async with AsyncSessionLocal() as db:
                await seed_module.reset(db)
            print("\n정리: 테스트 데이터 삭제")

    failed = [r for r in results if not r[1]]
    print(f"\n결과: {len(results) - len(failed)}/{len(results)} 통과, 실패 {len(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
