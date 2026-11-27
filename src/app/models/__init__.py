# 서버 시작 시 Base.metadata가 모든 테이블을 알도록 52개 모델을 모두 import 한다.

# 배치 1 — 회원·병원·권한
from app.models.user_model import User
from app.models.admin_model import Admin
from app.models.user_setting_model import UserSetting
from app.models.hospital_model import Hospital
from app.models.room_model import Room
from app.models.doctor_profile_model import DoctorProfile
from app.models.doctor_verification_model import DoctorVerification
from app.models.patient_chart_model import PatientChart
from app.models.care_relation_model import CareRelation

# 배치 2 — AI 상담
from app.models.chat_session_model import ChatSession
from app.models.message_model import Message
from app.models.session_summary_model import SessionSummary
from app.models.rag_document_model import RagDocument
from app.models.rag_chunk_model import RagChunk
from app.models.session_document_model import SessionDocument
from app.models.checklist_item_model import ChecklistItem
from app.models.session_checklist_model import SessionChecklist
from app.models.checklist_event_model import ChecklistEvent
from app.models.llm_call_model import LlmCall
from app.models.guard_check_model import GuardCheck
from app.models.emotion_analysis_model import EmotionAnalysis
from app.models.assessment_result_model import AssessmentResult

# 배치 3 — 위험 감지
from app.models.risk_event_model import RiskEvent
from app.models.risk_event_action_model import RiskEventAction
from app.models.daily_risk_score_model import DailyRiskScore

# 배치 4 — 진료 요청·예약·일정·알림
from app.models.care_request_model import CareRequest
from app.models.request_contact_model import RequestContact
from app.models.request_message_model import RequestMessage
from app.models.appointment_model import Appointment
from app.models.appointment_change_model import AppointmentChange
from app.models.schedule_model import Schedule
from app.models.notification_model import Notification
from app.models.doctor_memo_model import DoctorMemo

# 배치 5 — 환자 기록
from app.models.diary_model import Diary
from app.models.diary_analysis_model import DiaryAnalysis
from app.models.emotion_record_model import EmotionRecord
from app.models.sleep_record_model import SleepRecord
from app.models.exercise_record_model import ExerciseRecord
from app.models.attendance_model import Attendance

# 배치 6 — 캐릭터·꽃·아이템·미션·포인트
from app.models.flower_model import Flower
from app.models.expression_model import Expression
from app.models.item_model import Item
from app.models.character_model import Character
from app.models.character_position_model import CharacterPosition
from app.models.myroom_entity_model import MyroomEntity
from app.models.user_flower_model import UserFlower
from app.models.user_expression_model import UserExpression
from app.models.user_item_model import UserItem
from app.models.care_action_log_model import CareActionLog
from app.models.mission_model import Mission
from app.models.point_history_model import PointHistory
from app.models.user_mission_log_model import UserMissionLog

__all__ = [
    "User", "Admin", "UserSetting", "Hospital", "Room",
    "DoctorProfile", "DoctorVerification", "PatientChart", "CareRelation",
    "ChatSession", "Message", "SessionSummary", "RagDocument", "RagChunk",
    "SessionDocument", "ChecklistItem", "SessionChecklist", "ChecklistEvent",
    "LlmCall", "GuardCheck", "EmotionAnalysis", "AssessmentResult",
    "RiskEvent", "RiskEventAction", "DailyRiskScore",
    "CareRequest", "RequestContact", "RequestMessage", "Appointment",
    "AppointmentChange", "Schedule", "Notification", "DoctorMemo",
    "Diary", "DiaryAnalysis", "EmotionRecord", "SleepRecord", "ExerciseRecord", "Attendance",
    "Flower", "Expression", "Item", "Character", "CharacterPosition", "MyroomEntity", "UserFlower", "UserExpression",
    "UserItem", "CareActionLog", "Mission", "PointHistory", "UserMissionLog",
]
