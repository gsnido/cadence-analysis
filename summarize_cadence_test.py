#!/usr/bin/env python3
"""Summarize one-minute cadence segments using FIT active timer time, ignoring laps.

Requires: pandas, fitparse.
Reads target cadence from Minute and BPM columns in a schedule CSV.
Start and End columns are ignored; each row represents one successive minute.
Uses sample means from [20, 55) seconds within each segment.
Output cadence_bpm is the target cadence, not measured cadence.
"""
import argparse
import csv
import math
import warnings
from pathlib import Path
import pandas as pd
from fitparse import FitFile

STOP_TYPES = {"stop", "stop_all", "stop_disable", "stop_disable_all"}


def load_cadence_schedule(csv_path):
    """Load target BPM in Minute order; Start and End are not used."""
    csv_path = Path(csv_path)
    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        required = {"Minute", "BPM"}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError(f"{csv_path}: required columns are Minute and BPM.")
        schedule = []
        for row in reader:
            if None in row:
                raise ValueError(f"{csv_path}, line {reader.line_num}: too many CSV fields.")
            if not any((value or "").strip() for value in row.values()):
                continue
            try:
                minute = int(row["Minute"])
                bpm = float(row["BPM"])
            except (ValueError, TypeError) as exc:
                raise ValueError(
                    f"{csv_path}, line {reader.line_num}: "
                    "Minute must be an integer and BPM must be numeric."
                ) from exc
            if not math.isfinite(bpm) or bpm <= 0:
                raise ValueError(
                    f"{csv_path}, line {reader.line_num}: BPM must be positive and finite."
                )
            schedule.append((minute, bpm))
    if not schedule:
        raise ValueError(f"{csv_path}: no cadence entries found.")
    schedule.sort(key=lambda item: item[0])
    if [minute for minute, _ in schedule] != list(range(1, len(schedule) + 1)):
        raise ValueError(
            f"{csv_path}: Minute must run from 1 to {len(schedule)} "
            "without gaps or duplicates."
        )
    return [bpm for _, bpm in schedule]


def add_timer_time(df, events):
    events = sorted(events, key=lambda item: item[0])
    if not any(kind == "start" for _, kind in events):
        raise ValueError("No timer-start events found; cannot reconstruct timer time safely.")
    intervals = []
    active_start = None
    for timestamp, kind in events:
        if kind == "start":
            if active_start is None:
                active_start = timestamp
        elif kind in STOP_TYPES and active_start is not None:
            intervals.append((active_start, timestamp))
            active_start = None
    if active_start is not None:
        intervals.append((active_start, None))
    timer = pd.Series(float("nan"), index=df.index)
    cumulative = 0.0
    for start, stop in intervals:
        mask = df["timestamp"] >= start
        if stop is not None:
            mask &= df["timestamp"] < stop
        timer.loc[mask] = cumulative + (df.loc[mask, "timestamp"] - start).dt.total_seconds()
        if stop is not None:
            cumulative += (stop - start).total_seconds()
    result = df.copy()
    result["timer_s"] = timer
    return result.loc[result["timer_s"].notna()].reset_index(drop=True)


def read_fit(path):
    rows, events = [], []
    for message in FitFile(str(path)).get_messages():
        if message.name not in {"record", "event"}:
            continue
        values = message.get_values()
        timestamp = values.get("timestamp")
        if timestamp is None:
            continue
        if message.name == "event":
            if values.get("event") == "timer":
                events.append((timestamp, values.get("event_type")))
            continue
        speed = values.get("enhanced_speed")
        if speed is None:
            speed = values.get("speed")
        rows.append({"timestamp": timestamp, "speed_m_s": speed,
                     "heart_rate_bpm": values.get("heart_rate"),
                     "power_w": values.get("power")})
    if not rows:
        raise ValueError("No timestamped FIT records found.")
    df = pd.DataFrame(rows).sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    for column in ["speed_m_s", "heart_rate_bpm", "power_w"]:
        df[column] = pd.to_numeric(df[column], errors="coerce")
        if df[column].isna().all():
            warnings.warn(f"No values for {column}; corresponding output will be blank.")
    df = add_timer_time(df, events)
    if df.empty:
        raise ValueError("No records overlap active timer intervals.")
    return df


def summarize(df, tempo_start, cadences):
    rows = []
    for index, cadence in enumerate(cadences):
        start = tempo_start + index * 60 + 20
        end = start + 35
        window = df.loc[(df["timer_s"] >= start) & (df["timer_s"] < end)]
        if window.empty:
            raise ValueError(f"Segment {index+1}: no data in timer window {start:.1f}–{end:.1f} s.")
        gaps = window["timestamp"].diff().dt.total_seconds().dropna()
        if len(window) != 35 or (gaps > 1.5).any():
            warnings.warn(f"Segment {index+1}: {len(window)} records; irregular sampling or clock gaps. Means are sample-weighted.")
        for column in ["speed_m_s", "heart_rate_bpm", "power_w"]:
            if window[column].isna().any():
                warnings.warn(f"Segment {index+1}: missing {column} samples; available values only are averaged.")
        speed = window["speed_m_s"].mean()
        pace = 1000 / speed / 60 if pd.notna(speed) and speed > 0 else float("nan")
        rows.append({"segment": index+1, "cadence_bpm": cadence,
                     "pace_min_per_km": pace,
                     "heart_rate_bpm": window["heart_rate_bpm"].mean(),
                     "power_w": window["power_w"].mean()})
    return pd.DataFrame(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fit_file", type=Path)
    parser.add_argument("-o", "--output", type=Path, default=Path("cadence_summary.csv"))
    parser.add_argument("--tempo-start", type=float, default=600,
                        help="Active timer seconds to first WAV click, excluding pauses (default: 600).")
    parser.add_argument(
        "--cadence-schedule", type=Path, default=Path("cadence_schedule.csv"),
        help="Cadence schedule CSV, relative to the working directory (default: cadence_schedule.csv)."
    )
    args = parser.parse_args()
    if not math.isfinite(args.tempo_start) or args.tempo_start < 0:
        parser.error("--tempo-start must be finite and nonnegative")
    try:
        cadences = load_cadence_schedule(args.cadence_schedule)
    except (OSError, ValueError, csv.Error, UnicodeError) as exc:
        parser.error(str(exc))
    df = read_fit(args.fit_file)
    print(f"Active records: {len(df)}; timer range: {df.timer_s.min():.1f}–{df.timer_s.max():.1f} s")
    result = summarize(df, args.tempo_start, cadences)
    result.to_csv(args.output, index=False, float_format="%.4f")
    print(result.to_string(index=False))
    print(f"Saved {len(result)} segments to {args.output}")


if __name__ == "__main__":
    main()
