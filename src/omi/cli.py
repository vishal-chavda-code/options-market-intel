"""``omi`` command line: probe, backfill, daily, features, health, chain, report, info."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Annotated

import pandas as pd
import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from omi import __version__, pipeline
from omi import calendar as cal
from omi import probe as probe_mod
from omi import report as report_mod
from omi.config import PROJECT_ROOT, ConfigError, Settings, load_settings
from omi.http import HttpClient, describe_proxy, describe_trust, user_agent
from omi.runner import SourceSummary
from omi.storage import Storage

app = typer.Typer(
    help="Options market intelligence: archive free public options-market data.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_show_locals=False,
)
console = Console()
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def _setup_logging(logs_dir: Path | None, verbose: bool) -> None:
    """Console gets warnings (or everything with -v); logs/omi_<date>.log gets INFO and up."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
               for h in root.handlers):
        stream = logging.StreamHandler()
        stream.setLevel(logging.DEBUG if verbose else logging.WARNING)
        stream.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        root.addHandler(stream)
    if logs_dir is not None:
        logs_dir.mkdir(parents=True, exist_ok=True)
        log_file = logs_dir / f"omi_{cal.now_et():%Y%m%d}.log"
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
        file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
        root.addHandler(file_handler)


@app.callback()
def main(
    ctx: typer.Context,
    config: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="YAML config file (default: config/config.yaml)."),
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")] = False,
) -> None:
    _setup_logging(None, verbose)  # console only, so config warnings are visible
    try:
        settings = load_settings(config)
    except ConfigError as exc:
        console.print(f"[red]Config error:[/] {exc}")
        raise typer.Exit(2) from exc
    _setup_logging(settings.paths.logs_dir, verbose)
    ctx.obj = settings


@app.command()
def probe(
    ctx: typer.Context,
    source: Annotated[
        list[str] | None,
        typer.Option("--source", "-s", help="Only probe this source (repeatable)."),
    ] = None,
    as_of: Annotated[
        datetime | None,
        typer.Option("--date", formats=["%Y-%m-%d"],
                     help="Report date for dated URLs (default: the session before the last "
                          "completed one, so publication lag can't cause false failures)."),
    ] = None,
    attempts: Annotated[
        int, typer.Option(min=1, help="Attempts per URL (pipeline runs use network.max_attempts).")
    ] = 2,
    include_disabled: Annotated[
        bool, typer.Option("--include-disabled", help="Also probe sources disabled in config.")
    ] = False,
    save_samples: Annotated[
        Path | None,
        typer.Option("--save-samples",
                     help="Save each response body here. Personal machine only (fixtures)."),
    ] = None,
) -> None:
    """Hit one known-good URL per source and print a pass/fail table. Run this first at work."""
    settings: Settings = ctx.obj
    report_date = as_of.date() if as_of else cal.previous_session(cal.last_completed_session())
    client = HttpClient(settings.network, max_attempts=attempts)
    with console.status("Probing sources (rate-limited, can take a minute)..."):
        rows = probe_mod.run_probe(
            settings, client, report_date,
            only=source or (), include_disabled=include_disabled, save_dir=save_samples,
        )
    if not rows:
        console.print(f"[red]No probe targets match --source {source}[/]")
        raise typer.Exit(2)
    probe_mod.render(rows, report_date, console)
    log_path = probe_mod.write_log(rows, settings.paths.logs_dir)
    console.print(f"Probe log: {log_path}")
    if save_samples:
        console.print(f"Samples saved to: {save_samples.resolve()}")
    raise typer.Exit(probe_mod.exit_code(rows))


def _print_summaries(summaries: list[SourceSummary]) -> int:
    table = Table(title="Run summary")
    for col in ("Source", "Planned", "Skipped", "OK", "Not avail.", "Failed", "Rows", "Error"):
        table.add_column(col, justify="left" if col in ("Source", "Error") else "right")
    for s in summaries:
        style = "red" if s.failed else ("green" if s.ok else "")
        table.add_row(f"[{style}]{s.source}[/]" if style else s.source, str(s.planned),
                      str(s.skipped), str(s.ok), str(s.not_available), str(s.failed),
                      f"{s.rows:,}", escape(s.error))
    console.print(table)
    return 1 if any(s.failed for s in summaries) else 0


