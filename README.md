# SVL Signal Server

A Python daemon that drives physical signal masts on the Sunnyvale model railroad layout. It polls JMRI for the current state of turnouts, sensors, and memory variables, evaluates a YAML-based signaling ruleset, and then commands the hardware — either over JMRI's signal-head API or directly over an OpenLCB/LCC CAN bus connection.

## Overview

```
JMRI (turnouts, sensors,      signal_config.yaml
memory vars via JSON API)      (mast / route rules)
          │                            │
          ▼                            ▼
   signal_server.py ◄─────── signal_config.py
          │                            │
    ┌─────┴─────┐               (evaluates aspects)
    ▼           ▼
 jmri.py    openlcb.py
(JMRI API)  (LCC CAN bus)
    │           │
    ▼           ▼
 JMRI signal  Physical LCC
 heads        hardware
```

Every ~1.5 seconds the server:
1. Fetches current turnout, sensor, and memory-variable state from JMRI.
2. Evaluates each signal mast's routes in order using `signal_config.py`.
3. Determines the correct aspect (CLEAR, APPROACH, STOP, etc.) for each mast.
4. Sends the resulting head appearances to either JMRI or the LCC bus.
5. Publishes the snapshot to an optional HTTP `/status` endpoint.

---

## Requirements

- Python 3.8+
- [JMRI](https://www.jmri.org/) running with its JSON server enabled (default port 12080, proxied to 3000 by the SVL Panel Server)
- Python packages:

```
pip install pyyaml prettytable lxml
```

No additional packages are needed for the OpenLCB socket connection or the HTTP status server — both use the standard library.

---

## Running

```bash
python3 signal_server.py
```

The server connects to JMRI, waits for it to become available, then begins its poll loop. Press **Ctrl-C** to shut down gracefully (all masts are set to STOP before exit).

### Command-line options

| Flag | Default | Description |
|------|---------|-------------|
| `--jmri_host URL` | `http://127.0.0.1:3000` | Base URL of the JMRI JSON server. |
| `--status_port PORT` | `0` (disabled) | Start an HTTP server on this port exposing `/status` and `/reload`. |
| `--pretty` | off | Clear the terminal before each update and print a formatted table. |
| `--output_xml` | off | Print JMRI signal-head XML to stdout and exit (for bootstrapping JMRI config). |
| `--verbose` | off | Mirror log output to stdout in addition to the log file. |

### Examples

```bash
# Basic run, table view in terminal
python3 signal_server.py --pretty

# Custom JMRI host, status endpoint for the SVL Signal Editor
python3 signal_server.py --jmri_host http://192.168.1.10:3000 --status_port 8080

# Generate JMRI signal-head XML
python3 signal_server.py --output_xml > signal_heads.xml
```

---

## Logging

Log output goes to `svl_signal_server.log` in the working directory. The file rotates hourly and the last 6 hours are retained. Use `--verbose` to also echo log lines to stdout.

---

## HTTP Status API

Start the status server with `--status_port PORT`. Two endpoints are available:

### `GET /status`

Returns a JSON snapshot of every mast's current state.

```jsonc
{
  "masts": {
    "CP1_Main": {
      "type": "double_tri",       // single_tri | double_tri | cpl
      "aspect": "APPROACH",
      "appearance": "YELLOW over RED",
      "upper": "HEAD_YELLOW",
      "lower": "HEAD_RED",
      "lit": [
        {"color": "yellow", "flashing": false, "head": "upper"}
      ],
      "reason": "[Main] OK to CP2_Main, which is STOP",
      "ts": 1717700000.123
    }
  },
  "last_update_ts": 1717700000.123,
  "signaling_mode": "block",      // "block" or "dispatch"
  "config_path": "/path/to/signal_config.yaml",
  "uptime_sec": 3600,
  "consecutive_failures": 0,
  "jmri_alert": false             // true when JMRI has been unreachable long enough to alert
}
```

**Field reference — per mast:**

| Field | Description |
|-------|-------------|
| `type` | `single_tri` — single-head tri-light; `double_tri` — two-head tri-light; `cpl` — CPL color-position-light |
| `aspect` | Signaling aspect, `SIGNAL_` prefix stripped (e.g. `CLEAR`, `APPROACH`, `STOP`) |
| `appearance` | Human-readable head colors (e.g. `GREEN`, `YELLOW over RED`) |
| `upper` / `lower` | `HEAD_*` constant for each head (`lower` is `null` for single-head masts) |
| `lit` | List of lamps that are illuminated. Each entry: `{color, flashing, head}` |
| `reason` | Human-readable explanation of why this aspect was chosen |
| `ts` | Unix timestamp of this mast's last update |

**Top-level fields:**

| Field | Description |
|-------|-------------|
| `last_update_ts` | Unix timestamp of the last successful JMRI poll |
| `signaling_mode` | `"block"` or `"dispatch"` |
| `uptime_sec` | Seconds since the server process started |
| `consecutive_failures` | Number of consecutive failed JMRI polls (0 when healthy) |
| `jmri_alert` | `true` when JMRI has been unreachable for 3 or more consecutive polls. Signals are frozen at their last known state. A web panel can use this field to show a prominent warning to operators. |

### `GET /reload`

Forces the config cache to expire. The next poll cycle will re-parse `signal_config.yaml` from disk.

```json
{"reloaded": true}
```

Both endpoints set `Access-Control-Allow-Origin: *` so the SVL Signal Editor (running on a different port) can poll them directly from a browser.

---

## Configuration File (`signal_config.yaml`)

The config file maps signal mast names to mast definitions. Each mast has a head type and one or more named routes.

### Signal mast types

#### Single-head tri-light (`head_address`)

One signal head with green / yellow / red / flashing variants. `head_address` is a JMRI DCC address (integer) or an OpenLCB event ID string (16+ hex chars).

```yaml
CP1_Main:
  head_address: 101
  routes:
    Main:
      next_signal: CP2_Main
      requirements:
        - turnout: NT5
          state: closed
```

#### Double-head tri-light (`upper_head_address` + `lower_head_address`)

Two stacked heads. The upper head carries the primary aspect; the lower head shows diverging or speed-limit information. Both addresses must be the same type (both DCC integers or both LCC event IDs).

```yaml
CP1_Diverge:
  upper_head_address: 102
  lower_head_address: 103
  routes:
    Main:
      next_signal: CP2_Main
      requirements:
        - turnout: NT5
          state: closed
    Diverging:
      next_signal: CP2_Branch
      is_diverging: true
      requirements:
        - turnout: NT5
          state: thrown
```

#### CPL mast (`*_lamp_first_eventid`)

A color-position-light mast driven over LCC. Four lamps (green, yellow, red, lunar), each defined by the first event ID in its ON/FLASHING/OFF event triple.

```yaml
CP1_CPL:
  green_lamp_first_eventid:  "05.01.01.01.08.00.00.00"
  yellow_lamp_first_eventid: "05.01.01.01.08.00.00.10"
  red_lamp_first_eventid:    "05.01.01.01.08.00.00.20"
  lunar_lamp_first_eventid:  "05.01.01.01.08.00.00.30"
  routes:
    Main:
      next_signal: CP2_Main
      requirements:
        - turnout: NT5
          state: closed
```

### Route fields

| Field | Required | Description |
|-------|----------|-------------|
| `next_signal` | No | Name of the mast immediately ahead on this route. If omitted, assumes the track ahead is dark. |
| `requirements` | Yes | List of turnout and/or sensor conditions that must all be met for this route to be active. |
| `is_diverging` | No | `true` — convert the computed aspect to its diverging equivalent before applying. |
| `maximum_speed` | No | `slow` — cap at APPROACH; `restricting` — cap at RESTRICTING. |
| `dispatch_control` | No | Dispatch signaling config (see below). |

### Requirements

```yaml
requirements:
  # Turnout must be in a specific position
  - turnout: NT5
    state: closed        # or: thrown

  # Sensor (block detector) must be in a specific state
  - sensor: LS12
    state: false         # false = inactive (unoccupied); true = active (occupied)

  # Permissive sensor: if occupied, route is still active but aspect is capped at RESTRICTING
  - sensor: LS12
    state: false
    permissive: true
```

### Dispatch signaling

When the JMRI memory variable `IMSVL_DISPATCH_SIGNALING` equals `yes` (case-insensitive), the server switches to dispatch mode. In this mode, a route without a `dispatch_control` stanza shows DARK, and routes with one read a second memory variable to determine clearance.

```yaml
CP1_Main:
  head_address: 101
  routes:
    Main:
      next_signal: CP2_Main
      requirements:
        - turnout: NT5
          state: closed
      dispatch_control:
        memory_var: IMSVL_SECTION_A_EAST
        direction: East

    # A route that is always visible in dispatch mode (e.g. a yard lead)
    Yard:
      next_signal: CP2_Yard
      requirements:
        - turnout: NT5
          state: thrown
      dispatch_control:
        ignore: true
```

The dispatch memory variable value must follow the format `<token>:<status>:<dispatcher>`, for example `T:Authorized East:Jake`. The signal server reads the `<status>` field and grants clearance when it equals `Authorized <direction>`.

---

## Signaling Logic

### Aspects and their meanings

| Aspect | Appearance (single head) | Meaning |
|--------|--------------------------|---------|
| `CLEAR` | Green | Proceed at full speed |
| `ADVANCE_APPROACH` | Flashing yellow | Prepare to find next signal at APPROACH |
| `APPROACH` | Yellow | Slow down; next signal is STOP |
| `APPROACH_CLEAR_SIXTY` | Flashing green | Prepare for diverging at 60 mph |
| `APPROACH_CLEAR_FIFTY` | Flashing green | Prepare for diverging at 50 mph |
| `RESTRICTING` | Flashing red | Proceed at restricted speed, prepared to stop |
| `STOP` | Red | Stop |
| `DARK` | Dark | Mast not in service |

Diverging equivalents (`DIVERGING_CLEAR`, `DIVERGING_APPROACH`, etc.) are used when `is_diverging: true` is set on a route. On a single-head mast, diverging aspects are automatically collapsed to the nearest single-head equivalent.

### Aspect propagation

The aspect shown at any mast depends on the aspect of the **next** mast ahead:

| Next mast shows | This mast shows |
|-----------------|-----------------|
| CLEAR | CLEAR |
| ADVANCE_APPROACH | CLEAR |
| APPROACH | ADVANCE_APPROACH |
| STOP / DARK | APPROACH |
| DIVERGING_CLEAR | APPROACH_CLEAR_SIXTY |
| DIVERGING_CLEAR_LIMITED | APPROACH_CLEAR_FIFTY |
| DIVERGING_ADVANCE_APPROACH | APPROACH_CLEAR_FIFTY |
| DIVERGING_APPROACH / DIVERGING_RESTRICTING | APPROACH_DIVERGING |

### Multi-route masts

Each mast can have multiple routes. At most one route should be active at any time (its requirements satisfied). If no route is active the mast shows STOP. If more than one route is active simultaneously, the server logs an error and shows STOP.

---

## Module Reference

| File | Purpose |
|------|---------|
| `signal_server.py` | Entry point. Poll loop, exponential backoff, HTTP status server, graceful shutdown. |
| `signal_config.py` | Parses `signal_config.yaml`; implements `SignalMast`, `SignalRoute`, and aspect logic. |
| `signal_requirements.py` | `SensorRequirement` and `TurnoutRequirement` classes. |
| `enums.py` | String constants for all aspects, head appearances, and turnout/sensor states. Also `ConvertAspectToDivergingAspect`. |
| `jmri.py` | Thin wrapper around the JMRI JSON HTTP API. Fetches turnout/sensor/memory data; sets signal-head appearances and memory variables. |
| `openlcb.py` | Manages a TCP socket connection to the OpenLCB hub (port 12021). Sends LCC CAN frames for signal events; re-broadcasts its cache on node initialization packets. |
| `layout_handle.py` | Abstract base class (`LayoutHandle`) that both `JMRI` and `OpenlcbLayoutHandle` implement. |

---

## OpenLCB / LCC Details

`OpenlcbLayoutHandle` connects to the OpenLCB hub at `localhost:12021` (the standard JMRI hub port). Each signal head or lamp is identified by its first event ID. Appearances are mapped to event offsets:

| Offset | Appearance |
|--------|------------|
| +0 | Green / ON |
| +1 | Yellow / FLASHING |
| +2 | Red / OFF |
| +3 | Flashing green |
| +4 | Flashing yellow |
| +5 | Flashing red |
| +6 | Dark |

The handle maintains a cache keyed by mast name. If the hardware reboots and broadcasts an "Initialization Complete" packet (MTI `0x100` or `0x101`), the cache is re-broadcast in full so all signals are restored without waiting for the next poll cycle.

---

## Running Tests

```bash
python3 -m unittest test_signals -v
```

The test suite covers 100 cases across 13 test classes — no real JMRI connection or LCC hardware is required.

| Test class | What it tests |
|------------|---------------|
| `TestConvertAspectToDivergingAspect` | `enums.ConvertAspectToDivergingAspect` mapping |
| `TestGetNextMostPermissiveAspect` | Aspect propagation table in `signal_config` |
| `TestDispatchSignalingMode` | Dispatch mode toggle from JMRI memory variable |
| `TestSignalSummary` | `SignalSummary` dataclass, prefix stripping, `PrettyAppearance` |
| `TestSensorRequirement` | Sensor satisfied / unsatisfied / permissive / unknown |
| `TestTurnoutRequirement` | Turnout satisfied / unsatisfied / missing / unknown |
| `TestSingleHeadGetAppearance` | All aspects mapped to `HEAD_*` for a single tri-light head |
| `TestCPLGetAppearance` | CPL aspect-to-head mapping (lunar for restricting) |
| `TestDoubleHeadPutAspect` | Full `PutAspect` round-trip with a fake layout handle |
| `TestSignalRoute` | `GetAspectOrNone` — requirements, diverging, speed caps, permissive |
| `TestHeadAppearanceToLitColors` | `_HeadAppearanceToLitColors` helper (color, flashing, head fields) |
| `TestDetermineMastTypeAndHeads` | `_DetermineMastTypeAndHeads` — type detection, head parsing, lit list |
| `TestAlertThresholds` | Alert trigger/repeat/recovery conditions and banner output |
