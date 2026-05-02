#!/usr/bin/env python3
"""Find idle gaps and group activity into phases.

Reads the same env-overridable session-log paths as parse_sessions.py:
  CC_LOG, CD_LOG, OC_DB, OC_SID.
"""

import json
import os
import sqlite3
import datetime as dt
from pathlib import Path

HOME = Path.home()
def _required_path(env: str) -> Path:
    val = os.environ.get(env)
    if not val:
        raise SystemExit(f"set ${env} to the session log path (see README)")
    return Path(val)


CC_LOG = _required_path("CC_LOG")
CD_LOG = _required_path("CD_LOG")
OC_DB = Path(os.environ.get("OC_DB", HOME / ".local/share/opencode/opencode.db"))
OC_SID = os.environ.get("OC_SID")
if not OC_SID:
    raise SystemExit("set $OC_SID to the OpenCode session id (see README)")


def load_cc():
    timestamps = []
    with open(CC_LOG) as f:
        for line in f:
            d = json.loads(line)
            ts = d.get('timestamp')
            if ts:
                timestamps.append(dt.datetime.fromisoformat(ts.replace('Z', '+00:00')))
    return timestamps


def load_cd():
    timestamps = []
    with open(CD_LOG) as f:
        for line in f:
            d = json.loads(line)
            ts = d.get('timestamp')
            if ts:
                timestamps.append(dt.datetime.fromisoformat(ts.replace('Z', '+00:00')))
    return timestamps


def load_oc():
    conn = sqlite3.connect(OC_DB)
    cur = conn.cursor()
    cur.execute("SELECT time_created FROM message WHERE session_id=?", (OC_SID,))
    timestamps = [dt.datetime.fromtimestamp(r[0] / 1000.0, tz=dt.timezone.utc) for r in cur]
    cur.execute("SELECT time_created FROM part WHERE session_id=?", (OC_SID,))
    timestamps.extend(dt.datetime.fromtimestamp(r[0] / 1000.0, tz=dt.timezone.utc) for r in cur)
    return sorted(timestamps)


for label, ts_list in [('cc', load_cc()), ('cd', load_cd()), ('oc', load_oc())]:
    ts_list = sorted(ts_list)
    if not ts_list:
        continue
    gaps = []
    for i in range(1, len(ts_list)):
        delta = (ts_list[i] - ts_list[i - 1]).total_seconds()
        if delta > 30:
            gaps.append((ts_list[i - 1], ts_list[i], delta))
    print(f"\n=== {label}: {len(gaps)} gaps > 30s ===")
    for start, end, d in sorted(gaps, key=lambda x: -x[2])[:10]:
        print(f"  {start.isoformat()} → {end.isoformat()}  gap={d:.0f}s")

    # detect long stretches that could be "stuck" — many tool calls in same area
    # bin into 60s buckets
    buckets = {}
    for ts in ts_list:
        b = int((ts - ts_list[0]).total_seconds() // 60)
        buckets[b] = buckets.get(b, 0) + 1
    busy = sorted(buckets.items(), key=lambda x: -x[1])[:5]
    print(f"  Busiest minutes: {busy}")
