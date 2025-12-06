#!/usr/bin/env python3

# Copyright (c) 2025 Rubypoint
#
# Licensed under the MIT License. See the LICENSE file in the project root for full license information.

"""
Simple MTConnect SHDR adapter for LinuxCNC (simulation or real).

Publishes a subset of data items via SHDR on TCP port 7878 by default.

Data items sent (match Devices.xml):
  - avail             (AVAILABILITY)
  - estop             (EMERGENCY_STOP)
  - mode              (CONTROLLER_MODE)
  - execution         (EXECUTION)
  - line_number       (LINE_NUMBER, subType=ABSOLUTE)
  - block             (BLOCK)
  - program_comment   (PROGRAM_COMMENT)  <-- previous G-code comment
  - program           (PROGRAM)
  - path_pos          (POSITION, ACTUAL, 3-axis)
  - path_pos_cmd      (POSITION, COMMANDED, 3-axis)
  - path_pos_work     (POSITION, WORK/ACTUAL, 3-axis)
  - path_feedrate     (PATH_FEEDRATE)
  - path_feedrate_ovr (PATH_FEEDRATE, OVERRIDE)
  - spindle_speed     (SPINDLE_SPEED)
  - spindle_speed_ovr (SPINDLE_SPEED, OVERRIDE)
  - coolant_flood     (COOLANT, FLOOD)
  - coolant_mist      (COOLANT, MIST)
  - tool_number       (TOOL_NUMBER)
  - work_offset       (WORK_OFFSET)

Notes:
  - This is intentionally minimal and robust for sim use. It ignores input
    from the Agent and only writes SHDR lines periodically.
  - Mapping for execution/mode is best-effort for Axis + milltask.
"""
from __future__ import annotations

import argparse
import logging
import math
import os
import random
import select
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Set, TextIO

import linuxcnc  # provided by LinuxCNC install


HOST: str = os.environ.get("SHDR_HOST", "127.0.0.1")
PORT: int = int(os.environ.get("SHDR_PORT", "7878"))
PERIOD: float = float(os.environ.get("SHDR_PERIOD", "0.02"))  # seconds
LOG_LEVEL: str = os.environ.get("SHDR_LOG_LEVEL", "INFO").upper()
SHDR_DUMP_FILE: Optional[str] = os.environ.get("SHDR_DUMP_FILE")
if SHDR_DUMP_FILE:
    SHDR_DUMP_FILE = os.path.expanduser(SHDR_DUMP_FILE)
NOISE_SEED = os.environ.get("SHDR_NOISE_SEED")
if NOISE_SEED is not None:
    try:
        random.seed(int(NOISE_SEED))
    except ValueError:
        random.seed(NOISE_SEED)

NOISE_POS_STD = float(os.environ.get("SHDR_NOISE_POS_STD", "0.0"))
NOISE_WORK_POS_STD = float(os.environ.get("SHDR_NOISE_WORK_POS_STD", "0.0"))
NOISE_DTG_STD = float(os.environ.get("SHDR_NOISE_DTG_STD", "0.0"))
NOISE_FEED_STD = float(os.environ.get("SHDR_NOISE_FEED_STD", "0.0"))
NOISE_FEED_OVR_STD = float(os.environ.get("SHDR_NOISE_FEED_OVR_STD", "0.0"))
NOISE_SPINDLE_STD = float(os.environ.get("SHDR_NOISE_SPINDLE_STD", "0.0"))
NOISE_SPINDLE_OVR_STD = float(os.environ.get("SHDR_NOISE_SPINDLE_OVR_STD", "0.0"))


def _jitter(value: float, sigma: float) -> float:
    """Add Gaussian noise with std-dev sigma (0 disables noise)."""
    if sigma <= 0.0:
        return value
    return value + random.gauss(0.0, sigma)


def _jitter_vector(vec: List[float], sigma: float) -> List[float]:
    """Apply jitter to each component of a vector."""
    if sigma <= 0.0:
        return vec
    return [_jitter(v, sigma) for v in vec]


logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
LOGGER: logging.Logger = logging.getLogger("lcnc-shdr")
_LAST_SHDR_BODY: Optional[str] = None
_LOG_FIELD_FILTER: Optional[Set[str]] = None

_GCODE_CACHE_PATH: Optional[str] = None
_GCODE_CACHE_MTIME: Optional[float] = None
_GCODE_CACHE_LINES: List[str] = []

# Last known program comment, so we can send it on every line
_LAST_PROGRAM_COMMENT: str = "UNAVAILABLE"


