from fastapi import Depends
from app.repository.mood_record_repository import MoodRecordRepository, get_mood_record_repository
from datetime import date, timedelta
from fastapi import HTTPException, status
from app.models.emotion_record_model import EmotionRecord
from app.util.date_util import today_kst, now_kst
from app.schema.mood_record_schema import (
    MoodRecordCreateDTO,
    MoodRecordUpdateDTO,
    MoodRecordResponseDTO,
    MoodRecordDetailResponseDTO,
)

PAST_DAYS_LIMIT = 7   # 지난 날짜는 7일 전까지 기록 가능 (일기와 같게)

# 마음 기록 (마음 날씨/키워드/한 줄) 비즈니스 로직, 예외 처리, 트랜잭션 관리
class MoodRecordService:

    def __init__(self, repo: MoodRecordRepository):
        self.repo = repo

    # TODO: 서비스 메서드 추가
    
    #날짜 확인: 미래 X, 7일 보다 지난 날 X
    @staticmethod
    def _check_date(day:date) -> None:
        today = today_kst()
        if day>today: 
            raise HTTPException(
                status_code=status.HTTP_404_BAD_REQUEST,
                detail = "미래 날짜는 기록할 수 없습니다"           
            )
    #ORM -> 응답 DTO (칼럼 이름과 DTO 필드 이름이 같아서 그대로 옮겨진다)

def get_mood_record_service(repo: MoodRecordRepository = Depends(get_mood_record_repository)) -> MoodRecordService:
    return MoodRecordService(repo)
