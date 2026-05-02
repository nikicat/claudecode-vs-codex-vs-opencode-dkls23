#!/usr/bin/env python3
"""Parse cc/cd/oc session logs and extract timing/iteration metrics.

Session log paths can be overridden via env vars:
  CC_LOG  — Claude Code JSONL transcript
  CD_LOG  — Codex rollout JSONL
  OC_DB   — OpenCode SQLite db
  OC_SID  — OpenCode session id
Outputs are written next to this script.
"""

import json
import sqlite3
import datetime
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
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


@dataclass
class SessionMetrics:
    name: str
    model: str = ""
    first_ts: str = ""
    last_ts: str = ""
    wall_seconds: float = 0.0
    active_seconds: float = 0.0  # excluding long idle gaps (>120s)
    user_messages: int = 0
    assistant_messages: int = 0
    tool_calls: int = 0
    tool_call_breakdown: dict = field(default_factory=dict)
    permission_prompts: int = 0
    bash_commands: int = 0
    file_writes: int = 0
    file_edits: int = 0
    web_searches: int = 0
    timeline: list = field(default_factory=list)  # (ts, kind, label) for charting
    # error/blocker hints
    error_msgs: int = 0


def parse_iso(ts):
    if not ts:
        return None
    # handle "Z" suffix
    return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))


def compute_active_time(timestamps, idle_gap_secs=120):
    """Sum of intervals between consecutive timestamps, excluding gaps > idle_gap_secs."""
    if len(timestamps) < 2:
        return 0
    timestamps = sorted(timestamps)
    total = 0
    for i in range(1, len(timestamps)):
        delta = (timestamps[i] - timestamps[i - 1]).total_seconds()
        if delta < idle_gap_secs:
            total += delta
    return total


# ===================== CC (Claude Code) =====================
def parse_cc(path):
    m = SessionMetrics(name="cc (Claude Code)", model="opus-4-7")
    timestamps = []
    user_prompt_ts = []
    with open(path) as f:
        for line in f:
            d = json.loads(line)
            ts = d.get("timestamp")
            t = d.get("type")
            if ts:
                pts = parse_iso(ts)
                timestamps.append(pts)
            if t == "user":
                msg = d.get("message", {})
                # user-typed content -> count as user message; tool result is also "user" type
                if "toolUseResult" not in d and msg.get("role") == "user":
                    content = msg.get("content")
                    # only count actual user prompts (text), not tool results
                    is_prompt = False
                    if isinstance(content, str):
                        is_prompt = True
                    elif isinstance(content, list):
                        for part in content:
                            if isinstance(part, dict) and part.get("type") == "text":
                                is_prompt = True
                                break
                    if is_prompt and d.get("isSidechain") is False:
                        m.user_messages += 1
                        if pts:
                            user_prompt_ts.append(pts)
                            m.timeline.append((pts, "user", "user prompt"))
            elif t == "assistant":
                m.assistant_messages += 1
                msg = d.get("message", {})
                content = msg.get("content")
                if isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "tool_use":
                            tname = part.get("name", "?")
                            m.tool_calls += 1
                            m.tool_call_breakdown[tname] = m.tool_call_breakdown.get(tname, 0) + 1
                            if tname == "Bash":
                                m.bash_commands += 1
                            if tname == "Write":
                                m.file_writes += 1
                            if tname in ("Edit", "MultiEdit"):
                                m.file_edits += 1
                            if tname in ("WebSearch", "WebFetch"):
                                m.web_searches += 1
                            if pts:
                                m.timeline.append((pts, "tool_use", tname))
            elif t == "permission-mode":
                # not a permission prompt, but indicates mode change
                pass

    if timestamps:
        m.first_ts = min(timestamps).isoformat()
        m.last_ts = max(timestamps).isoformat()
        m.wall_seconds = (max(timestamps) - min(timestamps)).total_seconds()
        m.active_seconds = compute_active_time(timestamps)
    return m


