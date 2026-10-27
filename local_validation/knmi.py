"""Hourly KNMI forcing (precipitation + Makkink evaporation) for one station.

Source: the public KNMI script endpoints on daggegevens.knmi.nl — no API key and no
quota, about a second per station-year — so every run simply refetches the whole
period instead of keeping incremental state.

- Precipitation: hourly `RH` (0.1 mm; -1 means < 0.05 mm, stored as 0).
- Evaporation: KNMI publishes Makkink only per day (`EV24`, 0.1 mm). It is spread over
  that day's hours in proportion to the hourly global radiation `Q`, so the daily
  totals equal KNMI's official EV24 (a day without radiation is spread evenly).

Timestamps are naive UTC, labelled at the end of the hour (KNMI hour HH covers
HH-1..HH UT), so the 01:00 row holds 00:00-01:00.
"""

from __future__ import annotations

import pandas as pd
import requests

HOURLY_URL = "https://www.daggegevens.knmi.nl/klimatologie/uurgegevens"
DAILY_URL = "https://www.daggegevens.knmi.nl/klimatologie/daggegevens"
COLUMNS = ["precipitation_mm", "makkink_mm"]


def _post(url: str, start: str, end: str, variables: str, station: int) -> pd.DataFrame:
    r = requests.post(url, data={"start": start, "end": end, "vars": variables,
                                 "stns": station, "fmt": "json"}, timeout=120)
    r.raise_for_status()
    return pd.DataFrame(r.json())


def fetch_hourly(station: int, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """Hourly `RH` and `Q`, one request per calendar year, indexed on end-of-hour UTC."""
    parts = []
    for year in range(start.year, end.year + 1):
        df = _post(HOURLY_URL, f"{year}010101", f"{year}123124", "RH:Q", station)
        if df.empty:
            continue
        date = pd.to_datetime(df["date"], utc=True).dt.tz_localize(None)
        df.index = date + pd.to_timedelta(df["hour"], unit="h")
        parts.append(df[["RH", "Q"]])
    hourly = pd.concat(parts)
    hourly = hourly[~hourly.index.duplicated(keep="last")].sort_index()
    return hourly.loc[start:end]


def fetch_daily_ev24(station: int, start: pd.Timestamp, end: pd.Timestamp) -> pd.Series:
    """Daily Makkink `EV24` in mm, indexed on the (UT) day."""
    df = _post(DAILY_URL, start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), "EV24", station)
    day = pd.to_datetime(df["date"], utc=True).dt.tz_localize(None)
    return pd.Series(df["EV24"].to_numpy(), index=day, name="ev24").astype(float) / 10


def forcing(station: int, start: str = "2020-01-01", end: str | None = None) -> pd.DataFrame:
    """`precipitation_mm` + `makkink_mm` per hour from `start` up to `end` (default: now)."""
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end) if end else pd.Timestamp.now("UTC").tz_localize(None).floor("h")
    hourly = fetch_hourly(station, start_ts, end_ts)
    ev24 = fetch_daily_ev24(station, start_ts, end_ts)

    out = pd.DataFrame(index=hourly.index)
    out.index.name = "Time"
    out["precipitation_mm"] = hourly["RH"].clip(lower=0) / 10

    # The hour ending at 00:00 belongs to the previous UT day.
    day = (hourly.index - pd.Timedelta(hours=1)).floor("D")
    q = hourly["Q"].clip(lower=0).fillna(0)
    q_day = q.groupby(day).transform("sum")
    n_day = q.groupby(day).transform("size")
    weight = (q / q_day).where(q_day > 0, 1 / n_day)
    out["makkink_mm"] = weight * ev24.reindex(day).to_numpy()
    return out


def yearly_totals(df: pd.DataFrame) -> pd.DataFrame:
    """Sum per calendar year (mm). The current year is partial."""
    return df[COLUMNS].groupby(hour_start(df).year).sum().rename_axis("year")


def hour_start(df: pd.DataFrame) -> pd.DatetimeIndex:
    """Start of each hour, so the row labelled Jan 1 00:00 counts toward the old year."""
    return df.index - pd.Timedelta(hours=1)
