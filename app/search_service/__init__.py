"""Search-service package — W4.5a split of the former 3,604-line app/search_service.py.

Pure structural move: behavior identical. Nine submodules by concern:
cache (market stats + 15-min Redis result cache), mpn_expansion (PN fan-out set +
FRU aliases + 48h cooldown), dedupe (display/aggressive/incremental), fanout
(connector config/build + _fetch_fresh + budget helpers), persistence (threaded
write orchestrators + _save_sightings), material_cards (history + resolve/upsert +
deterministic passes), presentation (dict shaping + SSE card HTML), pipeline
(search_requirement + quick_search_mpn), streaming (stream_search_mpn SSE).

Every former top-level name is re-exported here so existing imports
(`from app.search_service import ...`) keep working; test PATCH targets, however,
must point at the defining submodule (patching a package attribute cannot
intercept a submodule-local lookup — same rule as app/routers/sightings/).
`broker` is deliberately NOT a package attribute: stream_search_mpn late-binds it
via getattr(app.search_service, "broker", real_broker) so tests can inject one
with patch(..., create=True).
"""

from ..connectors.ai_live_web import AIWebSearchConnector  # noqa: F401
from ..connectors.digikey import DigiKeyConnector  # noqa: F401
from ..connectors.ebay import EbayConnector  # noqa: F401
from ..connectors.element14 import Element14Connector  # noqa: F401
from ..connectors.mouser import MouserConnector  # noqa: F401
from ..connectors.oemsecrets import OEMSecretsConnector  # noqa: F401
from ..connectors.sourcengine import SourcengineConnector  # noqa: F401
from ..connectors.sources import (  # noqa: F401
    BrokerBinConnector,
    NexarConnector,
    _redact_secrets,
)
from ..database import SessionLocal, engine  # noqa: F401
from ..services.credential_service import get_credential, get_credentials_batch  # noqa: F401
from ..services.ics_worker.queue_manager import enqueue_for_ics_search  # noqa: F401
from ..services.nc_worker.queue_manager import enqueue_for_nc_search  # noqa: F401
from ..services.sourcing_leads import (  # noqa: F401
    get_vendor_feedback_adjustment,
    sync_leads_for_sightings,
)
from ..services.tbf_worker.queue_manager import enqueue_for_tbf_search  # noqa: F401
from ..services.vendor_affinity_service import find_vendor_affinity  # noqa: F401
from ..utils.normalization import (  # noqa: F401
    normalize_mpn,
    normalize_mpn_key,
    normalize_price,
    normalize_quantity,
)
from ..vendor_utils import normalize_vendor_name  # noqa: F401
from .cache import (  # noqa: F401
    _SEARCH_CACHE_PREFIX,
    _SEARCH_CACHE_TTL,
    _cache_age_hours,
    _connect_search_redis,
    _get_search_cache,
    _get_search_redis,
    _median,
    _search_cache_key,
    _search_redis_probe,
    _set_search_cache,
    compute_market_baseline,
)
from .dedupe import (  # noqa: F401
    _deduplicate_sightings,
    _deduplicate_sightings_aggressive,
    _incremental_dedup,
)
from .fanout import (  # noqa: F401
    _CONNECTOR_SOURCE_MAP,
    _MARKET_SOURCE_DISPLAY,
    _aggregate_source_stats,
    _await_next_within_budget,
    _build_connectors,
    _fetch_fresh,
    _flatten_dedupe_filter_junk,
    _load_connector_config,
    _make_stat,
    _reset_connector_config_cache,
    get_market_source_health,
    should_trigger_ai_search,
)
from .material_cards import (  # noqa: F401
    _audit_card_created,
    _get_material_history,
    _history_to_result,
    _schedule_background_enrichment,
    _upsert_material_card,
    resolve_material_card,
    run_deterministic_passes,
)
from .mpn_expansion import (  # noqa: F401
    MAX_FRU_ALIASES,
    MPN_COOLDOWN_HOURS,
    _any_pn_obsolete,
    _expand_fru_aliases,
    _mpn_cooldown_partition,
    _persist_fru_aliases,
    get_all_pns,
)
from .persistence import (  # noqa: F401
    _persist_interactive_sightings,
    _persist_search_write,
    _propagate_vendor_emails,
    _save_sightings,
)
from .pipeline import (  # noqa: F401
    _find_affinity_in_thread,
    quick_search_mpn,
    search_requirement,
)
from .presentation import (  # noqa: F401
    _affinity_match_to_result,
    _render_search_vendor_cards_html,
    _score_raw_hit,
    sighting_to_dict,
)
from .streaming import stream_search_mpn  # noqa: F401
