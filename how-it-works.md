# How Feature Traceability Works

This document explains how feature IDs embedded in G-code flow through the system and end up in the logged MTConnect data.

## Overview

```
┌─────────────┐    ┌─────────────┐    ┌─────────────┐    ┌─────────────┐    ┌─────────────┐
│   G-code    │───▶│  LinuxCNC   │──▶│   Adapter   │──▶│   Agent     │───▶│   Logger    │
│   File      │    │  Simulator  │    │ (Python)    │    │  (Docker)   │    │  (Python)   │
└─────────────┘    └─────────────┘    └─────────────┘    └─────────────┘    └─────────────┘
                                             │                                      │
                                             │                                      ▼
                                             │                              ┌─────────────┐
                                             ▼                              │    Log      │
                                      ┌─────────────┐                       │ Processor   │
                                      │ SHDR Dump   │                       └─────────────┘
                                      │    File     │
                                      └─────────────┘
```

## Step 1: Feature IDs in G-code

Feature IDs are embedded as comments in the G-code file. G-code comments use parentheses:

```gcode
G0 X1.0 Y2.0 Z0.5
(HOLE 1 id=5510)
G1 Z-0.25 F10.0
G0 Z0.5
(HOLE 2 id=5511)
G1 Z-0.25 F10.0
```

These comments like `(HOLE 1 id=5510)` contain the feature identification that links the machining operation to the QIF model.

## Step 2: LinuxCNC Execution

When LinuxCNC runs the G-code:
- It tracks the current line number being executed
- It exposes machine state via a shared memory interface (`linuxcnc.stat`)
- It provides the path to the currently loaded G-code file

## Step 3: The Adapter (`adapter_lcnc.py`)

The adapter is the key component that extracts feature IDs and publishes them to MTConnect.

### How it works:

1. **Caches the G-code file** (`_refresh_gcode_cache`)
   - Reads and caches all lines of the active G-code file
   - Monitors file modification time to reload if changed

2. **Extracts comments** (`_extract_comment_text`)
   - For each G-code line, extracts text between parentheses
   - `"(HOLE 1 id=5510)"` → `"HOLE 1 id=5510"`

3. **Finds the current comment** (`_program_comment_from_program`)
   - Gets the current line number from LinuxCNC
   - Scans **backwards** through the cached G-code
   - Returns the most recent preceding comment
   - This comment persists until a new comment is encountered

4. **Publishes via SHDR** (`build_shdr_line`)
   - Every 20ms, builds an SHDR line with all data items:
   ```
   2025-01-15T10:30:45.123Z|avail|AVAILABLE|estop|ARMED|...|program_comment|HOLE 1 id=5510|...
   ```

### Key code sections:

```python
# Extract comment text from a G-code line
def _extract_comment_text(line: str) -> Optional[str]:
    start = stripped.find("(")
    end = stripped.find(")", start + 1)
    comment = stripped[start + 1 : end].strip()
    return comment

# Find the most recent comment before the current line
def _program_comment_from_program(stat, line_number: int) -> Optional[str]:
    idx = line_number - 2  # Start from previous line
    while idx >= 0:
        comment = _extract_comment_text(_GCODE_CACHE_LINES[idx])
        if comment:
            return comment
        idx -= 1
    return None
```

## Step 4: The MTConnect Agent (Docker)

The agent receives SHDR data from the adapter and serves it via HTTP/XML.

### Configuration (`agent.container.cfg`)
- Connects to the adapter on port 7878
- Exposes HTTP API on port 5000 (mapped to 15000 externally)

### Device Model (`devices.xml`)
Defines the `PROGRAM_COMMENT` data item:
```xml
<DataItem id="program_comment" category="EVENT" type="PROGRAM_COMMENT" name="program_comment" />
```

### HTTP Endpoints
- `/probe` - Device description
- `/current` - Current snapshot of all data items
- `/sample` - Historical stream of data items

### Example response:
```xml
<MTConnectStreams>
  <Streams>
    <DeviceStream name="LinuxCNC">
      <ComponentStream component="Controller">
        <Events>
          <ProgramComment dataItemId="program_comment" timestamp="2025-01-15T10:30:45.123Z" sequence="1234">
            HOLE 1 id=5510
          </ProgramComment>
          <LineNumber dataItemId="line_number" timestamp="2025-01-15T10:30:45.123Z" sequence="1235">
            42
          </LineNumber>
        </Events>
      </ComponentStream>
    </DeviceStream>
  </Streams>
</MTConnectStreams>
```

## Step 5: The Logger (`mtc_logger.py`)

The logger polls the agent and saves responses to a log file.

### How it works:

1. **Initial setup**
   - Calls `/probe` to get device description
   - Calls `/current` to get initial state and buffer info

2. **Continuous polling loop**
   - Calls `/sample?from=<nextSequence>&count=<count>`
   - Logs the full XML response to file
   - Updates `nextSequence` for the next request

3. **Output format**
   - Writes timestamped XML responses to the log file
   - Each response contains all data items that changed since the last poll

### Usage:
```bash
python3 mtconnect/mtc_logger.py \
  --agent-url http://localhost:15000 \
  --log-file logs/mtc_client.log \
  --count 1000 \
  --interval 0.5
```

## Step 6: Log Processing (`log_processor.py`)

The log processor extracts and analyzes data from the raw logs.

### Functions:

1. **XML Extraction** (`filter_logs`)
   - Filters log file to extract only XML content
   - Removes timestamps and log level prefixes

2. **Data Grouping** (`group_series_by_comment`)
   - Groups sensor data (e.g., PathFeedrate) by ProgramComment
   - Enables analysis of machining parameters per feature

3. **Visualization** (`plot_series_by_comment`)
   - Plots time series data grouped by feature ID
   - Shows feedrate, spindle speed, etc. for each feature

### Usage:
```bash
# Extract XML from logs
python3 mtconnect/log_processor.py logs/mtc_client.log -o logs/xml_only.xml

# For analysis (in Python):
from log_processor import group_series_by_comment, plot_series_by_comment
series = group_series_by_comment("logs/xml_only.xml", "PathFeedrate")
plot_series_by_comment(series, "PathFeedrate")
```

### Example output structure:
```python
{
    "HOLE 1 id=5510": [
        ("2025-01-15T10:30:45.123Z", 25.4),
        ("2025-01-15T10:30:45.143Z", 25.4),
        ...
    ],
    "HOLE 2 id=5511": [
        ("2025-01-15T10:30:46.123Z", 25.4),
        ...
    ],
}
```

## Data Flow Summary

| Stage | Component | Input | Output |
|-------|-----------|-------|--------|
| 1 | G-code file | CAM system | `(HOLE 1 id=5510)` comments |
| 2 | LinuxCNC | G-code | Line numbers, machine state |
| 3 | Adapter | LinuxCNC stat | SHDR: `\|program_comment\|HOLE 1 id=5510` |
| 4 | Agent | SHDR stream | XML: `<ProgramComment>HOLE 1 id=5510</ProgramComment>` |
| 5 | Logger | Agent HTTP | Log file with XML responses |
| 6 | Processor | Log file | Grouped data by feature ID |
