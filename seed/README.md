# Seed snapshot (public data, personal machine, 2026-10-29)

A one-time copy of the archive built on the personal machine, so the work machine starts with
history it can no longer download. The OCC per-symbol contract-volume file is only kept for
~30 sessions, and this snapshot holds 2026-09-17 to 2026-10-28.

| Folder | What |
|---|---|
| `raw/` | every downloaded file, gzipped, as served by the source |
| `tables/` | the parsed Parquet tables (same layout as `data/tables/`) |
| `meta/ingest_log/` | the fetch log, so backfills skip dates already in the snapshot |

Coverage: OCC contract volume 2026-09-17 → 2026-10-28 (30 sessions); OCC calls/puts for 50
symbols 2026-10-15 → 2026-10-28; OCC weekly by class 2024-10-25 → 2026-10-23; OCC daily totals
2024-11-01 → 2026-10-28; Cboe daily 2026-07-01 → 2026-10-28; Nasdaq symbol directory 2026-10-29.

## Use it (work machine, once)

```powershell
.venv\Scripts\omi import-seed      # merges seed\ into data\, then rebuilds features
```

Safe to run before or after the pipeline has started: tables merge by primary key (newer
fetches win), and files already in `data\` are left alone. `data\` itself stays git-ignored,
so nothing produced at work can be committed back (spec C3).

## Rules

- **Keep the GitHub repo private.** This is OCC, Cboe and Nasdaq website data for internal
  use; publishing it would be redistribution (spec §13).
- This folder is a snapshot, not a sync. Don't keep committing new data here; git is a poor
  store for a growing archive. The work machine's `data\` is the archive from now on.
