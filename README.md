# F1 Race Engineer & Telemetry Assistant

A personal race engineer for **F1 25** and **F1 26**. Reads live UDP
telemetry on your own PC and turns it into lap analysis, tyre and stint
modelling, race intelligence, pit strategy, driving feedback and
cross-session progression.

It is a normal desktop window. There is no in-game overlay, no injection
into the game, and no language model anywhere in the decision path —
every recommendation comes from a deterministic rule or an explicit cost
model you can read in the source.

> **What is verified, honestly.** Packet reception and decoding have been
> tested against real F1 26 game packets. Everything downstream — laps,
> stints, strategy, coaching, suggestions, history — is verified against
> synthetic and replayed telemetry only. See
> [Verification status](#verification-status) for the exact split.

---

## Installation

Developed and tested on **Python 3.14** on Windows 11. The only
requirements are PySide6 6.6+ and pytest.

```bash
python -m venv .venv
```

```bash
.venv\Scripts\python -m pip install -r requirements.txt
```

Nothing is installed system-wide and nothing is written to the install
directory. User data lives in `%APPDATA%\F1RaceEngineer`.

## Running the application

```bash
.venv\Scripts\python -m app.main
```

| Flag | Purpose |
| --- | --- |
| *(none)* | Launch the GUI |
| `--selftest` | Start every subsystem, report status, exit |
| `--diagnose` | Trace the telemetry pipeline stage by stage, exit |
| `--headless` | Run telemetry with no UI |
| `--verbose` | Debug logging |
| `--port N` | Override the telemetry UDP port **for this run only** |
| `--mode f1_25\|f1_26` | Start in a specific game mode, for this run only |

`--port`, `--mode` and `--selftest` never write to your saved settings. A
one-off `--port 20800` must not quietly become the port the app listens on
tomorrow.

**Launch only once.** The receiver binds the UDP port exclusively, so a
second copy refuses the port loudly instead of silently receiving nothing.

---

## Telemetry configuration

### F1 25 setup

In game: **Settings → Telemetry Settings**

| Setting | Value |
| --- | --- |
| UDP Telemetry | **On** |
| UDP Broadcast Mode | **Off** |
| UDP IP Address | `127.0.0.1` |
| UDP Port | `20777` |
| UDP Send Rate | `60 Hz` |
| UDP Format | `2025` |

### F1 26 setup

Identical, with **UDP Format `2026`**. Select **F1 26** in the app's
sidebar so the mode's own settings, car list, track list and stored
sessions are used.

Two details found by testing against the real game, not assumed:

- **F1 26 sends 24 cars per packet array**, where earlier titles sent 22.
  The parser does not hardcode either. It solves the car count and stride
  arithmetically from the payload size and validates the result, then
  caches the winner.
- The app accepts packet formats by **range**, not an allowlist. An
  allowlist silently rejected every format-2026 packet, which looks
  exactly like the game not sending at all.

If the game sends a format the selected mode did not expect, Diagnostics
flags it and telemetry keeps parsing anyway. A mode mismatch is a warning,
never a failure.

### The pipeline is a ladder, not a boolean

"Socket is open" and "data is arriving" are different problems, and only
an explicit ladder makes that visible:

| Stage | Meaning |
| --- | --- |
| `0/6 Error` | Port held by another process |
| `1/6 Waiting` | Listener not started |
| `2/6 UDP socket bound` | Listening, **zero packets** — game config issue |
| `3/6 Packets received` | Bytes arriving but none parse — version mismatch |
| `4/6 Packets parsed` | Decoding, no complete frame yet |
| `5/6 Telemetry valid` | Frames produced, then went quiet |
| `6/6 Telemetry live` | Working |

### Live, stale, and no data

Three states, never two:

| State | Meaning | On screen |
| --- | --- | --- |
| `NO DATA` | Nothing has ever arrived | Blank |
| `LIVE` | Telemetry is arriving | Current values |
| `STALE` | Telemetry stopped | **Last known values, marked stale** |

Going stale never blanks the screen and never rewrites history. A lap you
completed is a fact; it does not stop being a fact because the next packet
failed to arrive.

---

## Car selection

**Car** page. Each mode has its own car list, under
`%APPDATA%\F1RaceEngineer\modes\<mode>\cars`. Ratings shipped with the app
are **priors** — starting assumptions, flagged as such, carrying low
confidence. They are
never presented as measured fact and never overwrite what your own driving
measures. You can edit any record and reset it to the shipped values.

## Track selection

**Track** page, same arrangement, under `modes\<mode>\tracks` in the same
place.

### Where a number came from

Every value the app shows is labelled with its origin, and these never
blur together:

| Source | Meaning |
| --- | --- |
| `PROFILE` | Shipped or user-edited prior |
| `OBSERVED` | Measured from your own sessions, with sample count |
| `INFERENCE` | A conclusion drawn from the above — always labelled, never stated as fact |

Observed data is written to `modes/<mode>/observed/` and never overwrites a
profile. Laps run in traffic, in the wet, behind a safety car, on an
in-lap, or with unusual fuel burn are excluded before anything is learned
from them, because those describe the conditions rather than the car.

---

## Smart Suggestions

**Suggestions** page, with the single most important one mirrored on the
Dashboard.

Suggestions are arbitrated, not stacked. Every engine below can produce
advice at the same time, and a driver reading a list mid-corner reads
nothing — so they compete on category, severity and priority, and only the
winner reaches the driving view. Each carries its confidence and an
explanation of what it was derived from. Nothing repeats: every suggestion
has a cooldown and a lifecycle.

## Strategy

**Strategy** page. A deterministic cost model, stated in the source:

```
stay out            d_now * sum(age+1 .. age+R)
pit after k laps    d_now * sum(age+1 .. age+k)
                    + pit_loss
                    + d_next * sum(1 .. R-k)
```

where `d` is measured degradation per lap and `R` is the laps remaining.
Traffic and track position are weighted in explicitly. The safety-car pit
loss factor is a **stated modelling assumption**, not a measurement, and
it caps the confidence of any recommendation that depends on it below
`HIGH`.

Strategy can never be more confident than the tyre model it rests on. That
is enforced by a test, not by convention.

## Driver Coach

**Driver** page. Reports where time is going, and whether it is improving.

It works from sector times and your own bests — never from a reference lap
it does not have. It will not name a corner or a braking distance, because
the telemetry does not identify corners. Every observation is labelled
either `MEASURED DIRECTLY` or `CORRELATION — NOT PROOF OF CAUSE`, and time
loss is reported as the mean across the slow laps rather than a worst case.

Problems are kept for the whole session, including after telemetry stops.

## History

**History** page. Sessions are stored per mode in
`modes/<mode>/sessions/`, one file each, and **nothing is ever
auto-deleted**.

The record is written after **every completed lap**, not at shutdown, so a
crash, a power cut or a killed game costs at most the lap in progress
rather than the session. Two sessions are only compared when the mode, car
and track match.

Progression across sessions needs several comparable sessions before it
will say anything at all; until then it says `INSUFFICIENT DATA` rather
than reporting noise as a trend.

---

## Recording

Captures **raw packets exactly as the game sent them** — nothing parsed on
the way in. A recording is therefore ground truth about the wire format
even if the parser is wrong.

```
Inspector -> Start Recording -> drive 30-60s -> Stop
```

## Replay

Feeds those bytes to the *same* adapter method the UDP listener calls.
There is no second parsing path, so a bug found in replay is the real bug.

```
Inspector -> pick a recording -> Load -> Play  (or Step, one packet at a time)
```

Live and replay are mutually exclusive: two sources feeding one normalized
state would be impossible to reason about.

---

## Diagnostics

**Diagnostics** page — raw packet counts, parse counts, packet rate,
bytes/s, per-packet-type breakdown, and a RAW vs NORMALIZED vs UI
comparison table that localises a bad value to the parser, the adapter or
the UI binding in one glance.

**Inspector** page — per field: is it present, is it changing, what range
has been seen.

| Verdict | Meaning |
| --- | --- |
| `OK` | Present and changing |
| `STATIC` | Arrived but never changed — may be legitimate, may be a bad offset |
| `ABSENT` | Arrived but always zero/empty — usually a missing packet |
| `NO DATA` | No frame carrying it yet |

`STATIC` is the verdict that matters. The parsing failure that cost the
most time on this project looked perfectly healthy, because it was a
plausible constant.

---

## Troubleshooting

**Stage stuck at `2/6` — bound, but no packets.**
The game is not sending. Check UDP Telemetry is On, the IP is `127.0.0.1`,
the port matches, and that you are **in a session** — F1 does not stream
from the menus.

**Stage stuck at `3/6` — packets arrive, none parse.**
A format mismatch. Run `--diagnose` and read the reported packet format,
then set the matching game mode.

**Stage `0/6` — port held by another process.**
A second copy of the app is running, or a previous run did not exit. Close
it. The exclusive bind is deliberate: without it a second listener
silently steals packets on Windows and both copies appear broken.

**Values frozen on screen.**
They are not frozen, they are `STALE` and labelled as such. Telemetry
stopped; the last known values are kept deliberately.

**Everything reads `UNAVAILABLE` on the Race page for the car behind.**
Expected. Only the player's own lap data is decoded — see
[Known limitations](#known-limitations).

**The app closed with an error dialog.**
The full traceback is written to
`%APPDATA%\F1RaceEngineer\logs\f1_race_engineer.log`.

**Diagnosing without launching the app:**

```bash
.venv\Scripts\python -m app.main --diagnose
```

This binds only for the probe, never starts the engine, and never takes
the port the app would use.

---

## Architecture

```
app/
  main.py                    entry point (GUI / headless / selftest / diagnose)
  core/
    application.py             composition root and lifecycle
    models.py                  TelemetryFrame - the game-agnostic contract
    telemetry_state.py         LIVE / STALE / NO_DATA, thread-safe
    events.py, logging.py, paths.py
  games/
    base.py                    GameAdapter ABC + pipeline stage ladder
    modes.py                   GameMode, capabilities, version config
    f1/                        packets, parser, UDP listener, adapter
    forza/                     placeholder, honestly reported as unsupported
  diagnostics/                 health collection + standalone probe
  config/
    settings.py                global settings (active mode, window, logging)
    mode_settings.py           per-mode settings (port, units, preferences)
  telemetry/
    recording.py               raw packet capture (.f1re container)
    replay.py                  deterministic playback through the live path
    inspector.py               per-packet and per-field validation
  domain/
    driver_session.py          lap/behaviour collection, no inference
    lap_analysis.py            lap classification, outliers, confidence
    stints.py                  stint construction and degradation
    race_intelligence.py       gaps, attack/defence, DRS, race phase
    strategy.py                pit cost model
    driver_coach.py            where time is going
    profile_intelligence.py    PROFILE / OBSERVED / INFERENCE
    session_history.py         sessions, personal bests, progression
    smart_suggestions.py       arbitration across every engine above
    car_profiles.py            car performance priors
    store.py                   editable JSON record store
    track_profiles.py          circuit characteristics
  ui/                          theme, widgets, 16 pages
tests/                         760 tests
```

Only the adapter parses packets. No analysis module imports the parser or
unpacks bytes — enforced by a test, because two layers interpreting the
same bytes is how they start disagreeing.

Telemetry runs on its own thread and writes to `TelemetryState`; the UI
polls an immutable snapshot at 20 Hz. Analysis runs on **lap completion**,
never per packet.

The session clock is derived from frames observed, not wall time, so a
replay produces exactly the same conclusions as the live run that recorded
it.

### Per-mode isolation

Settings, cars, tracks, learned profiles and stored sessions all live
under `modes/<mode>/`. F1 25 and F1 26 share no path, so switching modes
cannot overwrite the other mode's data and switching back restores it
exactly. That is a storage guarantee, covered by tests, not a convention.

### Storage safety

Every data file is written to a uniquely-named temporary file and renamed
into place. Readers and writers share a lock, because on Windows replacing
a file that another thread holds open fails outright — which loses the
save silently. A corrupt file is skipped with a warning rather than taking
the rest of the history down with it.

---

## Tests

```bash
.venv\Scripts\python -m pytest
```

**760 tests, all passing.** Packet parsing against byte-exact spec-built
packets; malformed, truncated and hostile input; version tolerance; the
stage ladder; exclusive bind; burst handling; a full field trace over a
real UDP socket; staleness; settings persistence; per-mode isolation;
recording round-trip fidelity; replay determinism; inspector verdicts; lap
classification; stint degradation; race intelligence; the strategy cost
model; coaching evidence; profile source separation; session history and
progression; suggestion arbitration and cooldowns; and an integration
suite covering the seams between all of them.

---

## Verification status

The distinction below is deliberate and worth reading before trusting any
number this application shows you.

**Verified against real F1 game packets:**

- UDP reception from a running F1 26 session
- Packet header and format decoding (format 2026 identified correctly)
- The 24-car array layout in F1 26, and the stride solver that finds it
- Packet-rate and packet-type accounting in Diagnostics

**Verified against synthetic and replayed telemetry only:**

- Every field value beyond the header (verified byte-exact against the
  published spec over a real socket, but not yet cross-checked against a
  live session's on-screen values)
- Lap and sector analysis, tyre and stint modelling
- Race intelligence, strategy, driver coach
- Smart suggestion arbitration, session history and progression

Nothing in the second list is claimed to be validated in a real race. It
is claimed to be correct with respect to its inputs, and tested as such.

## Known limitations

- **Safety car and VSC are never detected.** The field exists and is wired
  through every consumer, but no adapter populates it, so every
  safety-car-aware behaviour is currently unreachable in a real session.
- **No opponent data.** Only the player's own lap data slice is decoded.
  The gap to the car behind, defence state and undercut projection are
  therefore reported as `UNAVAILABLE`, not estimated.
- **F1 26 tyre thermal data is unconfirmed.** The layout is recovered by a
  validated offset search rather than a documented offset, and has not
  been checked against a real F1 26 session.
- Corners are not identified, so coaching speaks in sectors.
- Windows only, in practice — the UDP and storage behaviour is written and
  tested against Windows semantics.
