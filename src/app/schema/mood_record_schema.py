from app.schema.common_schema import BaseDTO
from datetime import date, datetime
from typing import Literal
from pydantic import Field

# 마음 기록 (마음 날씨/키워드/한 줄) 요청/응답 DTO
# 규칙: BaseDTO 상속, XxxCreateDTO / XxxUpdateDTO / XxxResponseDTO 이름 사용

# TODO: DTO 추가

#마음 키워드 10개
MoodTag = Literal["평온", "기쁨", "감사","설렘","뿌듯함","피곤","불안","우울","외로움","무기력"]

#기록하기(POST / mood-records)
class MoodRecordCreateDTO(BaseDTO):
    record_date: date | None = None #없으면 오늘
    mood_score: int = Field(ge=1,le=5)  #마음 날씨: 1 비와요 ~ 5 화창해요
    mood_tag: MoodTag | None=None   #마음 키워드 - 안 골라도 됨
    memo: str | None = Field(defalt=None, max_length=255)   #한 줄 남기기 - 쓰고 싶을 때만

#수정하기 & 선생님께 보여주기(PATCH /mood-records/{emotionId}) - 보낸 값만 바뀐다
class MoodRecordUpdateDTO(BaseDTO):
    mood_score: int | None = Field(default=None, ge=1, le=5)
    mood_tag: MoodTag | None=None
    memo: str | None = Field(defalt=None, max_length=255) 
    is_shared: bool | None = None

#마음 기록 1개(오늘 카드, 상세, 저장&수정 결과)
class MoodRecordResponseDTO(BaseDTO):
    emotion_id: int
    record_date:date
    mood_score: int
    mood_tag: str | None = None
    memo: str| None = None
    is_shared: bool
    created_at: datetime
    updated_at: datetime | None=None

#날짜별 상세 (GET / mood-record/days/{day})
class MoodRecordDetailResponseDTO(BaseDTO):
    record: MoodRecordResponseDTO | None = None   #그 날 기록이 없으면 null
    done_missions: list[str] = []   #이날 한 돌봄(그날 완료한 일일목표 이름)


    