# ===================== CD (Codex) =====================
def parse_cd(path):
    m = SessionMetrics(name="cd (Codex)", model="gpt-5 (codex)")
    timestamps = []
    with open(path) as f:
        for line in f:
            d = json.loads(line)
            ts = d.get("timestamp")
            t = d.get("type")
            payload = d.get("payload", {}) or {}
            if ts:
                pts = parse_iso(ts)
                timestamps.append(pts)
            else:
                pts = None

            if t == "session_meta":
                model = payload.get("model_provider") or "openai"
                m.model = f"openai-codex ({model})"
            elif t == "response_item":
                ptype = payload.get("type")
                if ptype == "function_call":
                    m.tool_calls += 1
                    name = payload.get("name", "?")
                    m.tool_call_breakdown[name] = m.tool_call_breakdown.get(name, 0) + 1
                    if name in ("shell", "local_shell", "exec"):
                        m.bash_commands += 1
                    if name in ("apply_patch", "edit_file", "write_file"):
                        m.file_edits += 1
                    if pts:
                        m.timeline.append((pts, "tool_use", name))
                elif ptype == "custom_tool_call":
                    m.tool_calls += 1
                    name = payload.get("name", "custom")
                    m.tool_call_breakdown[name] = m.tool_call_breakdown.get(name, 0) + 1
                    if name in ("apply_patch",):
                        m.file_edits += 1
                    if pts:
                        m.timeline.append((pts, "tool_use", name))
                elif ptype == "web_search_call":
                    m.web_searches += 1
                    m.tool_calls += 1
                    m.tool_call_breakdown["web_search"] = m.tool_call_breakdown.get("web_search", 0) + 1
                    if pts:
                        m.timeline.append((pts, "tool_use", "web_search"))
                elif ptype == "message":
                    role = payload.get("role")
                    if role == "user":
                        # check it's a real user prompt vs system instructions
                        content = payload.get("content", [])
                        is_user_real = False
                        if isinstance(content, list):
                            for c in content:
                                if isinstance(c, dict) and c.get("type") == "input_text":
                                    text = c.get("text", "")
                                    # filter system reminders
                                    if not text.startswith("<user_instructions>") and "user-prompt" not in text:
                                        is_user_real = True
                        if is_user_real:
                            m.user_messages += 1
                            if pts:
                                m.timeline.append((pts, "user", "user prompt"))
                    elif role == "assistant":
                        m.assistant_messages += 1
            elif t == "event_msg":
                ptype = payload.get("type")
                if ptype == "user_message":
                    m.user_messages += 1
                    if pts:
                        m.timeline.append((pts, "user", "user prompt"))
                elif ptype == "agent_message":
                    pass  # already counted via response_item

    if timestamps:
        m.first_ts = min(timestamps).isoformat()
        m.last_ts = max(timestamps).isoformat()
        m.wall_seconds = (max(timestamps) - min(timestamps)).total_seconds()
        m.active_seconds = compute_active_time(timestamps)
    return m


# ===================== OC (OpenCode) =====================
def parse_oc(db_path, session_id):
    m = SessionMetrics(name="oc (OpenCode)")
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.execute("SELECT title FROM session WHERE id=?", (session_id,))
    row = cur.fetchone()
    if row:
        m.model = "opencode"

    cur.execute("SELECT id, time_created, time_updated, data FROM message WHERE session_id=? ORDER BY time_created", (session_id,))
    timestamps = []
    msg_count = 0
    for mid, tc, tu, data_s in cur:
        msg_count += 1
        try:
            data = json.loads(data_s)
        except:
            continue
        ts = datetime.datetime.fromtimestamp(tc / 1000.0, tz=datetime.timezone.utc)
        timestamps.append(ts)
        role = data.get("role")
        if role == "user":
            m.user_messages += 1
            m.timeline.append((ts, "user", "user prompt"))
        elif role == "assistant":
            m.assistant_messages += 1
            # detect model from providerID/modelID
            if not m.model or m.model == "opencode":
                prov = data.get("providerID", "")
                mod = data.get("modelID", "")
                if prov or mod:
                    m.model = f"{prov}/{mod}".strip("/")

    # parts include tool calls and results
    cur.execute("SELECT id, message_id, time_created, data FROM part WHERE session_id=? ORDER BY time_created", (session_id,))
    for pid, mid, tc, data_s in cur:
        try:
            data = json.loads(data_s)
        except:
            continue
        ts = datetime.datetime.fromtimestamp(tc / 1000.0, tz=datetime.timezone.utc)
        timestamps.append(ts)
        ptype = data.get("type")
        if ptype == "tool":
            m.tool_calls += 1
            tool = data.get("tool", "?")
            m.tool_call_breakdown[tool] = m.tool_call_breakdown.get(tool, 0) + 1
            if tool == "bash":
                m.bash_commands += 1
            if tool == "write":
                m.file_writes += 1
            if tool == "edit":
                m.file_edits += 1
            if tool in ("websearch", "webfetch"):
                m.web_searches += 1
            m.timeline.append((ts, "tool_use", tool))

    if timestamps:
        m.first_ts = min(timestamps).isoformat()
        m.last_ts = max(timestamps).isoformat()
        m.wall_seconds = (max(timestamps) - min(timestamps)).total_seconds()
        m.active_seconds = compute_active_time(timestamps)
    conn.close()
    return m


def main():
    cc = parse_cc(str(CC_LOG))
    cd = parse_cd(str(CD_LOG))
    oc = parse_oc(str(OC_DB), OC_SID)

    out = {}
    for m in (cc, cd, oc):
        d = asdict(m)
        # convert timeline timestamps to ISO
        d["timeline"] = [(ts.isoformat(), kind, label) for (ts, kind, label) in m.timeline]
        out[m.name.split()[0]] = d

    with open(HERE / "sessions.json", "w") as f:
        json.dump(out, f, indent=2, default=str)

    for m in (cc, cd, oc):
        print(f"\n=== {m.name} ===")
        print(f"Model: {m.model}")
        print(f"Wall time: {m.wall_seconds/60:.1f} min")
        print(f"Active time (excl >120s gaps): {m.active_seconds/60:.1f} min")
        print(f"User messages: {m.user_messages}")
        print(f"Assistant messages: {m.assistant_messages}")
        print(f"Tool calls: {m.tool_calls}")
        print(f"  Bash: {m.bash_commands}")
        print(f"  Write: {m.file_writes}")
        print(f"  Edit: {m.file_edits}")
        print(f"  Web: {m.web_searches}")
        print(f"  Top tool breakdown: {sorted(m.tool_call_breakdown.items(), key=lambda x:-x[1])[:6]}")


if __name__ == "__main__":
    main()
