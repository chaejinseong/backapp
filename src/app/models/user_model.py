from datetime import date, datetime, timedelta, timezone
from sqlalchemy import BigInteger, String, Integer, Date, DateTime, Identity, UniqueConstraint, Enum as SQLEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.enums.user_enum import UserRole, UserSocialProvider, UserGender

# PostgreSQL의 Base를 상속받아서 서버가 켜질 때 동기화시킨다.
from app.infrastructure.postgresql import Base
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.admin_model import Admin
    from app.models.user_setting_model import UserSetting
    from app.models.doctor_profile_model import DoctorProfile
    from app.models.doctor_verification_model import DoctorVerification
    from app.models.patient_chart_model import PatientChart
    from app.models.care_relation_model import CareRelation
    from app.models.chat_session_model import ChatSession
    from app.models.assessment_result_model import AssessmentResult
    from app.models.risk_event_model import RiskEvent
    from app.models.risk_event_action_model import RiskEventAction
    from app.models.daily_risk_score_model import DailyRiskScore
    from app.models.care_request_model import CareRequest
    from app.models.request_contact_model import RequestContact
    from app.models.request_message_model import RequestMessage
    from app.models.appointment_model import Appointment
    from app.models.appointment_change_model import AppointmentChange
    from app.models.schedule_model import Schedule
    from app.models.notification_model import Notification
    from app.models.doctor_memo_model import DoctorMemo
    from app.models.diary_model import Diary
    from app.models.emotion_record_model import EmotionRecord
    from app.models.sleep_record_model import SleepRecord
    from app.models.exercise_record_model import ExerciseRecord
    from app.models.attendance_model import Attendance
    from app.models.character_model import Character
    from app.models.user_flower_model import UserFlower
    from app.models.user_expression_model import UserExpression
    from app.models.user_item_model import UserItem
    from app.models.myroom_entity_model import MyroomEntity
    from app.models.care_action_log_model import CareActionLog
    from app.models.point_history_model import PointHistory
    from app.models.user_mission_log_model import UserMissionLog