@app.command()
def backfill(
    ctx: typer.Context,
    source: Annotated[
        list[str] | None,
        typer.Option("--source", "-s",
                     help="Source to backfill (repeatable; default: all enabled)."),
    ] = None,
    start: Annotated[
        datetime | None,
        typer.Option(formats=["%Y-%m-%d"],
                     help="First session (default: the source's backfill_start)."),
    ] = None,
    end: Annotated[
        datetime | None,
        typer.Option(formats=["%Y-%m-%d"], help="Last session (default: last completed session)."),
    ] = None,
    force: Annotated[bool, typer.Option(help="Re-download dates already stored.")] = False,
) -> None:
    """Fetch every session in a date range that is not stored yet. Safe to re-run."""
    settings: Settings = ctx.obj
    try:
        summaries = pipeline.backfill(settings, source, start.date() if start else None,
                                      end.date() if end else None, force=force)
    except pipeline.PipelineError as exc:
        console.print(f"[red]{escape(str(exc))}[/]")
        raise typer.Exit(2) from exc
    raise typer.Exit(_print_summaries(summaries))


@app.command()
def daily(
    ctx: typer.Context,
    as_of: Annotated[
        datetime | None,
        typer.Option("--date", formats=["%Y-%m-%d"], help="Session (default: last completed)."),
    ] = None,
    force: Annotated[bool, typer.Option(help="Re-download dates already stored.")] = False,
) -> None:
    """Nightly job: every enabled source for the last session (plus catch-up), then features."""
    settings: Settings = ctx.obj
    summaries = pipeline.daily(settings, as_of.date() if as_of else None, force=force)
    raise typer.Exit(_print_summaries(summaries))


@app.command()
def features(ctx: typer.Context) -> None:
    """Rebuild the feature tables from stored data (no network)."""
    settings: Settings = ctx.obj
    counts = pipeline.build_features(Storage(settings.paths.data_dir), settings)
    if not counts:
        console.print("[yellow]No contract-volume data stored yet; run `omi backfill` first.[/]")
        raise typer.Exit(1)
    for name, n in counts.items():
        console.print(f"{name}: {n:,} rows")


@app.command("import-seed")
def import_seed(
    ctx: typer.Context,
    path: Annotated[Path, typer.Option(help="Snapshot folder (default: seed/ in the repo).")]
    = PROJECT_ROOT / "seed",
) -> None:
    """Merge the committed public-data snapshot into data/, then rebuild features. Safe to
    repeat."""
    settings: Settings = ctx.obj
    if not (path / "tables").is_dir():
        console.print(f"[red]No snapshot at {escape(str(path))} (expected a tables/ folder).[/]")
        raise typer.Exit(2)
    counts = pipeline.import_seed(settings, path)
    for name, n in counts.items():
        console.print(f"{name}: {n:,}")
    built = pipeline.build_features(Storage(settings.paths.data_dir), settings)
    console.print(f"features rebuilt: {', '.join(f'{k} {v:,}' for k, v in built.items())}")


@app.command()
def health(ctx: typer.Context) -> None:
    """Per-source freshness and errors from the ingest log (also written for the dashboard)."""
    settings: Settings = ctx.obj
    df = pipeline.health(settings)
    if df.empty:
        console.print("Ingest log is empty; nothing has been fetched yet.")
        raise typer.Exit(1)
    table = Table(title="Data health")
    cols = ["source", "report", "last_ok_as_of", "sessions_behind", "last_status", "ok_fetches",
            "not_available", "errors", "last_error"]
    for col in cols:
        table.add_column(col, overflow="fold")
    for row in df[cols].itertuples(index=False):
        table.add_row(*["" if pd.isna(v) else escape(str(v))[:120] for v in row])
    console.print(table)


