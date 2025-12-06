#!/usr/bin/env python3

# Copyright (c) 2025 Rubypoint
#
# Licensed under the MIT License. See the LICENSE file in the project root for full license information.

import argparse
import logging
import sys
import time
from typing import Tuple, Optional

import requests
import xml.etree.ElementTree as ET


class TeeLogger:
    """Dispatch log messages of varying verbosity to console and file handlers."""

    def __init__(self, console_logger: logging.Logger, file_logger: logging.Logger):
        self._console = console_logger
        self._file = file_logger

    def debug(self, msg, *args, **kwargs):
        # self._file.debug(msg, *args, **kwargs)
        self._console.debug(msg, *args, **kwargs)

    def info(self, msg, *args, **kwargs):
        self._file.info(msg, *args, **kwargs)
        self._console.info(msg, *args, **kwargs)

    def warning(self, msg, *args, **kwargs):
        self._file.warning(msg, *args, **kwargs)
        self._console.warning(msg, *args, **kwargs)

    def error(self, msg, *args, **kwargs):
        self._file.error(msg, *args, **kwargs)
        self._console.error(msg, *args, **kwargs)

    def exception(self, msg, *args, **kwargs):
        self._file.exception(msg, *args, **kwargs)
        self._console.exception(msg, *args, **kwargs)

    def log(self, level, msg, *args, **kwargs):
        self._file.log(level, msg, *args, **kwargs)
        self._console.log(level, msg, *args, **kwargs)

    def __getattr__(self, attr):
        return getattr(self._console, attr)


def setup_logger(log_file: str, debug: bool) -> TeeLogger:
    console_logger = logging.getLogger("mtc_client.console")
    console_logger.setLevel(logging.DEBUG if debug else logging.INFO)
    console_logger.handlers.clear()
    console_logger.propagate = False

    file_logger = logging.getLogger("mtc_client.file")
    file_logger.setLevel(logging.DEBUG)
    file_logger.handlers.clear()
    file_logger.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    ch.setLevel(logging.DEBUG if debug else logging.INFO)
    console_logger.addHandler(ch)

    fh = logging.FileHandler(log_file, mode="a", encoding="utf-8")
    fh.setFormatter(fmt)
    fh.setLevel(logging.DEBUG)
    file_logger.addHandler(fh)

    return TeeLogger(console_logger, file_logger)


def log_request_response(
    logger: logging.Logger,
    method: str,
    url: str,
    response: requests.Response,
    label: str = "",
) -> None:
    """Log request and response to console & file."""
    prefix = f"[{label}] " if label else ""
    # logger.debug("%sREQUEST: %s %s", prefix, method, url)
    # logger.debug(
    #     "%sRESPONSE: %s %s", prefix, response.status_code, response.reason
    # )
    # Log full body; if you want truncation, change the next line.
    logger.info("\n%s\n%s",  response.text, "-" * 80)


def get_namespace(root: ET.Element) -> str:
    """Extract XML namespace from root tag, e.g. {urn:...}MTConnectStreams."""
    if root.tag.startswith("{"):
        return root.tag.split("}")[0][1:]
    return ""


def parse_header_sequences(
    xml_text: str, logger: Optional[logging.Logger] = None
) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    """
    Parse bufferSize, firstSequence, nextSequence/lastSequence from an MTConnect
    response document. Works for /current and /sample responses.

    Returns:
        (buffer_size, first_sequence, next_sequence)
    """
    try:
        root = ET.fromstring(xml_text)
    except Exception as e:
        if logger:
            logger.error("Failed to parse XML: %s", e)
        return None, None, None

    ns = get_namespace(root)
    ns_prefix = f"{{{ns}}}" if ns else ""
    header = root.find(f".//{ns_prefix}Header")
    if header is None:
        if logger:
            logger.error("No <Header> element found in MTConnect document")
        return None, None, None

    buffer_size = header.attrib.get("bufferSize")
    first_seq = header.attrib.get("firstSequence")
    next_seq = header.attrib.get("nextSequence") or header.attrib.get("lastSequence")

    def to_int(name: str, value: Optional[str]) -> Optional[int]:
        if value is None:
            return None
        try:
            return int(value)
        except ValueError:
            if logger:
                logger.error("Invalid %s value in header: %r", name, value)
            return None

    return (
        to_int("bufferSize", buffer_size),
        to_int("firstSequence", first_seq),
        to_int("nextSequence/lastSequence", next_seq),
    )


POSITION_LABELS = {
    "path_pos": "machine",
    "path_pos_work": "work",
    "path_pos_cmd": "commanded",
}


def _format_xyz(text: str) -> Optional[Tuple[str, str, str]]:
    parts = text.strip().split()
    if len(parts) < 3:
        return None
    formatted = []
    for part in parts[:3]:
        try:
            formatted.append(f"{float(part):.5f}")
        except ValueError:
            formatted.append(part)
    return tuple(formatted)  # type: ignore[return-value]


