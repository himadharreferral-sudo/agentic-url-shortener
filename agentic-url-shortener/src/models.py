from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, Any
import time


@dataclass
class ShortLink:
    code: str
    long_url: str
    created_at: float
    numeric_id: Optional[int] = None
    expires_at: Optional[float] = None
    clicks: int = 0
    is_active: bool = True
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ClickEvent:
    code: str
    timestamp: float
    user_agent: str
    referer: str
    client_ip_hash: str
    redirect_status: int
    latency_ms: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