KST = timezone(timedelta(hours=9)) # UTC+9

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)  # 회원 PK
    email: Mapped[str | None] = mapped_column(String(255))  # 로그인 이메일 (소셜이 안 줄 수 있음)
    nickname: Mapped[str | None] = mapped_column(String(50))  # 화면 표시용
    role: Mapped[UserRole] = mapped_column(
        SQLEnum(UserRole, native_enum=False, values_callable=lambda e: [m.value for m in e]),
        nullable=False
    )  # 역할 (patient·doctor)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(KST)
    )  # 가입 시각
    social_provider: Mapped[UserSocialProvider] = mapped_column(
        SQLEnum(UserSocialProvider, native_enum=False, values_callable=lambda e: [m.value for m in e]),
        nullable=False
    )  # 소셜 제공처
    social_id: Mapped[str] = mapped_column(String(100), nullable=False)  # 소셜 사용자 번호
    name: Mapped[str] = mapped_column(String(50), nullable=False)  # 이름. 없으면 '익명유저'+회원 id
    gender: Mapped[UserGender | None] = mapped_column(
        SQLEnum(UserGender, native_enum=False, values_callable=lambda e: [m.value for m in e])
    )  # 성별
    birth_date: Mapped[date | None] = mapped_column(Date)  # 생년월일
    point_balance: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # 현재 포인트
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        onupdate=lambda: datetime.now(KST)
    )  # 수정 시각
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # 탈퇴 시각
    account_level: Mapped[int] = mapped_column(Integer, nullable=False, default=1)  # 계정 레벨 (exp 50마다 +1)
    account_exp: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # 누적 경험치

    __table_args__ = (
        UniqueConstraint("social_provider", "social_id", name="uq_users_social_provider_social_id"),
    )

    # 관계
    # cascade: User가 삭제되면 참조된 테이블을 모두 같이 연쇄삭제
    admin: Mapped["Admin | None"] = relationship(
        "Admin", back_populates="user", uselist=False,
        cascade="all, delete-orphan", passive_deletes=True
    )
    setting: Mapped["UserSetting | None"] = relationship(
        "UserSetting", back_populates="user", uselist=False,
        cascade="all, delete-orphan", passive_deletes=True
    )
    doctor_profile: Mapped["DoctorProfile | None"] = relationship(
        "DoctorProfile", back_populates="user", uselist=False,
        cascade="all, delete-orphan", passive_deletes=True
    )
    doctor_verifications: Mapped[list["DoctorVerification"]] = relationship(
        "DoctorVerification", back_populates="user",
        cascade="all, delete-orphan", passive_deletes=True
    )
    patient_charts: Mapped[list["PatientChart"]] = relationship(
        "PatientChart", back_populates="patient",
        cascade="all, delete-orphan", passive_deletes=True
    )
    patient_care_relations: Mapped[list["CareRelation"]] = relationship(
        "CareRelation", back_populates="patient", foreign_keys="[CareRelation.patient_id]",
        cascade="all, delete-orphan", passive_deletes=True
    )
    doctor_care_relations: Mapped[list["CareRelation"]] = relationship(
        "CareRelation", back_populates="doctor", foreign_keys="[CareRelation.doctor_id]",
        cascade="all, delete-orphan", passive_deletes=True
    )
    chat_sessions: Mapped[list["ChatSession"]] = relationship(
        "ChatSession", back_populates="user",
        cascade="all, delete-orphan", passive_deletes=True
    )
    assessment_results: Mapped[list["AssessmentResult"]] = relationship(
        "AssessmentResult", back_populates="user",
        cascade="all, delete-orphan", passive_deletes=True
    )
    patient_risk_events: Mapped[list["RiskEvent"]] = relationship(
        "RiskEvent", back_populates="patient", foreign_keys="[RiskEvent.user_id]",
        cascade="all, delete-orphan", passive_deletes=True
    )
    reviewed_risk_events: Mapped[list["RiskEvent"]] = relationship(
        "RiskEvent", back_populates="reviewer", foreign_keys="[RiskEvent.reviewed_by]"
    )
    risk_event_actions: Mapped[list["RiskEventAction"]] = relationship(
        "RiskEventAction", back_populates="actor"
    )
    daily_risk_scores: Mapped[list["DailyRiskScore"]] = relationship(
        "DailyRiskScore", back_populates="user",
        cascade="all, delete-orphan", passive_deletes=True
    )
    patient_care_requests: Mapped[list["CareRequest"]] = relationship(
        "CareRequest", back_populates="patient", foreign_keys="[CareRequest.patient_id]",
        cascade="all, delete-orphan", passive_deletes=True
    )
    targeted_care_requests: Mapped[list["CareRequest"]] = relationship(
        "CareRequest", back_populates="target_doctor", foreign_keys="[CareRequest.target_doctor_id]"
    )
    responded_contacts: Mapped[list["RequestContact"]] = relationship(
        "RequestContact", back_populates="responder"
    )
    sent_request_messages: Mapped[list["RequestMessage"]] = relationship(
        "RequestMessage", back_populates="sender"
    )
    patient_appointments: Mapped[list["Appointment"]] = relationship(
        "Appointment", back_populates="patient", foreign_keys="[Appointment.patient_id]",
        cascade="all, delete-orphan", passive_deletes=True
    )
    doctor_appointments: Mapped[list["Appointment"]] = relationship(
        "Appointment", back_populates="doctor", foreign_keys="[Appointment.doctor_id]",
        cascade="all, delete-orphan", passive_deletes=True
    )
    created_appointments: Mapped[list["Appointment"]] = relationship(
        "Appointment", back_populates="creator", foreign_keys="[Appointment.created_by]"
    )
    appointment_changes: Mapped[list["AppointmentChange"]] = relationship(
        "AppointmentChange", back_populates="changer"
    )
    schedules: Mapped[list["Schedule"]] = relationship(
        "Schedule", back_populates="user", foreign_keys="[Schedule.user_id]",
        cascade="all, delete-orphan", passive_deletes=True
    )
    created_schedules: Mapped[list["Schedule"]] = relationship(
        "Schedule", back_populates="creator", foreign_keys="[Schedule.created_by]"
    )
    notifications: Mapped[list["Notification"]] = relationship(
        "Notification", back_populates="user",
        cascade="all, delete-orphan", passive_deletes=True
    )
    patient_memos: Mapped[list["DoctorMemo"]] = relationship(
        "DoctorMemo", back_populates="patient", foreign_keys="[DoctorMemo.patient_id]",
        cascade="all, delete-orphan", passive_deletes=True
    )
    doctor_memos: Mapped[list["DoctorMemo"]] = relationship(
        "DoctorMemo", back_populates="doctor", foreign_keys="[DoctorMemo.doctor_id]",
        cascade="all, delete-orphan", passive_deletes=True
    )
    diaries: Mapped[list["Diary"]] = relationship(
        "Diary", back_populates="user",
        cascade="all, delete-orphan", passive_deletes=True
    )
    emotion_records: Mapped[list["EmotionRecord"]] = relationship(
        "EmotionRecord", back_populates="user",
        cascade="all, delete-orphan", passive_deletes=True
    )
    sleep_records: Mapped[list["SleepRecord"]] = relationship(
        "SleepRecord", back_populates="user",
        cascade="all, delete-orphan", passive_deletes=True
    )
    exercise_records: Mapped[list["ExerciseRecord"]] = relationship(
        "ExerciseRecord", back_populates="user",
        cascade="all, delete-orphan", passive_deletes=True
    )
    attendances: Mapped[list["Attendance"]] = relationship(
        "Attendance", back_populates="user",
        cascade="all, delete-orphan", passive_deletes=True
    )
    characters: Mapped[list["Character"]] = relationship(
        "Character", back_populates="user",
        cascade="all, delete-orphan", passive_deletes=True
    )
    user_flowers: Mapped[list["UserFlower"]] = relationship(
        "UserFlower", back_populates="user",
        cascade="all, delete-orphan", passive_deletes=True
    )
    user_expressions: Mapped[list["UserExpression"]] = relationship(
        "UserExpression", back_populates="user",
        cascade="all, delete-orphan", passive_deletes=True
    )
    user_items: Mapped[list["UserItem"]] = relationship(
        "UserItem", back_populates="user",
        cascade="all, delete-orphan", passive_deletes=True
    )
    myroom_entities: Mapped[list["MyroomEntity"]] = relationship(
        "MyroomEntity", back_populates="user",
        cascade="all, delete-orphan", passive_deletes=True
    )
    care_action_logs: Mapped[list["CareActionLog"]] = relationship(
        "CareActionLog", back_populates="user",
        cascade="all, delete-orphan", passive_deletes=True
    )
    point_histories: Mapped[list["PointHistory"]] = relationship(
        "PointHistory", back_populates="user",
        cascade="all, delete-orphan", passive_deletes=True
    )
    user_mission_logs: Mapped[list["UserMissionLog"]] = relationship(
        "UserMissionLog", back_populates="user",
        cascade="all, delete-orphan", passive_deletes=True
    )