def connect_linuxcnc_stat() -> "linuxcnc.stat":
    """Obtain a linuxcnc.stat() handle, retrying until the status channel is ready."""
    while True:
        try:
            stat = linuxcnc.stat()
            # Probe once so we fail fast if the shared memory is not yet available.
            stat.poll()
            LOGGER.info("Connected to LinuxCNC status channel.")
            return stat
        except linuxcnc.error as exc:  # type: ignore[attr-defined]
            LOGGER.warning("LinuxCNC status unavailable (%s). Ensure LinuxCNC is running. Retrying...", exc)
            time.sleep(1.0)


def iso_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def map_avail(enabled: bool) -> str:
    return "AVAILABLE" if enabled else "UNAVAILABLE"


def map_estop(estop: int) -> str:
    # linuxcnc.stat().estop: 1 when estopped, 0 otherwise.
    return "TRIGGERED" if estop else "ARMED"


def map_mode(task_mode: int) -> str:
    # Map LinuxCNC task_mode to MTConnect CONTROLLER_MODE
    if task_mode == getattr(linuxcnc, "MODE_AUTO", 2):
        return "AUTOMATIC"
    if task_mode == getattr(linuxcnc, "MODE_MDI", 3):
        return "SEMI_AUTOMATIC"
    # MODE_MANUAL or unknown
    return "MANUAL"


def map_execution(stat: "linuxcnc.stat") -> str:
    # Best-effort mapping using interpreter/paused state
    # READY, ACTIVE, STOPPED, INTERRUPTED
    try:
        if stat.paused or stat.task_paused:
            return "INTERRUPTED"
        # interp_state: 1=idle, 2=reading, etc. Treat idle as READY
        if getattr(stat, "interp_state", 0) == 1 and stat.current_line == 0:
            return "READY"
        # If velocity is moving or a file is loaded, report ACTIVE conservatively
        if stat.current_vel > 0 or stat.current_line > 0:
            return "ACTIVE"
    except Exception:
        pass
    # Fallback
    return "READY"


def fmt_path_pos(stat: "linuxcnc.stat") -> str:
    # Use actual_position for machine coordinates X Y Z
    x, y, z = _jitter_vector(stat.actual_position[:3], NOISE_POS_STD)
    return f"{x:.5f} {y:.5f} {z:.5f}"


def fmt_path_pos_cmd(stat: "linuxcnc.stat") -> str:
    try:
        x, y, z = stat.position[:3]
    except Exception:
        x, y, z = stat.actual_position[:3]
    x, y, z = _jitter_vector([x, y, z], NOISE_POS_STD)
    return f"{x:.5f} {y:.5f} {z:.5f}"


def fmt_path_pos_work(stat: "linuxcnc.stat") -> str:
    # Work coords = machine actual minus G5x and G92, with XY rotation removed if present
    try:
        x, y, z = stat.actual_position[:3]
        gx, gy, gz = stat.g5x_offset[:3]
        g92x, g92y, g92z = stat.g92_offset[:3]
        x -= (gx + g92x)
        y -= (gy + g92y)
        z -= (gz + g92z)
        rot = float(getattr(stat, 'rotation_xy', 0.0) or 0.0)
        if abs(rot) > 1e-9:
            r = math.radians(rot)
            c, s = math.cos(r), math.sin(r)
            # Undo rotation: world(machine) = R * work; so work = R^-1 * world
            # R^-1 = R^T for pure rotation
            x, y = (c * x + s * y, -s * x + c * y)
    except Exception:
        pass
    x, y, z = _jitter_vector([x, y, z], NOISE_WORK_POS_STD)
    return f"{x:.5f} {y:.5f} {z:.5f}"


def fmt_feedrate(stat: "linuxcnc.stat") -> float:
    # current_vel is in units/min
    value = float(getattr(stat, "current_vel", 0.0) or 0.0)
    return _jitter(value, NOISE_FEED_STD)


def fmt_spindle(stat: "linuxcnc.stat") -> float:
    try:
        spindle = stat.spindle[0]
        rpm = float(spindle.get("speed", 0.0) or 0.0)
        return _jitter(rpm, NOISE_SPINDLE_STD)
    except Exception:
        return 0.0


def fmt_spindle_ovr(stat: "linuxcnc.stat") -> float:
    try:
        spindle = stat.spindle[0]
        # LinuxCNC reports override as a ratio (1.0 == 100%).
        ovr = float(spindle.get("override", 1.0) or 1.0) * 100.0
        return _jitter(ovr, NOISE_SPINDLE_OVR_STD)
    except Exception:
        return 100.0


