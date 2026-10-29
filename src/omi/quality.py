"""Schema enforcement for parsed frames before they reach a table.

``conform`` is strict: a missing or extra column, an uncastable value or a duplicate primary
key raises ``SchemaDrift``. That status keeps the raw file but writes nothing to the table.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from omi.sources.base import SchemaDrift, TableSpec


def _cast(series: pd.Series, dtype: str) -> pd.Series:
    if dtype == "date":
        return pd.to_datetime(series).dt.date
    if dtype == "timestamp":
        return pd.to_datetime(series, utc=True)
    if dtype == "string":
        return series.astype("string")
    return series.astype(dtype)


def conform(spec: TableSpec, df: pd.DataFrame, fetched_at: datetime | None = None) -> pd.DataFrame:
    """Return ``df`` with exactly the spec's columns (plus ``fetched_at``), cast to its dtypes."""
    expected = set(spec.columns)
    have = set(df.columns) - {"fetched_at"}
    if have != expected:
        missing, extra = sorted(expected - have), sorted(have - expected)
        raise SchemaDrift(f"{spec.name}: missing columns {missing}, unexpected {extra}")

    out = pd.DataFrame(index=df.index)
    for col, dtype in spec.columns.items():
        try:
            out[col] = _cast(df[col], dtype)
        except (ValueError, TypeError) as exc:
            raise SchemaDrift(f"{spec.name}.{col}: cannot cast to {dtype}: {exc}") from exc
    if "fetched_at" in df.columns:
        out["fetched_at"] = pd.to_datetime(df["fetched_at"], utc=True)
    else:
        stamp = pd.Timestamp(fetched_at or datetime.now().astimezone())
        out["fetched_at"] = stamp.tz_convert("UTC")

    pk = list(spec.pk)
    if out[pk].isna().any().any():
        raise SchemaDrift(f"{spec.name}: null values in primary key {pk}")
    dupes = out.duplicated(pk, keep=False)
    if dupes.any():
        sample = out.loc[dupes, pk].head(3).to_dict("records")
        raise SchemaDrift(
            f"{spec.name}: {int(dupes.sum())} rows share a primary key, e.g. {sample}"
        )
    return out.reset_index(drop=True)
