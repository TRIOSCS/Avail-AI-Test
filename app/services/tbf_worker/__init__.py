"""The Broker Forum (TBF) automated search worker.

Browser-based search automation for electronic component sourcing on thebrokersite.com.
Searches are queued automatically when board-level component RFQs are added to AVAIL.

Re-exports the public surface used by callers outside the package.
"""

from .config import TbfConfig
from .session_manager import TbfSessionManager
from .sighting_writer import save_tbf_sightings

__all__ = ["TbfConfig", "TbfSessionManager", "save_tbf_sightings"]
