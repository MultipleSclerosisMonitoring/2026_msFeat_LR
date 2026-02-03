from pydantic import BaseModel, Field
from typing import Optional, List

class CodeID(BaseModel):
    codeid: str  # Unique CodeID string
    id: Optional[int] = Field(default=None)  # Database ID if already stored

class ActivityLeg(BaseModel):
    codeid_id: int        # Foreign key to CodeID table
    foot: str             # Leg identifier: "Left" or "Right"
    start_time: str       # ISO 8601 start timestamp
    end_time: str         # ISO 8601 end timestamp
    duration: float       # Duration of the segment in seconds
    total_value: float    # Aggregated sample count for this segment
    mac: Optional[str] = None           # Sensor device MAC address
    device_name: Optional[str] = None   # Sensor device name

class ActivityAll(BaseModel):
    codeid_ids: List[int] = []       # Two CodeID IDs ([Left, Right])
    codeleg_ids: List[int] = []      # Two activity_leg IDs ([Left, Right])
    start_time: str                  # ISO 8601 start timestamp of the synchronized period
    end_time: str                    # ISO 8601 end timestamp of the synchronized period
    duration: float                  # Duration of the synchronized period in seconds
    macs: List[str] = []             # MAC addresses ([Left, Right])
    active_legs: List[str] = []      # Active legs in this period, e.g. ["Left", "Right"]
    device_names: List[str] = []     # Sensor device names ([Left, Right])
    is_effective: Optional[bool] = Field(default=False)  # Indicates if this is an effective gait period
