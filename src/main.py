import asyncio
from fastapi import FastAPI
from app.security.cors import setup_cors
from app.security.security_headers import set_up_security
from contextlib import asynccontextmanager
from app.infrastructure.postgresql import engine, Base, AsyncSessionLocal
from app.infrastructure.redis import start_redis_client
from app.infrastructure.s3 import start_s3_client
from app.ml.emotion_analysis import EmotionAnalyzer
import app.models
from app.api import default_api, auth_api, user_api, memo_api, doctor_verification_api, doctor_api, mypage_api, mood_record_api, reflection_api, diary_api, calendar_api, care_task_api, point_api, llm_api, care_request_api, sleep_record_api
from app.service.schedule_reminder_service import run_schedule_reminder_loop
from app.service.care_request_service import run_care_request_expire_loop

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 비동기 연결
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        print("PostgreSQL 연결 성공!")

    async with start_redis_client(app):
        print("Redis 연결 성공!")

        async with start_s3_client(app):
            print("S3 연결 성공!")

            # 4. 감정분석 모델 로드 (서버가 요청을 받기 전에. 실패하면 감정분석 없이 진행)
            try:
                app.state.emotion_analyzer = await asyncio.to_thread(EmotionAnalyzer)
                print(f"감정분석 모델 로드 성공! ({app.state.emotion_analyzer.model_version})")
            except Exception as e:
                app.state.emotion_analyzer = None
                print(f"감정분석 모델 로드 실패: {e!r}")

            # 5. 백그라운드 스케줄러 시작 (일정 알림, 진료 요청 무응답 처리. 서버 종료 시 취소)
            # - lifespan의 yield는 이 한 곳뿐이다. yield 앞은 시작 처리, finally는 종료 처리
            background_tasks = [
                asyncio.create_task(run_schedule_reminder_loop(AsyncSessionLocal)),
                asyncio.create_task(run_care_request_expire_loop(AsyncSessionLocal)),
            ]
            try:
                yield
            finally:
                # 종료 시 스케줄러 task를 모두 멈춘다
                for task in background_tasks:
                    task.cancel()
                await asyncio.gather(*background_tasks, return_exceptions=True)

app = FastAPI(
    lifespan=lifespan,
    title="Fast API Service",
    description="파이썬 백엔드 구축!😎",
    version="1.0.0",
    docs_url="/docs", # Swagger
    redoc_url="/redoc"
)

setup_cors(app) 
set_up_security(app)

@app.get("/")
def health_check():
    return {"message": "Hello World"}






app.include_router(default_api.router, prefix="/defaults", tags=["defaults"])
app.include_router(auth_api.router, prefix="/auth", tags=["auth"])
app.include_router(user_api.router, prefix="/users", tags=["users"])
app.include_router(doctor_api.router, prefix="/doctor", tags=["doctor"])
app.include_router(calendar_api.router, prefix="/calendar", tags=["calendar"])
app.include_router(memo_api.router, prefix="/doctor", tags=["memo"])
app.include_router(mypage_api.router, prefix="/mypage", tags=["mypage"])
app.include_router(mood_record_api.router, prefix="/mood-records", tags=["mood-records"])
app.include_router(reflection_api.router, prefix="/reflections", tags=["reflections"])
app.include_router(diary_api.router, prefix="/diaries", tags=["diaries"])
app.include_router(sleep_record_api.router, prefix="/sleep-records", tags=["sleep-records"])
app.include_router(care_task_api.router, prefix="/care-tasks", tags=["care-tasks"])
app.include_router(doctor_verification_api.router, prefix="/doctor", tags=["doctor-verification"])
app.include_router(doctor_verification_api.admin_router, prefix="/admin", tags=["admin"])
app.include_router(llm_api.router, prefix="/chat", tags=["chat"])
app.include_router(llm_api.doctor_router, prefix="/doctor", tags=["doctor-chat"])
app.include_router(point_api.router, prefix="/points", tags=["points"])
app.include_router(care_request_api.router, prefix="/care-requests", tags=["care-requests"])
app.include_router(care_request_api.doctor_router, prefix="/doctor", tags=["doctor-care-requests"])
