#!/usr/bin/env python3

# Copyright (c) 2025 Rubypoint
#
# Licensed under the MIT License. See the LICENSE file in the project root for full license information.

"""
Utility for extracting XML payloads from MTConnect client logs.

The script scans the specified log file and writes only the lines that contain
XML elements (`<tag>` … `</tag>`) to stdout or to an optional output file.
"""
from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import DefaultDict, Dict, Iterable, Iterator, List, TextIO, Tuple, Union

DEFAULT_LOG_PATH = Path(__file__).resolve().parents[1] / "logs" / "mtc_client.log"
XML_PATTERN = re.compile(r"<[^>]+>")
MTSTREAMS_END = "</MTConnectStreams>"
UNSPECIFIED_COMMENT = "UNSPECIFIED"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Filter MTConnect logs to only XML lines.")
    parser.add_argument(
        "log_path",
        nargs="?",
        default=str(DEFAULT_LOG_PATH),
        help=f"Path to the MTConnect client log. Defaults to {DEFAULT_LOG_PATH}",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Optional output file. If omitted, XML lines are printed to stdout.",
    )
    return parser.parse_args()


def xml_lines(lines: Iterable[str]) -> Iterator[str]:
    """Yield only lines that contain XML elements."""
    for line in lines:
        if XML_PATTERN.search(line):
            yield line.rstrip("\n")


def write_lines(lines: Iterable[str], handle: TextIO) -> None:
    for line in lines:
        handle.write(f"{line}\n")


def _iter_mtconnect_documents(lines: Iterable[str]) -> Iterator[str]:
    """Yield full MTConnectStreams XML documents from a log stream."""
    buffer: List[str] = []
    capturing = False
    for raw in lines:
        if not capturing and ("<?xml" in raw or "<MTConnectStreams" in raw):
            capturing = True
            buffer = [raw]
            if MTSTREAMS_END in raw:
                yield "".join(buffer)
                buffer = []
                capturing = False
            continue
        if capturing:
            buffer.append(raw)
            if MTSTREAMS_END in raw:
                yield "".join(buffer)
                buffer = []
                capturing = False
    if capturing and buffer:
        yield "".join(buffer)


def _ns(tag: str) -> str:
    if tag.startswith("{"):
        return tag[1 : tag.find("}")]
    return ""


def _parse_timestamp(ts: str) -> datetime:
    """Parse MTConnect timestamps that may have fractional seconds of varying precision."""
    cleaned = ts.strip()
    if not cleaned:
        return datetime.now(timezone.utc)
    tz = ""
    if cleaned.endswith("Z"):
        tz = "+00:00"
        cleaned = cleaned[:-1]
    elif len(cleaned) >= 6 and cleaned[-3] == ":" and cleaned[-6] in "+-":
        tz = cleaned[-6:]
        cleaned = cleaned[:-6]
    if "." in cleaned:
        base, frac = cleaned.split(".", 1)
        frac = frac[:6].ljust(6, "0")
        cleaned = f"{base}.{frac}"
    if tz:
        cleaned = f"{cleaned}{tz}"
    try:
        return datetime.fromisoformat(cleaned)
    except ValueError:
        # Fall back to second precision if fractional parsing failed
        if "." in cleaned:
            base = cleaned.split(".", 1)[0]
            cleaned = f"{base}{tz}"
            return datetime.fromisoformat(cleaned)
        raise


def group_series_by_comment(log_path: Union[str, Path], element:str) -> Dict[str, List[Tuple[str, float]]]:
    """
    Return a mapping of ProgramComment -> [(timestamp, path_feedrate), ...].

    The function walks the MTConnect stream chronologically, updating the active
    ProgramComment whenever a new ProgramComment event is seen, and assigns each
    PathFeedrate sample to the most recent comment.
    """
    normalized_path =  Path(log_path).expanduser()
    if not normalized_path.is_file():
        raise FileNotFoundError(f"Log file not found: {normalized_path}")

    grouped: DefaultDict[str, List[Tuple[str, float]]] = defaultdict(list)
    current_comment = UNSPECIFIED_COMMENT
    current_seq = -1

    with normalized_path.open("r", encoding="utf-8", errors="replace") as src:
        for doc in _iter_mtconnect_documents(src):
            if not doc.strip():
                continue
            try:
                root = ET.fromstring(doc)
            except ET.ParseError:
                continue
            ns = _ns(root.tag)
            program_comment_tag = f".//{{{ns}}}ProgramComment" if ns else ".//ProgramComment"
            feedrate_tag = f".//{{{ns}}}{element}" if ns else f".//{element}"

            for event in root.findall(program_comment_tag):
                seq = int(event.attrib.get("sequence", current_seq))
                text = (event.text or "").strip() or UNSPECIFIED_COMMENT
                if seq >= current_seq:
                    current_comment = text
                    current_seq = seq

            for sample in root.findall(feedrate_tag):
                timestamp = sample.attrib.get("timestamp", "")
                value_text = (sample.text or "").strip()
                try:
                    value = float(value_text)
                except ValueError:
                    continue
                grouped[current_comment].append((timestamp, value))

    for series in grouped.values():
        series.sort(key=lambda tv: tv[0])
    return dict(grouped)


def plot_series_by_comment(series: Dict[str, List[Tuple[str, float]]], label:str) -> None:
    """
    Plot feedrate time series for each ProgramComment grouping.

    Requires matplotlib; the import is deferred so the module remains usable
    without plotting dependencies.
    """
    import matplotlib.pyplot as plt

    if not series:
        raise ValueError("No feedrate data to plot.")

    fig, ax = plt.subplots()
    for comment, data in series.items():
        if not data:
            continue
        timestamps = [_parse_timestamp(ts) for ts, _ in data]
        values = [value for _, value in data]
        ax.plot(timestamps, values, label=comment)

    ax.set_xlabel("Timestamp")
    ax.set_ylabel(label)
    ax.set_title(f"{label} by Program Comment")
    ax.legend()
    fig.autofmt_xdate()
    plt.tight_layout()
    plt.show()

def filter_logs()-> None:
    args = parse_args()
    log_path = Path(args.log_path).expanduser()
    if not log_path.is_file():
        raise SystemExit(f"Log file not found: {log_path}")

    with log_path.open("r", encoding="utf-8", errors="replace") as src:
        filtered = list(xml_lines(src))

    if args.output:
        out_path = Path(args.output).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as dst:
            write_lines(filtered, dst)
    else:
        write_lines(filtered, sys.stdout)

# def main() -> None:
#     FEEDRATE_TAG = "PathFeedrate"
#     SPINDLE_SPEED_TAG = "SpindleSpeed"
#     series = group_series_by_comment("logs/xml_only.log", FEEDRATE_TAG)
#     plot_series_by_comment(series, FEEDRATE_TAG)
#     print(series)


if __name__ == "__main__":
    filter_logs()
