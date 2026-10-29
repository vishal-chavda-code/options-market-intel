"""Registry of ingestable sources, in run order (later sources may read earlier tables)."""

from __future__ import annotations

from omi.sources.base import Source
from omi.sources.cboe_chains import CboeChains
from omi.sources.cboe_daily_stats import CboeDailyStats
from omi.sources.occ_contract_volume import OccContractVolume
from omi.sources.occ_daily_volume import OccDailyVolume
from omi.sources.occ_symbol_volume import OccSymbolVolume
from omi.sources.occ_weekly_class import OccWeeklyClass
from omi.sources.symbol_directory import SymbolDirectory

REGISTRY: dict[str, Source] = {
    s.name: s
    for s in (
        SymbolDirectory(),
        OccContractVolume(),
        OccSymbolVolume(),
        OccWeeklyClass(),
        OccDailyVolume(),
        CboeDailyStats(),
        CboeChains(),  # last: reads occ_contract_volume and ref_listed_symbols for its list
    )
}
