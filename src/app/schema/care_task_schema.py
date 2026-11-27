from app.schema.common_schema import BaseDTO
from datetime import date, datetime
from app.enums.mission_enum import MissionCategory
# 오늘의 작은 돌봄 / 나만의 돌봄 (미션과 같은 개념으로 묶어서 처리) 요청/응답 DTO
# 규칙: BaseDTO 상속, XxxCreateDTO / XxxUpdateDTO / XxxResponseDTO 이름 사용

# TODO: DTO 추가

#일일목표 1개(화면; 홈 일일목표, 오늘의 일일 목표, 오늘의 작은 돌봄)
class CareTaskItemResponseDTO(BaseDTO):
    mission_id: int
    name: str
    description: str | None=None
    category:MissionCategory | None=None    #body, habit, mind
    reward_point: int
    is_completed: bool  # True-> 오늘 한 일, False-> 해 볼 만한 것
    completed_at:datetime | None=None   # 오늘 완료한 시각, 미완료면 None

#오늘의 일일목표 화면 전체 (모두 완료 보너스는 목록에 넣지 않고 따로 보낸다)
class TodayCareTaskResponseDTO(BaseDTO):
    today: date
    missions: list[CareTaskItemResponseDTO]  # 표시 순서(sort_order)대로
    completed_count: int #2/3 에서 2
    total_count: int    #2/3에서 3
    bonus_point: int | None=None    # 모두 완료 보너스 포인트, 보너스 미션이 없으면 None
    bonus_received: bool    # 오늘 보너스를 받았는지
    earned_today: int   #오늘 모은 포인트

#미션 달성 결과 - 기능 API(일기, 상담 등) 응답에 같이 담아 프론트에 보낸다
class MissionCompleteResponseDTO(BaseDTO):
    mission_id: int
    newly_completed: bool   # 이번 요청으로 완료됐으면 True, 오늘 이미 완료한 미션이면 False
    completed_count: int    # 오늘 완료한 화면 미션 수 (2/3 에서 2)
    total_count: int        # 화면 미션 수 (2/3 에서 3)
    bonus_completed: bool   # 이번 요청으로 모두 완료 보너스를 달성했으면 True
    earned_point: int       # 이번 요청으로 받은 포인트 (미션 + 보너스), 없으면 0

#이번 주 돌아보기 - 하루. -> 오늘의 일일목표 화면 아래에 있는 막대그래프 부분
class WeeklyDayResponseDTO(BaseDTO):
    day: date
    completed_count: int

# 이번 주 돌아보기 - 월요일 ~ 일요일 7개. -> 그 그래프 전체
class WeeklyCareTaskResponseDTO(BaseDTO):
    days: list[WeeklyDayResponseDTO]
    total_count: int    #하루 목표 수(막대 높이 기준)

#포인트 모으는법 (포인트샵 '모으는 법'버튼)
class MissionGuideResponseDTO(BaseDTO):
    mission_id: int
    name: str
    reward_point: int
    daily_limit: int
    is_auto: bool