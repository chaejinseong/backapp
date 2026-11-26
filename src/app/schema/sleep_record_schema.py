from datetime import date, datetime
from pydantic import AwareDatetime
from app.schema.common_schema import BaseDTO
from app.enums.sleep_record_enum import SleepRecordSource

# 수면 기록 요청/응답 DTO
# 규칙: BaseDTO 상속, XxxCreateDTO / XxxUpdateDTO / XxxResponseDTO 이름 사용
# - sleep_minutes, source는 클라이언트에게 받지 않고 서버가 채운다.
# - 시각은 시간대가 있어야 한다 (예: 2026-11-26T07:00:00+09:00). 시간대가 없으면 422


# 수면 기록 작성 요청
class SleepRecordCreateDTO(BaseDTO):
    sleep_at: AwareDatetime  # 잠든 시각
    wake_at: AwareDatetime   # 일어난 시각
    # 기상한 날 (보내지 않으면 wake_at의 한국 날짜)
    record_date: date | None = None


# 수면 기록 수정 요청 (보낸 값만 수정)
class SleepRecordUpdateDTO(BaseDTO):
    sleep_at: AwareDatetime | None = None
    wake_at: AwareDatetime | None = None
    record_date: date | None = None


# 수면 기록 응답
class SleepRecordResponseDTO(BaseDTO):
    sleep_id: int
    record_date: date
    sleep_at: datetime | None
    wake_at: datetime | None
    sleep_minutes: int | None
    source: SleepRecordSource
    created_at: datetime
    updated_at: datetime | None