def emit_line_events(xml_text: str, logger: logging.Logger) -> None:
    """Print line-number events and position coordinates to the console."""
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return
    ns = get_namespace(root)
    ns_prefix = f"{{{ns}}}" if ns else ""
    for elem in root.findall(f".//{ns_prefix}LineNumber"):
        data_item = elem.attrib.get("dataItemId", "")
        if data_item.lower() != "line_number":
            continue
        seq = elem.attrib.get("sequence", "?")
        ts = elem.attrib.get("timestamp", "?")
        value = (elem.text or "").strip()
        sub_type = elem.attrib.get("subType", "ABSOLUTE")
        logger.debug(
            "LineNumber (%s) seq=%s ts=%s value=%s", sub_type, seq, ts, value
        )

    for elem in root.findall(f".//{ns_prefix}Position"):
        data_item = elem.attrib.get("dataItemId", "")
        label = POSITION_LABELS.get(data_item)
        if not label:
            continue
        coords = _format_xyz(elem.text or "")
        if not coords:
            continue
        logger.debug(
            "Position %s: (%s, %s, %s)", label, coords[0], coords[1], coords[2]
        )


def run_client(
    agent_url: str,
    logger: logging.Logger,
    sample_count: int,
    sample_interval_sec: float,
) -> None:
    """
    Main MTConnect client loop:
      1. /probe (once)
      2. /current to get bufferSize + nextSequence
      3. loop: /sample?from=nextSequence&count=... (update nextSequence each time)
    """
    sess = requests.Session()

    # 1) PROBE – describe devices
    probe_url = f"{agent_url.rstrip('/')}/probe"
    resp = sess.get(probe_url, timeout=10)
    log_request_response(logger, "GET", probe_url, resp, label="PROBE")
    emit_line_events(resp.text, logger)
    resp.raise_for_status()

    # 2) CURRENT – snapshot, get bufferSize + nextSequence
    current_url = f"{agent_url.rstrip('/')}/current"
    resp = sess.get(current_url, timeout=10)
    log_request_response(logger, "GET", current_url, resp, label="CURRENT")
    emit_line_events(resp.text, logger)
    resp.raise_for_status()

    buffer_size, first_seq, next_seq = parse_header_sequences(resp.text, logger)
    if buffer_size is None or next_seq is None:
        logger.error("Could not determine bufferSize or nextSequence from /current; exiting.")
        return

    # Count MUST be > 0 and <= bufferSize to conform with spec
    # (spec says out-of-range count should cause OUT_OF_RANGE error).
    count = max(1, min(sample_count, buffer_size))
    logger.debug(
        "MTConnect bufferSize=%d, firstSequence=%s, nextSequence=%d, using count=%d",
        buffer_size,
        first_seq,
        next_seq,
        count,
    )

    # 3) SAMPLE LOOP – keep sampling from nextSequence forward
    while True:
        params = {"from": next_seq, "count": count}
        sample_url = f"{agent_url.rstrip('/')}/sample"

        try:
            resp = sess.get(sample_url, params=params, timeout=10)
        except Exception as e:
            logger.error("Error calling /sample: %s", e)
            time.sleep(sample_interval_sec)
            continue

        # Log every request + response body
        # Include the full URL with query parameters
        log_request_response(
            logger, "GET", resp.url, resp, label="SAMPLE"
        )
        emit_line_events(resp.text, logger)

        if resp.status_code >= 400:
            logger.error(
                "Sample request returned error status %d, trying to resync with /current",
                resp.status_code,
            )
            # Resync: call /current again to recover sequence range
            try:
                resp_current = sess.get(current_url, timeout=10)
                log_request_response(
                    logger, "GET", current_url, resp_current, label="RESYNC-CURRENT"
                )
                emit_line_events(resp_current.text, logger)
                resp_current.raise_for_status()
                buffer_size, first_seq, next_seq_new = parse_header_sequences(
                    resp_current.text, logger
                )
                if buffer_size is not None and next_seq_new is not None:
                    buffer_size = buffer_size
                    next_seq = next_seq_new
                    count = max(1, min(sample_count, buffer_size))
                    logger.debug(
                        "Resynced: bufferSize=%d, firstSequence=%s, nextSequence=%d, using count=%d",
                        buffer_size,
                        first_seq,
                        next_seq,
                        count,
                    )
            except Exception as e:
                logger.error("Failed to resync with /current: %s", e)

            time.sleep(sample_interval_sec)
            continue

        # Parse header from /sample to get updated nextSequence
        _, _, new_next_seq = parse_header_sequences(resp.text, logger)
        if new_next_seq is not None:
            # MTConnect standard says client should use nextSequence returned in the response.
            logger.debug("Advancing nextSequence from %d to %d", next_seq, new_next_seq)
            next_seq = new_next_seq
        else:
            # Fallback if header parsing fails
            next_seq += count

        time.sleep(sample_interval_sec)


def main():
    parser = argparse.ArgumentParser(
        description="MTConnect HTTP client with optional verbose request/response logging."
    )
    parser.add_argument(
        "--agent-url",
        default="http://localhost:15000",
        help="Base URL of MTConnect Agent (default: http://localhost:15000)",
    )
    parser.add_argument(
        "--log-file",
        default="mtc_client.log",
        help="Path to log file (default: mtc_client.log)",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=100,
        help="Max number of observations per /sample request (default: 100)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="Delay in seconds between /sample requests (default: 1.0)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print full request/response traffic to console.",
    )
    args = parser.parse_args()

    logger = setup_logger(args.log_file, debug=args.debug)
    logger.debug("Starting MTConnect client against %s", args.agent_url)

    try:
        run_client(
            agent_url=args.agent_url,
            logger=logger,
            sample_count=args.count,
            sample_interval_sec=args.interval,
        )
    except KeyboardInterrupt:
        logger.debug("Interrupted by user, exiting.")


if __name__ == "__main__":
    main()
