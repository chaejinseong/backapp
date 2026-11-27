from enum import Enum

class Instrument(str, Enum):
    PHQ9 = "phq9"
    GAD7 = "gad7"
    ISI = "isi"
    BDI2 = "bdi2"

class EmotionType(str, Enum):
    ANGER = "anger"
    JOY = "joy"
    ANXIETY = "anxiety"
    EMBARRASSMENT = "embarrassment"
    SADNESS = "sadness"
    HURT = "hurt"

class ChecklistStatus(str, Enum):
    PENDING = "pending"
    DONE = "done"
    SKIPPED = "skipped"

class RequestedBy(str, Enum):
    PATIENT = "patient"
    DOCTOR = "doctor"

class Season(str, Enum):
    SPRING = "spring"
    SUMMER = "summer"
    AUTUMN = "autumn"
    WINTER = "winter"
    COMMON = "common"
