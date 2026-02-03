from pydantic import BaseModel, Field
from typing import Optional, List


class EffectiveMovement(BaseModel):
    codeid_id: int  
    start_time: str  # ISO 8601 timestamp
    end_time: str  # ISO 8601 timestamp
    duration: float
    leg: str  # "Left" or "Right"


class ActivitySegment(BaseModel):
    codeid_id: int
    foot: str  # "Left" or "Right"
    device_name: Optional[str] = None
    mac: Optional[str] = None
    start_time: str  # ISO 8601 timestamp
    end_time: str  # ISO 8601 timestamp
