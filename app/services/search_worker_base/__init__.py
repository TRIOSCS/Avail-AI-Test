"""Shared base code for search worker packages (ICS, NC).

Contains common modules extracted from ics_worker and nc_worker:
- mpn_normalizer: MPN normalization for deduplication
- circuit_breaker: base circuit breaker state machine
- queue_manager: parameterized search queue management
- ai_gate: parameterized AI commodity classification gate
- human_behavior: browser interaction simulation
- monitoring: daily reports, Sentry alerts, HTML hash tracking (functions, not a class)
- config: environment-based configuration factory
- scheduler: search timing and break management
"""

from .ai_gate import AIGate
from .circuit_breaker import CircuitBreakerBase
from .config import build_worker_config
from .human_behavior import HumanBehavior
from .mpn_normalizer import strip_packaging_suffixes
from .queue_manager import QueueManager
from .scheduler import SearchScheduler

__all__ = [
    "AIGate",
    "CircuitBreakerBase",
    "HumanBehavior",
    "QueueManager",
    "SearchScheduler",
    "build_worker_config",
    "strip_packaging_suffixes",
]