def _should_log(line: str) -> bool:
    """Suppress repeated SHDR payloads (ignore timestamp portion)."""
    global _LAST_SHDR_BODY
    payload = line.split("|", 1)
    body = payload[1] if len(payload) == 2 else line
    if body == _LAST_SHDR_BODY:
        return False
    _LAST_SHDR_BODY = body
    return True


def _format_log_line(line: str) -> str:
    """Summarize the SHDR line using the field filter if provided."""
    stripped = line.strip()
    field_filter = _LOG_FIELD_FILTER
    if not stripped or not field_filter:
        return stripped
    parts = stripped.split("|")
    if len(parts) <= 1:
        return stripped
    ts, data = parts[0], parts[1:]
    filtered = []
    for i in range(0, len(data) - 1, 2):
        name, value = data[i], data[i + 1]
        if name.lower() in field_filter:
            filtered.append(f"{name}={value}")
    if not filtered:
        return f"{ts} (no requested fields present)"
    return f"{ts} {' '.join(filtered)}"


def _sanitize_block_text(text: str) -> str:
    """Normalize whitespace and remove SHDR delimiters."""
    return (
        str(text)
        .replace("\n", " ")
        .replace("\r", " ")
        .replace("|", " ")
        .strip()
    )


def _refresh_gcode_cache(path: str) -> None:
    """Cache the active G-code file to avoid re-reading it each cycle."""
    global _GCODE_CACHE_PATH, _GCODE_CACHE_MTIME, _GCODE_CACHE_LINES
    try:
        stat_info = os.stat(path)
    except OSError:
        _GCODE_CACHE_PATH = None
        _GCODE_CACHE_MTIME = None
        _GCODE_CACHE_LINES = []
        return
    mtime = stat_info.st_mtime
    if path == _GCODE_CACHE_PATH and _GCODE_CACHE_MTIME == mtime:
        return
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            _GCODE_CACHE_LINES = [line.rstrip("\r\n") for line in fh]
    except OSError:
        _GCODE_CACHE_PATH = None
        _GCODE_CACHE_MTIME = None
        _GCODE_CACHE_LINES = []
        return
    _GCODE_CACHE_PATH = path
    _GCODE_CACHE_MTIME = mtime


def _block_from_program(stat: "linuxcnc.stat", line_number: int) -> Optional[str]:
    """Return the raw G-code text for the current line if available."""
    path = getattr(stat, "file", None)
    if not path:
        return None
    _refresh_gcode_cache(path)
    if path != _GCODE_CACHE_PATH or line_number <= 0:
        return None
    idx = line_number - 1
    if idx < 0 or idx >= len(_GCODE_CACHE_LINES):
        return None
    return _GCODE_CACHE_LINES[idx]


def _extract_comment_text(line: str) -> Optional[str]:
    """Extract the comment text from a G-code line if present ( ( ... ) style )."""
    stripped = line.strip()
    if "(" not in stripped or ")" not in stripped:
        return None
    start = stripped.find("(")
    end = stripped.find(")", start + 1)
    if start == -1 or end == -1 or end <= start + 1:
        return None
    comment = stripped[start + 1 : end].strip()
    return comment or None


def _program_comment_from_program(stat: "linuxcnc.stat", line_number: int) -> Optional[str]:
    """
    Return the most recent preceding G-code comment as PROGRAM_COMMENT.

    We scan backwards from (line_number - 1) to find the last comment line.
    """
    path = getattr(stat, "file", None)
    if not path:
        return None
    _refresh_gcode_cache(path)
    if path != _GCODE_CACHE_PATH or line_number <= 1:
        return None
    idx = min(line_number - 2, len(_GCODE_CACHE_LINES) - 1)
    while idx >= 0:
        comment = _extract_comment_text(_GCODE_CACHE_LINES[idx])
        if comment:
            return comment
        idx -= 1
    return None


def _log_agent_request_line(dump_fh: Optional[TextIO], line: str) -> None:
    if not dump_fh:
        return
    clean = line.rstrip("\r")
    dump_fh.write(f"# {iso_ts()} agent> {clean}\n")
    dump_fh.flush()


def _drain_agent_requests(conn: socket.socket, dump_fh: Optional[TextIO], pending: str) -> str:
    """Capture any inbound agent requests so we can persist them to the dump file."""
    if not dump_fh:
        return pending
    while True:
        try:
            ready, _, _ = select.select([conn], [], [], 0)
        except (ValueError, OSError):
            break
        if not ready:
            break
        chunk = conn.recv(4096)
        if chunk == b"":
            raise ConnectionError("Agent closed connection.")
        pending += chunk.decode("utf-8", errors="replace")
        while "\n" in pending:
            line, pending = pending.split("\n", 1)
            _log_agent_request_line(dump_fh, line)
    return pending


