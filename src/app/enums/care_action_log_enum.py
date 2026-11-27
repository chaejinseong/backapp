from enum import Enum

# 캐릭터 상호작용 종류 - 경험치를 얻는 유일한 활동
class CareActionLogActionType(str, Enum):
    SNACK = "snack"  # 먹이기 (간식 아이템 사용, 줄인 배고픔 × 아이템 배율만큼 경험치)
    WASH = "wash"    # 씻기기 (돌봄 아이템 사용, 줄인 꼬질함 × 아이템 배율만큼 경험치)
    PLAY = "play"    # 놀아주기 (하루 3번, 고정 경험치)
