import math
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from fastapi import Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from app.repository.sleep_record_repository import SleepRecordRepository, get_sleep_record_repository
from app.models.sleep_record_model import SleepRecord
from app.enums.sleep_record_enum import SleepRecordSource
from app.schema.common_schema import PageRequestDTO, PageResponseDTO
from app.schema.sleep_record_schema import SleepRecordCreateDTO, SleepRecordUpdateDTO, SleepRecordResponseDTO

KST = timezone(timedelta(hours=9)) # UTC+9

# 수면 시간은 24시간을 넘을 수 없다.
MAX_SLEEP_DURATION = timedelta(hours=24)


# 수면 기록 비즈니스 로직, 예외 처리, 트랜잭션 관리
class SleepRecordService:

    def __init__(self, repo: SleepRecordRepository):
        self.repo = repo


    # 날짜 -> 한국 시간 자정 (record_date는 DateTime 컬럼이라 날짜를 자정으로 저장한다.)
    @staticmethod
    def _to_kst_start(day: date) -> datetime:
        return datetime(day.year, day.month, day.day, tzinfo=KST)


    # ORM -> 응답 DTO (날짜·시각은 한국 시간 기준으로 변환)
    @staticmethod
    def _to_response(record: SleepRecord) -> SleepRecordResponseDTO:
        return SleepRecordResponseDTO(
            sleep_id=record.sleep_id,
            record_date=record.record_date.astimezone(KST).date(),
            sleep_at=record.sleep_at.astimezone(KST) if record.sleep_at else None,
            wake_at=record.wake_at.astimezone(KST) if record.wake_at else None,
            sleep_minutes=record.sleep_minutes,
            source=record.source,
            created_at=record.created_at,
            updated_at=record.updated_at
        )


    # 수면 시각 검증 후 수면 시간(분) 계산
    # - 일어난 시각은 잠든 시각보다 늦어야 하고, 현재 시각보다 미래일 수 없다.
    # - 수면 시간은 24시간을 넘을 수 없다.
    @staticmethod
    def _validate_and_minutes(sleep_at: datetime, wake_at: datetime) -> int:
        if wake_at <= sleep_at:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="일어난 시각은 잠든 시각보다 늦어야 합니다."
            )
        if wake_at > datetime.now(KST):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="미래 시각의 수면 기록은 작성할 수 없습니다."
            )
        duration = wake_at - sleep_at
        if duration > MAX_SLEEP_DURATION:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="수면 시간은 24시간을 넘을 수 없습니다."
            )
        return int(duration.total_seconds() // 60)


    # 같은 날(기상한 날) 기록이 이미 있을 때 409
    @staticmethod
    def _conflict() -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="해당 날짜에 이미 수면 기록이 있습니다."
        )


    # 같은 날(기상한 날) 기록은 하나만 (exclude_id: 수정 중인 자기 자신은 제외)
    async def _check_duplicate(self, user_id: int, record_date: datetime, exclude_id: int | None = None) -> None:
        exists = await self.repo.find_by_record_date(user_id, record_date)
        if exists and exists.sleep_id != exclude_id:
            raise self._conflict()


    # 수면 기록 작성 (사용자가 직접 입력 -> source = MANUAL)
    async def create_sleep_record(self, user_id: int, dto: SleepRecordCreateDTO) -> SleepRecordResponseDTO:
        sleep_minutes = self._validate_and_minutes(dto.sleep_at, dto.wake_at)

        # 기상한 날을 보내지 않으면 wake_at의 한국 날짜
        record_date = self._to_kst_start(dto.record_date or dto.wake_at.astimezone(KST).date())
        await self._check_duplicate(user_id, record_date)

        try:
            record = await self.repo.create_sleep_record(
                user_id, record_date, dto.sleep_at, dto.wake_at, sleep_minutes, SleepRecordSource.MANUAL
            )
        except IntegrityError:
            # 중복 검사 뒤 같은 날 기록이 동시에 들어온 경우 (유니크 제약)
            raise self._conflict()
        return self._to_response(record)


    # 수면 기록 목록 (month: "YYYY-MM"이면 해당 월만)
    async def get_sleep_records(
        self, user_id: int, page: PageRequestDTO, month: str | None = None
    ) -> PageResponseDTO[SleepRecordResponseDTO]:
        start = end = None
        if month:
            try:
                year, mon = (int(x) for x in month.split("-"))
                first = date(year, mon, 1)
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="month는 YYYY-MM 형식이어야 합니다."
                )
            start = self._to_kst_start(first)
            end = self._to_kst_start(first + timedelta(days=monthrange(year, mon)[1]))

        records, total = await self.repo.find_sleep_records(user_id, page.offset, page.limit, start, end)

        return PageResponseDTO(
            total_count=total,
            total_pages=math.ceil(total / page.limit),
            offset=page.offset,
            limit=page.limit,
            items=[self._to_response(r) for r in records]
        )


    # 수면 기록 상세
    async def get_sleep_record(self, user_id: int, sleep_id: int) -> SleepRecordResponseDTO:
        record = await self.repo.find_sleep_record(user_id, sleep_id)
        if not record:
            raise self._not_found()
        return self._to_response(record)


    # 수면 기록 수정 (보낸 값만 수정)
    # 기존 기록과 보낸 값을 합쳐서 규칙을 다시 검증하고, 수면 시간을 다시 계산한다.
    async def update_sleep_record(
        self, user_id: int, sleep_id: int, dto: SleepRecordUpdateDTO
    ) -> SleepRecordResponseDTO:
        record = await self.repo.find_sleep_record(user_id, sleep_id)
        if not record:
            raise self._not_found()

        update_data = dto.model_dump(exclude_unset=True, exclude_none=True)
        if not update_data:
            return self._to_response(record)

        sleep_at = update_data.get("sleep_at", record.sleep_at)
        wake_at = update_data.get("wake_at", record.wake_at)
        if sleep_at is None or wake_at is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="잠든 시각과 일어난 시각이 모두 필요합니다."
            )
        update_data["sleep_minutes"] = self._validate_and_minutes(sleep_at, wake_at)

        # 기상한 날이 바뀌면 같은 날 중복 검사
        if "record_date" in update_data:
            update_data["record_date"] = self._to_kst_start(update_data["record_date"])
            if update_data["record_date"] != record.record_date:
                await self._check_duplicate(user_id, update_data["record_date"], exclude_id=sleep_id)

        try:
            updated = await self.repo.update_sleep_record(user_id, sleep_id, update_data)
        except IntegrityError:
            # 중복 검사 뒤 같은 날로 동시에 바뀐 경우 (유니크 제약)
            raise self._conflict()
        if not updated:
            raise self._not_found()
        return self._to_response(updated)


    # 수면 기록 삭제
    async def delete_sleep_record(self, user_id: int, sleep_id: int) -> None:
        if not await self.repo.delete_sleep_record(user_id, sleep_id):
            raise self._not_found()


    # 없거나 남의 기록은 모두 404 (존재 여부를 노출하지 않는다.)
    @staticmethod
    def _not_found() -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="수면 기록이 존재하지 않습니다."
        )



def get_sleep_record_service(
    repo: SleepRecordRepository = Depends(get_sleep_record_repository)
) -> SleepRecordService:
    return SleepRecordService(repo)