def _flush_pending_requests(dump_fh: Optional[TextIO], pending: str) -> str:
    if dump_fh and pending:
        _log_agent_request_line(dump_fh, pending)
    return ""


def _get_line_index(stat: "linuxcnc.stat") -> int:
    """
    Determine an ABSOLUTE line index into the currently loaded program.

    Preference order:
      1) stat.motion_line (line feeding the motion system)
      2) stat.current_line (interpreter's current line)

    Returns 0 if no valid index is available.
    """
    idx = 0
    try:
        motion_line = getattr(stat, "motion_line", None)
        if isinstance(motion_line, int) and motion_line > 0:
            idx = motion_line
        else:
            current_line = getattr(stat, "current_line", 0)
            if isinstance(current_line, int) and current_line > 0:
                idx = current_line
    except Exception:
        idx = 0
    return idx if idx > 0 else 0


def shdr_line(stat: "linuxcnc.stat") -> str:
    """
    Build one SHDR line from the current LinuxCNC status.

    MTConnect mapping (modern, non-deprecated):
      - avail            -> AVAILABILITY
      - estop            -> EMERGENCY_STOP
      - mode             -> CONTROLLER_MODE
      - execution        -> EXECUTION
      - line_number      -> LINE_NUMBER (ABSOLUTE)
      - block            -> BLOCK (full G-code block text)
      - program_comment  -> PROGRAM_COMMENT (nearest preceding G-code comment)
      - program          -> PROGRAM
      - path_pos*        -> POSITION (3-axis, various subtypes)
      - path_feedrate*   -> PATH_FEEDRATE (+ OVERRIDE)
      - spindle_speed*   -> SPINDLE_SPEED (+ OVERRIDE)
      - coolant_*        -> COOLANT
      - tool_number      -> TOOL_NUMBER
      - work_offset      -> WORK_OFFSET
    """
    global _LAST_PROGRAM_COMMENT

    ts = iso_ts()

    # Basic states
    avail = map_avail(stat.enabled)
    estop = map_estop(stat.estop)
    mode = map_mode(stat.task_mode)
    execution = map_execution(stat)

    # LineNumber
    try:
        line_number = _get_line_index(stat)
    except Exception:
        line_number = 0

    # Block text: prefer G-code file content for the executing line.
    block_raw = _block_from_program(stat, line_number)
    if block_raw is None:
        try:
            block_raw = getattr(stat, "command", "") or getattr(stat, "read_line", "") or ""
        except Exception:
            block_raw = ""
    block = _sanitize_block_text(block_raw)

    # Program comment: last preceding G-code comment, persisted across lines
    new_comment = _program_comment_from_program(stat, line_number)
    if new_comment:
        _LAST_PROGRAM_COMMENT = _sanitize_block_text(new_comment)
    program_comment = _LAST_PROGRAM_COMMENT or "UNAVAILABLE"

    # Program name
    program = os.path.basename(stat.file) if stat.file else ""

    # Composite positions
    path_pos = fmt_path_pos(stat)
    path_pos_cmd = fmt_path_pos_cmd(stat)
    path_pos_work = fmt_path_pos_work(stat)

    # Per-axis positions (machine coordinates)
    ax, ay, az = _jitter_vector(stat.actual_position[:3], NOISE_POS_STD)

    # Distance-to-go: prefer stat.dtg, otherwise commanded-actual
    try:
        dx, dy, dz = getattr(stat, "dtg", (None, None, None))[:3]
        if dx is None:
            cx, cy, cz = (stat.position or stat.actual_position)[:3]
            dx, dy, dz = (cx - ax, cy - ay, cz - az)
    except Exception:
        dx, dy, dz = 0.0, 0.0, 0.0
    dx, dy, dz = _jitter_vector([dx, dy, dz], NOISE_DTG_STD)

    # Overrides
    try:
        feed_ovr = float(stat.feedrate or 1.0) * 100.0
    except Exception:
        feed_ovr = 100.0
    feed_ovr = _jitter(feed_ovr, NOISE_FEED_OVR_STD)
    spindle_ovr = fmt_spindle_ovr(stat)

    # Coolant
    flood = "ON" if getattr(stat, "flood", False) else "OFF"
    mist = "ON" if getattr(stat, "mist", False) else "OFF"

    # Tool and work offset
    tool = int(getattr(stat, "tool_in_spindle", 0) or 0)
    g5x = int(getattr(stat, "g5x_index", 1) or 1)
    work_name = f"G{53 + g5x}" if g5x >= 1 else "G53"

    # Feed & spindle
    feed = fmt_feedrate(stat)
    rpm = fmt_spindle(stat)

    # SHDR format: ts|name|value|name|value|...
    # NOTE: we no longer send deprecated "line" – use line_number + block.
    return (
        f"{ts}"
        f"|avail|{avail}"
        f"|estop|{estop}"
        f"|mode|{mode}"
        f"|execution|{execution}"
        f"|line_number|{line_number}"
        f"|block|{block}"
        f"|program_comment|{program_comment}"
        f"|program|{program}"
        f"|path_pos|{path_pos}"
        f"|path_pos_cmd|{path_pos_cmd}"
        f"|path_pos_work|{path_pos_work}"
        f"|x_pos|{ax:.5f}|y_pos|{ay:.5f}|z_pos|{az:.5f}"
        f"|x_dtg|{dx:.5f}|y_dtg|{dy:.5f}|z_dtg|{dz:.5f}"
        f"|path_feedrate|{feed:.3f}|path_feedrate_ovr|{feed_ovr:.1f}"
        f"|spindle_speed|{rpm:.1f}|spindle_speed_ovr|{spindle_ovr:.1f}"
        f"|coolant_flood|{flood}|coolant_mist|{mist}"
        f"|tool_number|{tool}"
        f"|work_offset|{work_name}\n"
    )


