from enum import Enum

class ItemCategory(str, Enum):
    FURNITURE = "furniture"
    BACKGROUND = "background"
    SNACK = "snack"
    CARE = "care"

class ItemOwnershipType(str, Enum):
    UNIQUE = "unique"
    STACKABLE = "stackable"
