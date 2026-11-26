from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any, Optional
import time


@dataclass
class TaskNode:
    id: str
    stage: str  # requirements, architecture, implementation, testing, documentation, release
    title: str
    dependencies: List[str] = field(default_factory=list)
    parallel_group: Optional[str] = None
    requires_human_approval: bool = False
    policy_tags: List[str] = field(default_factory=list)
    max_retries: int = 2
    entry_gate: str = "dependencies_satisfied"
    exit_gate: str = "artifact_validated"


@dataclass
class ExecutionRecord:
    task_id: str
    attempt: int
    status: str  # SUCCEEDED, FALLBACK_SUCCEEDED, FAILED, FALLBACK_FAILED, ROLLED_BACK, INVALIDATED, SAFE_STOPPED
    started_at: float
    ended_at: float
    decision_lineage: List[str] = field(default_factory=list)
    artifacts: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


@dataclass
class AuditEvent:
    timestamp: float
    event_type: str
    task_id: Optional[str]
    details: Dict[str, Any]