def serve() -> None:
    global _LAST_SHDR_BODY
    dump_fh: Optional[TextIO] = None
    dump_path = Path(SHDR_DUMP_FILE) if SHDR_DUMP_FILE else None
    if dump_path:
        dump_path.parent.mkdir(parents=True, exist_ok=True)
        dump_fh = dump_path.open("a", encoding="utf-8")
        LOGGER.info("Dumping SHDR traffic to %s", dump_path)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((HOST, PORT))
        srv.listen(1)
        LOGGER.info("SHDR adapter listening on %s:%s", HOST, PORT)
        try:
            while True:
                conn, addr = srv.accept()
                LOGGER.info("Agent connected from %s", addr)
                _LAST_SHDR_BODY = None
                s = connect_linuxcnc_stat()
                with conn:
                    conn.settimeout(5.0)
                    agent_rx_buffer = ""
                    try:
                        while True:
                            agent_rx_buffer = _drain_agent_requests(conn, dump_fh, agent_rx_buffer)
                            try:
                                s.poll()
                            except linuxcnc.error as exc:  # type: ignore[attr-defined]
                                LOGGER.warning("Lost LinuxCNC status (%s). Attempting to reconnect...", exc)
                                time.sleep(1.0)
                                s = connect_linuxcnc_stat()
                                continue
                            line = shdr_line(s)
                            conn.sendall(line.encode('ascii', errors='ignore'))
                            if dump_fh:
                                dump_fh.write(line)
                                dump_fh.flush()
                            stripped = line.strip()
                            if _should_log(stripped):
                                LOGGER.info("Sent SHDR: %s", _format_log_line(line))
                            time.sleep(PERIOD)
                    except (socket.timeout, ConnectionError, BrokenPipeError):
                        LOGGER.warning("Agent disconnected; waiting for reconnect...")
                        continue
                    finally:
                        agent_rx_buffer = _flush_pending_requests(dump_fh, agent_rx_buffer)
        finally:
            if dump_fh:
                dump_fh.close()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LinuxCNC MTConnect SHDR adapter")
    parser.add_argument(
        "--log-fields",
        metavar="FIELD",
        nargs="+",
        help=(
            "Only print the selected SHDR fields to the console log. "
            "Defaults to logging the entire SHDR line."
        ),
    )
    return parser.parse_args()


def _check_required_env_vars() -> None:
    """Check that required environment variables are set, exit with error if not."""
    required_vars = [
        "LINUXCNC_REPO_ROOT",
        "SHDR_HOST",
        "SHDR_DUMP_FILE",
        "SHDR_NOISE_FEED_STD",
    ]
    missing = [var for var in required_vars if not os.environ.get(var)]
    if missing:
        print("ERROR: Required environment variables are not set:", file=sys.stderr)
        for var in missing:
            print(f"  - {var}", file=sys.stderr)
        print("\nPlease run 'source setup.sh' from the repo root before running this script.", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    global _LOG_FIELD_FILTER
    _check_required_env_vars()
    args = _parse_args()
    if args.log_fields:
        normalized = {field.strip().lower() for field in args.log_fields if field.strip()}
        _LOG_FIELD_FILTER = normalized or None
        if _LOG_FIELD_FILTER:
            LOGGER.info("Console logging limited to fields: %s", ", ".join(sorted(_LOG_FIELD_FILTER)))
    serve()


if __name__ == "__main__":
    main()