@app.command()
def chain(
    ctx: typer.Context,
    symbol: Annotated[str, typer.Argument(help="Underlying, e.g. AAPL, SPY, SPX (index: SPX or "
                                               "_SPX).")],
    expiry: Annotated[
        datetime | None,
        typer.Option(formats=["%Y-%m-%d"], help="Only this expiry in the printed tables."),
    ] = None,
    top: Annotated[int, typer.Option(help="Strikes to list by open interest.")] = 10,
    save: Annotated[bool, typer.Option(help="Write the full chain to exports/ as CSV.")] = True,
) -> None:
    """Fetch one symbol's full option chain now (Cboe delayed) and summarise open interest.

    On demand only: nothing is written to the stored tables (the nightly job owns those).
    """
    from omi.sources.cboe_chains import KNOWN_INDEX, fetch_one

    settings: Settings = ctx.obj
    raw = symbol.strip().upper()
    sym = raw.lstrip("_")
    url_sym = raw if raw.startswith("_") else (f"_{sym}" if sym in KNOWN_INDEX else sym)
    overrides = (settings.source("cboe_chains").model_extra or {}).get("symbol_overrides") or {}
    url_sym = overrides.get(sym, url_sym)
    client = HttpClient(settings.network)
    try:
        with console.status(f"Fetching {url_sym} chain from Cboe..."):
            tables = fetch_one(client, sym, url_sym)
    except Exception as exc:  # noqa: BLE001 - show the reason, exit non-zero
        console.print(f"[red]Could not fetch {escape(url_sym)}: {escape(str(exc))}[/]")
        raise typer.Exit(1) from exc
    df, under = tables["chain_eod"], tables["chain_underlying"].iloc[0]
    if expiry is not None:
        df = df[df["expiry"] == expiry.date()]
    calls, puts = df[df.cp == "C"], df[df.cp == "P"]
    console.print(f"[bold]{escape(str(under.cboe_symbol))}[/] {under.price:,.2f}  "
                  f"(delayed; Cboe time {under.cboe_timestamp} UTC)")
    console.print(f"Contracts {len(df):,}  expiries {df.expiry.nunique()}  "
                  f"call OI {calls.open_interest.sum():,}  put OI {puts.open_interest.sum():,}  "
                  f"P/C OI {puts.open_interest.sum() / max(calls.open_interest.sum(), 1):.2f}  "
                  f"volume {df.volume.sum():,}")
    by_exp = (df.groupby(["expiry", "cp"])["open_interest"].sum().unstack(fill_value=0)
              .reindex(columns=["C", "P"], fill_value=0))
    t = Table(title="Open interest by expiry (top 12)")
    for col in ("Expiry", "Call OI", "Put OI"):
        t.add_column(col, justify="right")
    for exp, row in by_exp.sort_values(by=["C", "P"], ascending=False).head(12).iterrows():
        t.add_row(str(exp), f"{row.C:,}", f"{row.P:,}")
    console.print(t)
    t = Table(title=f"Top {top} contracts by open interest")
    for col in ("Contract", "Expiry", "Strike", "C/P", "OI", "Volume", "Bid", "Ask", "IV"):
        t.add_column(col, justify="right")
    for r in df.nlargest(top, "open_interest").itertuples(index=False):
        t.add_row(r.option, str(r.expiry), f"{r.strike:g}", r.cp, f"{r.open_interest:,}",
                  f"{r.volume:,}", f"{r.bid:.2f}", f"{r.ask:.2f}",
                  "" if pd.isna(r.iv) else f"{r.iv:.1%}")
    console.print(t)
    if save:
        out = settings.paths.exports_dir / f"chain_{sym}_{cal.now_et():%Y%m%d_%H%M}.csv"
        out.parent.mkdir(parents=True, exist_ok=True)
        tables["chain_eod"].to_csv(out, index=False)
        console.print(f"Full chain: {out}")


@app.command()
def report(
    ctx: typer.Context,
    as_of: Annotated[
        datetime | None,
        typer.Option("--date", formats=["%Y-%m-%d"],
                     help="Snapshot date for the open-interest slides (default: latest)."),
    ] = None,
    out: Annotated[Path | None, typer.Option(help="Output .pptx path (default: exports/).")]
    = None,
) -> None:
    """Build the daily positioning slides (PowerPoint) from the stored features."""
    settings: Settings = ctx.obj
    try:
        path = report_mod.build_report(Storage(settings.paths.data_dir), settings,
                                       as_of=as_of.date() if as_of else None, out=out)
    except report_mod.ReportError as exc:
        console.print(f"[yellow]{escape(str(exc))}[/]")
        raise typer.Exit(1) from exc
    console.print(f"Report: {path}")


@app.command()
def info(ctx: typer.Context) -> None:
    """Show the resolved config, network settings and trading-calendar state."""
    settings: Settings = ctx.obj
    net = settings.network
    now = cal.now_et()
    enabled = [name for name, s in settings.sources.items() if s.enabled]
    disabled = [name for name, s in settings.sources.items() if not s.enabled]
    lines = {
        "omi version": __version__,
        "config file": settings.config_file,
        "data dir": settings.paths.data_dir,
        "logs dir": settings.paths.logs_dir,
        "exports dir": settings.paths.exports_dir,
        "proxy": describe_proxy(net),
        "TLS trust": describe_trust(net),
        "user agent": user_agent(net),
        "sources enabled": ", ".join(enabled) or "(none)",
        "sources disabled": ", ".join(disabled) or "(none)",
        "now (ET)": f"{now:%Y-%m-%d %H:%M %Z}",
        "last completed session": cal.last_completed_session(now).isoformat(),
        "market open now": "yes" if cal.is_market_open(now) else "no",
    }
    width = max(map(len, lines))
    for key, value in lines.items():
        typer.echo(f"{key:<{width}}  {value}")


if __name__ == "__main__":
    app()
