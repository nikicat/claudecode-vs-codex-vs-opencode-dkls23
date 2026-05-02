#!/usr/bin/env python3
"""Build the final comparison HTML report.

Reads the same env-overridable session-log paths as parse_sessions.py:
  CC_LOG, CD_LOG, OC_DB, OC_SID. Outputs index.html and report-data.json
next to this script.
"""

import json
import os
import datetime as dt
import sqlite3
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


# ===================== Load session data ======================

def load_cc_timeline():
    events = []
    with open(CC_LOG) as f:
        for line in f:
            d = json.loads(line)
            ts = d.get('timestamp')
            if not ts:
                continue
            t = d.get('type')
            pts = dt.datetime.fromisoformat(ts.replace('Z', '+00:00'))
            label = ''
            kind = t
            if t == 'assistant':
                msg = d.get('message', {})
                content = msg.get('content')
                if isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and part.get('type') == 'tool_use':
                            label = part.get('name', '?')
                            kind = 'tool_use'
                            break
            elif t == 'user':
                if 'toolUseResult' not in d and not d.get('isSidechain'):
                    label = 'user prompt'
                    kind = 'user'
                else:
                    continue  # tool result
            else:
                continue
            events.append((pts, kind, label))
    return events


def load_cd_timeline():
    events = []
    with open(CD_LOG) as f:
        for line in f:
            d = json.loads(line)
            ts = d.get('timestamp')
            if not ts:
                continue
            pts = dt.datetime.fromisoformat(ts.replace('Z', '+00:00'))
            t = d.get('type')
            p = d.get('payload', {}) or {}
            if t == 'response_item':
                pt = p.get('type')
                if pt in ('function_call', 'custom_tool_call'):
                    name = p.get('name', '?')
                    events.append((pts, 'tool_use', name))
                elif pt == 'web_search_call':
                    events.append((pts, 'tool_use', 'web_search'))
            elif t == 'event_msg':
                pt = p.get('type')
                if pt == 'user_message':
                    events.append((pts, 'user', 'user prompt'))
                elif pt == 'agent_message':
                    pass
    # The very first user message comes through response_item:message; record it manually
    return events


def load_oc_timeline():
    events = []
    conn = sqlite3.connect(OC_DB)
    cur = conn.cursor()
    cur.execute("SELECT time_created, data FROM message WHERE session_id=? ORDER BY time_created",
                (OC_SID,))
    for tc, data_s in cur:
        ts = dt.datetime.fromtimestamp(tc / 1000.0, tz=dt.timezone.utc)
        try:
            data = json.loads(data_s)
        except:
            continue
        if data.get('role') == 'user':
            events.append((ts, 'user', 'user prompt'))
    cur.execute("SELECT time_created, data FROM part WHERE session_id=? ORDER BY time_created",
                (OC_SID,))
    for tc, data_s in cur:
        ts = dt.datetime.fromtimestamp(tc / 1000.0, tz=dt.timezone.utc)
        try:
            data = json.loads(data_s)
        except:
            continue
        if data.get('type') == 'tool':
            tool = data.get('tool', '?')
            events.append((ts, 'tool_use', tool))
    return events


def compute_metrics(events):
    if not events:
        return {}
    timestamps = [e[0] for e in events]
    timestamps.sort()
    wall = (timestamps[-1] - timestamps[0]).total_seconds()
    # active time excludes gaps > 120s
    active = 0
    for i in range(1, len(timestamps)):
        delta = (timestamps[i] - timestamps[i - 1]).total_seconds()
        if delta < 120:
            active += delta
    user = sum(1 for e in events if e[1] == 'user')
    tools = sum(1 for e in events if e[1] == 'tool_use')
    tool_breakdown = {}
    for e in events:
        if e[1] == 'tool_use':
            tool_breakdown[e[2]] = tool_breakdown.get(e[2], 0) + 1
    # bin into per-minute counts for chart
    start = timestamps[0]
    minute_counts = {}
    for e in events:
        m = int((e[0] - start).total_seconds() // 60)
        if e[1] == 'tool_use':
            minute_counts[m] = minute_counts.get(m, 0) + 1
    return {
        'wall_seconds': wall,
        'active_seconds': active,
        'user_prompts': user,
        'tool_calls': tools,
        'tool_breakdown': tool_breakdown,
        'first_ts': timestamps[0].isoformat(),
        'last_ts': timestamps[-1].isoformat(),
        'minute_counts': minute_counts,
        'start_epoch': timestamps[0].timestamp(),
        'end_epoch': timestamps[-1].timestamp(),
    }


cc_events = load_cc_timeline()
cd_events = load_cd_timeline()
oc_events = load_oc_timeline()

cc_m = compute_metrics(cc_events)
cd_m = compute_metrics(cd_events)
oc_m = compute_metrics(oc_events)


# ===================== Token / cost data ======================

cc_tokens = {'input': 16925, 'output': 486182, 'cache_read': 99453976, 'cost_usd': None}
cd_tokens = {'input': 16372713, 'output': 41357, 'cache_read': 15990912,
             'reasoning': 8005, 'cost_usd': None}
oc_tokens = {'input': 204645, 'output': 124880, 'cache_read': 60507904,
             'reasoning': 51938, 'cost_usd': 9.74}


# ===================== QA findings ======================

with open(HERE / 'qa-results.json') as f:
    qa = json.load(f)


# ===================== HTML render ======================

def fmt_dur(s):
    if s < 60:
        return f"{s:.0f}s"
    return f"{int(s // 60)}m {int(s % 60)}s"


def to_unix_ms(events, base_epoch):
    out = []
    for e in events:
        out.append((int((e[0].timestamp() - base_epoch) * 1000), e[1], e[2]))
    return out


# Common base: align all sessions to their own start (relative timeline)
def relative_minutes(events):
    if not events:
        return [], 0
    base = min(e[0] for e in events)
    rel = [(int((e[0] - base).total_seconds()), e[1], e[2]) for e in events]
    return rel, base


cc_rel, cc_base = relative_minutes(cc_events)
cd_rel, cd_base = relative_minutes(cd_events)
oc_rel, oc_base = relative_minutes(oc_events)

# data structure for charts
data = {
    'cc': {
        'name': 'cc · Claude Code (Opus 4.7)',
        'short': 'cc',
        'metrics': cc_m,
        'tokens': cc_tokens,
        'timeline_rel': cc_rel,  # seconds relative to start
        'color': '#7c4dff',
    },
    'cd': {
        'name': 'cd · Codex (GPT-5)',
        'short': 'cd',
        'metrics': cd_m,
        'tokens': cd_tokens,
        'timeline_rel': cd_rel,
        'color': '#00bfa5',
    },
    'oc': {
        'name': 'oc · OpenCode (DeepSeek v4-pro)',
        'short': 'oc',
        'metrics': oc_m,
        'tokens': oc_tokens,
        'timeline_rel': oc_rel,
        'color': '#ff6e40',
    },
}

# write JSON for the HTML to consume
with open(HERE / 'report-data.json', 'w') as f:
    json.dump(data, f, default=str)


# ============ Build HTML ============

html = '''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>dkls23ctl: Three-Agent Showdown</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  :root{
    --bg:#0d1117; --panel:#161b22; --border:#30363d; --fg:#e6edf3;
    --muted:#8b949e; --accent:#58a6ff; --green:#3fb950; --red:#f85149;
    --amber:#d29922; --cc:#7c4dff; --cd:#00bfa5; --oc:#ff6e40;
  }
  *{box-sizing:border-box}
  html,body{margin:0;padding:0;background:var(--bg);color:var(--fg);font:14px/1.55 -apple-system,BlinkMacSystemFont,"SF Pro",system-ui,Segoe UI,Roboto,sans-serif}
  a{color:var(--accent)}
  .wrap{max-width:1280px;margin:0 auto;padding:32px 24px 64px}
  h1{font-size:32px;margin:0 0 4px;letter-spacing:-0.02em}
  .sub{color:var(--muted);margin-bottom:24px}
  h2{margin-top:48px;font-size:22px;border-bottom:1px solid var(--border);padding-bottom:8px}
  h3{font-size:16px;margin:24px 0 8px;color:var(--accent)}
  .grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}
  .card{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:16px}
  .card h4{margin:0 0 8px;font-size:14px;color:var(--muted);text-transform:uppercase;letter-spacing:0.05em}
  .big{font-size:30px;font-weight:600;letter-spacing:-0.02em;line-height:1.1}
  .pill{display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:600;background:#21262d;border:1px solid var(--border);margin-right:6px}
  .pill.cc{background:rgba(124,77,255,.18);color:#bda6ff;border-color:rgba(124,77,255,.4)}
  .pill.cd{background:rgba(0,191,165,.18);color:#5fe2ce;border-color:rgba(0,191,165,.4)}
  .pill.oc{background:rgba(255,110,64,.18);color:#ffb087;border-color:rgba(255,110,64,.4)}
  table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--border);border-radius:10px;overflow:hidden}
  th,td{padding:8px 12px;text-align:left;border-bottom:1px solid var(--border);font-size:13px;vertical-align:top}
  th{background:#0d1117;color:var(--muted);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:0.05em}
  tr:last-child td{border-bottom:none}
  .pass{color:var(--green);font-weight:600}
  .fail{color:var(--red);font-weight:600}
  .partial{color:var(--amber);font-weight:600}
  .chart-box{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:14px;margin:14px 0}
  .chart-box canvas{max-width:100%}
  .chart-box.compact{max-width:680px;margin-left:auto;margin-right:auto}
  .chart-box.compact canvas{height:380px !important;max-height:380px}
  .chart-box.skinny canvas{height:320px !important;max-height:320px}
  .obs{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:16px 20px;margin:8px 0}
  .obs strong{color:var(--accent)}
  .obs h3{margin-top:0;color:var(--fg);font-size:15px}
  ul{margin:6px 0 12px 18px;padding:0}
  li{margin:4px 0}
  code{background:#21262d;padding:1px 6px;border-radius:4px;font-size:12px;font-family:"SF Mono",Menlo,Consolas,monospace}
  .hypo{border-left:3px solid var(--accent);padding-left:14px}
  .winner{color:var(--green)}
  .ok{color:var(--green)}
  .footer{color:var(--muted);font-size:12px;margin-top:48px;border-top:1px solid var(--border);padding-top:16px;text-align:center}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
  .swatch{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px;vertical-align:middle}
  .timeline{font-size:12px;color:var(--muted);font-family:"SF Mono",monospace}
</style>
</head>
<body>
<div class="wrap">

<h1>dkls23ctl · Opus 4.7 vs GPT-5 vs DeepSeek v4-pro</h1>
<div class="sub">
  Three frontier models — <span class="pill cc">Opus 4.7 <small>· Claude Code · x-high effort</small></span>
  <span class="pill cd">GPT-5 <small>· Codex · high effort</small></span>
  <span class="pill oc">DeepSeek v4-pro <small>· OpenCode · high (or max) effort</small></span>
  — were given the same prompt: build a t-of-n threshold ECDSA CLI on top of Silence Laboratories'
  <code>dkls23</code> with <code>iroh</code>/mDNS for peer discovery. Same machine, same hour, no other context.
  Here's how each fared.
</div>

<h2>Headline numbers</h2>
<div class="grid3">
  <div class="card">
    <h4>QA scenarios (out of 16)</h4>
    <div class="big"><span style="color:var(--cc)">Opus 4.7 &nbsp;12 / 4 / 0</span><br><span style="color:var(--cd)">GPT-5 &nbsp;14 / 2 / 0</span><br><span style="color:var(--oc)">DeepSeek &nbsp;6 / 2 / 8</span></div>
    <div class="sub">pass / partial / fail. Opus and GPT-5's "partial" entries are intentional rejections of unimplemented reshare modes; DeepSeek's are real failures.</div>
  </div>
  <div class="card">
    <h4>Active session time (excl. >2&nbsp;min idle)</h4>
    <div class="big"><span style="color:var(--cc)">Opus 4.7 &nbsp;65m</span> · <span style="color:var(--cd)">GPT-5 &nbsp;26m</span> · <span style="color:var(--oc)">DeepSeek &nbsp;95m</span></div>
    <div class="sub">GPT-5 was 2.5× faster than Opus, 3.6× faster than DeepSeek.</div>
  </div>
  <div class="card">
    <h4>Tool calls / user interventions</h4>
    <div class="big"><span style="color:var(--cc)">337 / 2</span> · <span style="color:var(--cd)">217 / 3</span> · <span style="color:var(--oc)">294 / 1</span></div>
    <div class="sub">Opus and GPT-5 needed almost no user input on the task itself; DeepSeek asked one question (the user disagreed). But see §4.10 for Opus's permission-prompt cost.</div>
  </div>
</div>

<h2>1. QA — does it actually work?</h2>
<div class="chart-box compact"><canvas id="chartScenarios"></canvas></div>

<h3>Detail per scenario</h3>
<table>
  <thead><tr><th>Scenario</th><th>Opus 4.7</th><th>GPT-5</th><th>DeepSeek v4-pro</th></tr></thead>
  <tbody id="scenarioTable"></tbody>
</table>

<h2>2. Architecture &amp; code quality</h2>
<div class="grid3">
  <div class="card">
    <h4>Opus 4.7 <small style="color:var(--muted);font-weight:400">· Claude Code</small></h4>
    <ul>
      <li>9 source files, clean module split: <code>cli</code>, <code>commands/</code>, <code>discovery</code>, <code>transport</code>, <code>keyshare</code>, <code>singleton</code>.</li>
      <li>Uses dkls23's official <code>SimpleMessageRelay</code> + a thin <code>InterceptRelay</code> wrapper. The "obvious" idiomatic integration.</li>
      <li>Real mDNS discovery scoped via <code>blake3(tool_id|key_id)[..6]</code> — peers from different keys never see each other.</li>
      <li>Hello handshake with peer-id collision check.</li>
      <li>Library + binary split, tests use the library half.</li>
      <li>2 cargo tests + 4 shell QA scripts (incl. <code>run_all.sh</code> orchestrator).</li>
      <li>Detailed <code>README.md</code>.</li>
    </ul>
  </div>
  <div class="card">
    <h4>GPT-5 <small style="color:var(--muted);font-weight:400">· Codex</small></h4>
    <ul>
      <li><strong>Single 1254-line file</strong> (<code>main.rs</code>) — extreme density, no module split.</li>
      <li>Custom <code>IrohRelay</code> built directly on <code>Sink</code>+<code>Stream</code>; bypasses dkls23's <code>SimpleMessageRelay</code>. Riskier, but works.</li>
      <li>Carries discovery metadata (peer_id, party_id, pubkey, encryption pubkey) inline as <code>UserData</code> on each mDNS record.</li>
      <li>Implements <strong>all four reshare transitions</strong> incl. (1,1)→(t,n), (t,n)→(1,1) via <code>key_export</code>, and committee-size changes.</li>
      <li>2 cargo tests; <strong>no shell scripts</strong>.</li>
      <li>No README.</li>
    </ul>
  </div>
  <div class="card">
    <h4>DeepSeek v4-pro <small style="color:var(--muted);font-weight:400">· OpenCode</small></h4>
    <ul>
      <li>4 source files (~940 lines).</li>
      <li>Uses <code>dkls23-secp256k1</code> (a <em>different</em> upstream than the spec's <code>silence-laboratories/dkls23</code>) — phase-1/2/3/4 message API.</li>
      <li><strong>File-system based discovery</strong> via <code>/tmp/dkls23ctl/&lt;key&gt;/&lt;peer&gt;.json</code> — spec violation (mDNS was required).</li>
      <li>iroh endpoint exists but addresses are advertised by writing to disk and polling. The user explicitly called this out mid-session; the model never fixed it.</li>
      <li>Reshare cannot change t/n (only refresh) and cannot bring in fresh peers.</li>
      <li><code>main.rs</code> contains a quietly broken normalisation: <code>if n == 1 || t == 1 { t = 1; n = 1; }</code> silently overrides user-supplied params.</li>
      <li>Visible bug: <code>protocol.rs</code> sends sign-phase-1 messages twice (loop duplicated).</li>
      <li><strong>0 cargo tests</strong>; 3 shell scripts (qa_test.sh admits its own race conditions).</li>
      <li>~495 KB per share file (vs. ~few KB for the other two) — the wrong serialisation level.</li>
    </ul>
  </div>
</div>

<h2>3. Time, iterations, and where each model got stuck</h2>
<div class="chart-box skinny"><canvas id="chartActivity"></canvas></div>

<div class="grid3">
  <div class="card">
    <h4>Tool-call breakdown</h4>
    <canvas id="chartTools" height="240"></canvas>
  </div>
  <div class="card">
    <h4>Wall vs active time</h4>
    <canvas id="chartTime" height="240"></canvas>
  </div>
  <div class="card">
    <h4>Tokens used</h4>
    <canvas id="chartTokens" height="240"></canvas>
  </div>
</div>

<h3>Idle gaps (>30 s) — where each model waited</h3>
<table>
  <thead><tr><th>Model</th><th># gaps &gt; 30 s</th><th>Longest gap</th><th>Note</th></tr></thead>
  <tbody>
    <tr><td><span class="pill cc">Opus 4.7</span></td><td>37</td><td>~80 min (13:56→15:17 UTC)</td><td>Same global pause (user lunch / break) seen across all three sessions.</td></tr>
    <tr><td><span class="pill cd">GPT-5</span></td><td>8</td><td>~81 min (13:55→15:17 UTC)</td><td>Fewest mid-session waits — the model rarely paused on its own.</td></tr>
    <tr><td><span class="pill oc">DeepSeek v4-pro</span></td><td>69</td><td>~81 min (13:55→15:17 UTC)</td><td>Many small mid-session pauses: long generations, repeated retries.</td></tr>
  </tbody>
</table>

<h2>4. Key observations</h2>

<div class="obs">
  <h3>4.1  All three picked the same iroh primitive — but only two used the dkls23 relay correctly</h3>
  <p>Opus 4.7 and GPT-5 both used <code>iroh::address_lookup::MdnsAddressLookup</code> and a real ALPN-based QUIC connection between peers. Opus plugged dkls23's own <code>SimpleMessageRelay</code> into iroh via a sink interceptor (the "official" path). GPT-5 built a from-scratch relay, which is more code but enables features Opus skipped.</p>
  <p>DeepSeek v4-pro bound an iroh endpoint, but its <code>wait_for_peers()</code> loop reads <code>/tmp/dkls23ctl/&lt;key&gt;/&lt;peer&gt;.json</code> files instead of using mDNS. The user pushed back on this mid-session — the model acknowledged but did not fix it. The iroh dial path uses the loopback IP and port written to those files, defeating the whole point of mDNS / LAN discovery.</p>
</div>

<div class="obs">
  <h3>4.2  GPT-5 is the most feature-complete</h3>
  <p>Only GPT-5 implements the full reshare matrix:</p>
  <ul>
    <li>(1,1) → (t,n): <code>key_import::ecdsa_secret_shares</code> + <code>key_refresh</code> ✅</li>
    <li>(t,n) → (t,n): <code>key_refresh</code> ✅</li>
    <li>(t,n) → (t',n) same set: <code>quorum_change</code> ✅</li>
    <li>(t,n) → (t',n') different size: <code>quorum_change</code> with mixed old/new committees ✅ (verified: (2,3)→(3,4) works end-to-end)</li>
    <li>(t,n) → (1,1): <code>key_export</code> with x25519 export key ✅ (verified: receiver becomes singleton)</li>
  </ul>
  <p>Opus 4.7 explicitly errors on the last two; DeepSeek cannot do any reshare beyond a same-params refresh, and even that hung in our test.</p>
</div>

<div class="obs">
  <h3>4.3  Speed vs polish</h3>
  <ul>
    <li><strong>GPT-5</strong> finished the work in 26 minutes of active time — 2.5× faster than Opus 4.7, 3.6× faster than DeepSeek. Almost no wasted iterations: 22 patches, 2 web searches, 0 explicit retries.</li>
    <li><strong>Opus 4.7</strong> spent more time but produced the most polished artefact (clean module split, real README, comprehensive QA scripts, integration test that exercises actual storage round-trip). Lots of WebFetch-driven research up front (11 fetches into github/docs.rs/iroh docs).</li>
    <li><strong>DeepSeek v4-pro</strong> did 95 minutes of active work for the worst result. 34 webfetches (most of any), 4 of which errored. Long sequences of "Reshare: 3 peers running refresh." with no progress.</li>
  </ul>
</div>

<div class="obs">
  <h3>4.4  DeepSeek explicitly asked the user a question — and ignored the answer</h3>
  <p>DeepSeek invoked OpenCode's <code>question</code> tool once:</p>
  <p style="margin:8px 14px;color:var(--muted)"><em>"would you accept a simpler networking approach (TCP streams with file-based discovery) that's more reliable, or do you specifically need iroh QUIC for this tool?"</em></p>
  <p>The user's reply was emphatic: <em>"Initial request states clearly that this tool should work on localhost AND LAN, so file-based discovery is a critical flaw. iroh and related libs provide all the required functionality, you just didn't manage to use it correctly."</em> The shipped code still uses file-based discovery. Hypothesis: DeepSeek v4-pro repeatedly failed to figure out iroh's mDNS API, and the model treated the rebuke as guidance to "keep trying" rather than as a hard constraint.</p>
</div>

<div class="obs">
  <h3>4.5  Pubkey serialisation diverges from the spec for DeepSeek</h3>
  <p>The spec calls for showing pubkey on stdout. Opus 4.7 and GPT-5 print compressed SEC1 (33 bytes / 66 hex chars). DeepSeek prints raw uncompressed coordinates without the <code>04</code> prefix (64 bytes / 128 hex chars). This is technically a public key, but every downstream tool will choke. It's a leaky abstraction over <code>dkls23-secp256k1</code>'s API surface.</p>
</div>

<div class="obs">
  <h3>4.6  Library choice mattered enormously</h3>
  <p>Spec said <code>github.com/silence-laboratories/dkls23</code>. Opus 4.7 and GPT-5 picked <code>sl-dkls23</code> on crates.io — Silence Labs' v1 beta of the same code. DeepSeek picked <code>dkls23-secp256k1</code> — a different SL crate, multi-curve, with a much chattier phase-by-phase API. This forced DeepSeek to manually wire eight separate message types per DKG round, which it did adequately, then again for sign, then attempted reshare and got stuck. Opus and GPT-5 handed messages to dkls23's protocol task and let the library do the choreography.</p>
</div>

<div class="obs">
  <h3>4.7  Why is DeepSeek v4-pro's output the worst?</h3>
  <p>Several reinforcing factors. Probable root causes:</p>
  <ul>
    <li><strong>Lower base model capability</strong> — DeepSeek v4-pro is below Opus 4.7 / GPT-5 on long-horizon engineering tasks, especially when it has to navigate an unfamiliar crypto API.</li>
    <li><strong>Wrong library at the start</strong> — picking <code>dkls23-secp256k1</code> required hand-assembling 4 DKG phases × N messages each. The error budget compounded.</li>
    <li><strong>Discovery sidestep</strong> — when iroh's mDNS proved hard, the model fell back to <code>/tmp</code> rendezvous and never recovered, even after the user objected.</li>
    <li><strong>No internal QA loop</strong> — qa_test.sh is the only real test, and it explicitly tolerates the broken multi-peer paths ("This is expected with the current file-based discovery"). Marking your own bug as a feature is a yellow flag.</li>
  </ul>
</div>

<div class="obs">
  <h3>4.8  Why is GPT-5 the fastest?</h3>
  <ul>
    <li>The session log shows almost no wasted edits — 22 <code>apply_patch</code> calls produced 1254 lines that build and pass the QA matrix on first try.</li>
    <li>The Codex harness's <code>exec_command</code>+<code>write_stdin</code> pair (193 calls together) is GPT-5's primary tool — it can keep a long-running shell, drive cargo + tail logs without re-spawning. Claude Code instead fires 211 individual one-shot <code>Bash</code> calls.</li>
    <li>Single-file architecture is faster to write (no module wiring) but harder to read — the trade-off favoured speed here.</li>
    <li>GPT-5 used 16 M cached tokens vs. only 41 k uncached output. Almost everything benefits from KV-cache reuse, which keeps inference cheap and steady.</li>
  </ul>
</div>

<div class="obs">
  <h3>4.9  What Opus 4.7 did better than GPT-5</h3>
  <ul>
    <li><strong>Module structure</strong> — anyone joining the project can find <code>commands/sign.rs</code> in seconds; GPT-5's monolithic <code>main.rs</code> requires scrolling.</li>
    <li><strong>Tests &amp; scripts</strong> — 4 shell scripts + integration test that round-trips the storage layer. GPT-5 has 2 in-memory tests and nothing for the real binary.</li>
    <li><strong>README + comments</strong> — Opus explains the transport bridge and the reshare matrix in prose; GPT-5's only documentation is the type signatures.</li>
    <li><strong>mDNS service-name privacy</strong> — Opus hashes <code>tool_id|key_id</code> so peers running other key sessions don't show up in your discovery.</li>
  </ul>
</div>

<div class="obs">
  <h3>4.10  The harness tax: Claude Code is the most annoying to operate</h3>
  <p>While Opus 4.7 itself rarely needed user input on the actual task, the <em>Claude Code harness</em> demanded the most permission-prompt clicks of any of the three. After the session ended, <code>cc/.claude/settings.local.json</code> contained <strong>30 persisted "always allow" entries</strong> — and that's only the requests where the user picked the persistent option. The session log shows 50 <code>permissionMode</code> transitions through <code>acceptEdits</code> on top of those.</p>
  <p>Worse, many of the persistent entries are uselessly narrow — they won't match a similar future command:</p>
  <ul>
    <li><code>Bash(/home/&lt;user&gt;/src/&lt;project&gt;/target/debug/dkls23ctl verify *)</code> — absolute path baked in; useless if the dir ever moves.</li>
    <li><code>Bash(echo "exits: $?")</code>, <code>Bash(echo "exit=$?")</code>, <code>Bash(wait)</code> — exact-string match on a one-off shell snippet.</li>
    <li><code>Bash(rm -rf .secrets)</code>, <code>Bash(pkill -f 'reshare --key-id sk1')</code> — pinned to the test's literal key-id, won't apply to any other run.</li>
    <li><code>Bash(python3 -c '…literal 100-char snippet…')</code> — granted with the script body baked in.</li>
    <li><code>Bash(DKLS23CTL_BIN=$(readlink -f ./target/debug/dkls23ctl) bash scripts/test_reshare.sh)</code> — a single command line frozen as a permission rule.</li>
  </ul>
  <p>And many tool calls offered no persistence option at all (or only a one-shot accept), so the user kept re-clicking through similar variants. By contrast, Codex defers to its sandbox profile (one decision at session start), and OpenCode's permission table for this session has zero persisted rules. <strong>Net effect: Opus 4.7 needed the user to mash the keyboard tens of times that the other two harnesses didn't ask about at all</strong> — and the resulting allow-list is mostly cruft that won't help a future session.</p>
</div>

<h2>5. Strengths &amp; weaknesses summary</h2>
<table>
  <thead><tr><th>Aspect</th><th>Opus 4.7 <small>· Claude Code</small></th><th>GPT-5 <small>· Codex</small></th><th>DeepSeek v4-pro <small>· OpenCode</small></th></tr></thead>
  <tbody>
    <tr><td>Spec compliance (mDNS)</td><td class="pass">Yes</td><td class="pass">Yes</td><td class="fail">No (filesystem)</td></tr>
    <tr><td>Reshare completeness</td><td class="partial">Partial (no n-change, no export)</td><td class="pass">Full matrix</td><td class="fail">Refresh only, hangs</td></tr>
    <tr><td>Code structure</td><td class="pass">Modular, idiomatic</td><td class="partial">Monolithic but tight</td><td class="partial">Modular but dense network.rs</td></tr>
    <tr><td>Testing</td><td class="pass">2 tests + 4 scripts + run_all</td><td class="partial">2 tests, no scripts</td><td class="fail">0 tests, scripts admit failures</td></tr>
    <tr><td>Documentation</td><td class="pass">README + module docs</td><td class="fail">None</td><td class="fail">None</td></tr>
    <tr><td>Time efficiency</td><td>65 min active</td><td class="winner">26 min active</td><td>95 min active</td></tr>
    <tr><td>Bugs</td><td class="pass">None observed</td><td class="pass">None observed</td><td class="fail">Silent param override; duplicated send loop; reshare hangs</td></tr>
    <tr><td>API surface choice</td><td class="pass">SimpleMessageRelay (canonical)</td><td class="partial">Custom relay (works, more code)</td><td class="fail">Wrong upstream lib</td></tr>
    <tr><td>Operator UX (output)</td><td class="pass">Tagged stdout (PUBKEY/SHARE/SIGNATURE)</td><td class="partial">Single hex line, no tag</td><td class="partial">Single hex line, wrong format</td></tr>
    <tr><td>Operator UX (permissions)</td><td class="fail">~30 persisted grants, many over-narrow; lots of mid-session prompts</td><td class="pass">Sandbox profile, one decision at start</td><td class="pass">Zero persisted rules in session</td></tr>
  </tbody>
</table>

<h2>6. Verdict</h2>
<div class="hypo">
  <p><strong class="winner">GPT-5 (Codex, high effort)</strong> is the most spec-complete and the fastest to produce a working tool. If the only criterion is "does it pass the QA matrix", GPT-5 wins.</p>
  <p><strong>Opus 4.7 (Claude Code, x-high effort)</strong> wins on engineering quality — readable code, real tests, scripts, README, idiomatic library use — at the cost of skipping two reshare paths and spending more time. Best for handing off to another engineer. Operator caveat: the Claude Code harness's permission UX makes this the most interaction-heavy option, and most of its "always allow" rules end up too narrow to reuse.</p>
  <p><strong>DeepSeek v4-pro (OpenCode, high/max effort)</strong> failed to deliver a tool that meets the spec. The combination of a weaker base model, an unfortunate library pick, and a stubborn refusal to fix the discovery layer after explicit user feedback makes this the clear loser. The lesson: when a model gets stuck on a constraint it doesn't understand, escalating to the user works only if the model is then willing to reverse course.</p>
</div>

<div class="footer">Generated 2026-05-02 from <code>~/.claude-personal</code>, <code>~/.codex/sessions</code>, and <code>~/.local/share/opencode/opencode.db</code>. Source data and scripts in this directory.</div>

</div>

<script>
const REPORT_DATA = ''' + json.dumps(data, default=str) + ''';

const COLORS = {cc: '#7c4dff', cd: '#00bfa5', oc: '#ff6e40'};
const SCENARIOS = ''' + json.dumps(qa['scenarios']) + ''';

// Scenario table
const tbody = document.getElementById('scenarioTable');
function classify(s){
  if(!s) return '';
  if(/^PASS/i.test(s)) return 'pass';
  if(/^FAIL/i.test(s)) return 'fail';
  return 'partial';
}
SCENARIOS.forEach(sc => {
  const tr = document.createElement('tr');
  const td0 = document.createElement('td'); td0.textContent = sc.name; tr.appendChild(td0);
  ['cc','cd','oc'].forEach(k => {
    const td = document.createElement('td');
    const v = sc[k] || '—';
    td.innerHTML = `<span class="${classify(v)}">${v.replace(/^(PASS|FAIL)\\s*/i,'$1 ')}</span>`;
    tr.appendChild(td);
  });
  tbody.appendChild(tr);
});

// Scenario pass/fail bar chart — count per agent
function score(s){
  if(!s) return 0;
  if(/^PASS/i.test(s)) return 1;
  if(/^FAIL/i.test(s)) return -1;
  return 0.5; // partial
}
const passCounts = {cc:0, cd:0, oc:0};
const failCounts = {cc:0, cd:0, oc:0};
const partialCounts = {cc:0, cd:0, oc:0};
SCENARIOS.forEach(sc => {
  ['cc','cd','oc'].forEach(k => {
    const v = sc[k] || '';
    if(/^PASS/i.test(v)) passCounts[k]++;
    else if(/^FAIL/i.test(v)) failCounts[k]++;
    else partialCounts[k]++;
  });
});

new Chart(document.getElementById('chartScenarios'), {
  type: 'bar',
  data: {
    labels: ['Opus 4.7', 'GPT-5', 'DeepSeek v4-pro'],
    datasets: [
      {label:'Passed', data:['cc','cd','oc'].map(k=>passCounts[k]), backgroundColor:'#3fb950'},
      {label:'Partial / N-A', data:['cc','cd','oc'].map(k=>partialCounts[k]), backgroundColor:'#d29922'},
      {label:'Failed', data:['cc','cd','oc'].map(k=>failCounts[k]), backgroundColor:'#f85149'},
    ]
  },
  options: {
    responsive: true,
    maintainAspectRatio: false,
    plugins:{legend:{position:'top',labels:{color:'#e6edf3'}}, title:{display:true, text:'QA scenarios per model', color:'#e6edf3'}},
    scales:{x:{ticks:{color:'#e6edf3'},grid:{color:'#30363d'},stacked:true}, y:{ticks:{color:'#e6edf3'},grid:{color:'#30363d'},stacked:true,title:{display:true,text:'scenarios',color:'#8b949e'}}},
  }
});

// Activity timeline — bin by minute since session start
function activityDataset(key, label){
  const minutes = REPORT_DATA[key].metrics.minute_counts;
  const max = Math.max(...Object.keys(minutes).map(Number).concat([0]));
  const arr = [];
  for(let i=0;i<=max;i++) arr.push(minutes[i] || 0);
  return {label, data: arr, borderColor: COLORS[key], backgroundColor: COLORS[key]+'33', tension: 0.25, pointRadius: 0, fill: true};
}
const dsCC = activityDataset('cc','Opus 4.7');
const dsCD = activityDataset('cd','GPT-5');
const dsOC = activityDataset('oc','DeepSeek v4-pro');
const maxLen = Math.max(dsCC.data.length, dsCD.data.length, dsOC.data.length);
const labels = Array.from({length: maxLen}, (_,i) => i + ' min');
[dsCC, dsCD, dsOC].forEach(ds => { while(ds.data.length < maxLen) ds.data.push(0); });

new Chart(document.getElementById('chartActivity'), {
  type: 'line',
  data: {labels, datasets: [dsCC, dsCD, dsOC]},
  options: {
    responsive: true,
    maintainAspectRatio: false,
    plugins:{legend:{position:'top', labels:{color:'#e6edf3'}}, title:{display:true,text:'Tool calls per minute (relative to each session start; gaps include shared user-pause)', color:'#e6edf3'}},
    scales:{x:{ticks:{color:'#8b949e', maxTicksLimit:20},grid:{color:'#30363d'}, title:{display:true,text:'minutes since session start',color:'#8b949e'}}, y:{ticks:{color:'#e6edf3'},grid:{color:'#30363d'},title:{display:true,text:'tool calls',color:'#8b949e'}}}
  }
});

// Tool breakdown (top 6 per agent, normalized)
function topTools(key, n=8){
  const breakdown = REPORT_DATA[key].metrics.tool_breakdown;
  return Object.entries(breakdown).sort((a,b)=>b[1]-a[1]).slice(0, n);
}
const allKeys = new Set();
['cc','cd','oc'].forEach(k => topTools(k).forEach(([t]) => allKeys.add(t)));
const allKeysArr = Array.from(allKeys);
const MODEL_LABEL = {cc:'Opus 4.7', cd:'GPT-5', oc:'DeepSeek v4-pro'};
const toolDatasets = ['cc','cd','oc'].map(k => ({
  label: MODEL_LABEL[k],
  data: allKeysArr.map(t => REPORT_DATA[k].metrics.tool_breakdown[t] || 0),
  backgroundColor: COLORS[k],
}));
new Chart(document.getElementById('chartTools'), {
  type: 'bar',
  data: {labels: allKeysArr, datasets: toolDatasets},
  options: {
    indexAxis: 'y',
    plugins:{legend:{position:'bottom', labels:{color:'#e6edf3'}}, title:{display:true,text:'tool calls by tool',color:'#e6edf3'}},
    scales:{x:{ticks:{color:'#e6edf3'},grid:{color:'#30363d'}}, y:{ticks:{color:'#e6edf3'},grid:{color:'#30363d'}}}
  }
});

// Wall vs active time
new Chart(document.getElementById('chartTime'), {
  type: 'bar',
  data: {
    labels: ['Opus 4.7','GPT-5','DeepSeek v4-pro'],
    datasets: [
      {label:'Wall time (min)', data: ['cc','cd','oc'].map(k => Math.round(REPORT_DATA[k].metrics.wall_seconds/60)), backgroundColor:'#21262d', borderColor:'#8b949e', borderWidth:1},
      {label:'Active time (min)', data: ['cc','cd','oc'].map(k => Math.round(REPORT_DATA[k].metrics.active_seconds/60)), backgroundColor: ['cc','cd','oc'].map(k => COLORS[k])},
    ]
  },
  options: {
    plugins:{legend:{position:'bottom', labels:{color:'#e6edf3'}}, title:{display:true,text:'wall vs active time (min)', color:'#e6edf3'}},
    scales:{x:{ticks:{color:'#e6edf3'},grid:{color:'#30363d'}}, y:{ticks:{color:'#e6edf3'},grid:{color:'#30363d'}}}
  }
});

// Tokens chart
new Chart(document.getElementById('chartTokens'), {
  type: 'bar',
  data: {
    labels: ['input', 'output', 'cache_read'],
    datasets: ['cc','cd','oc'].map(k => ({
      label: MODEL_LABEL[k],
      data: [REPORT_DATA[k].tokens.input, REPORT_DATA[k].tokens.output, REPORT_DATA[k].tokens.cache_read],
      backgroundColor: COLORS[k],
    }))
  },
  options: {
    plugins:{legend:{position:'bottom', labels:{color:'#e6edf3'}}, title:{display:true,text:'tokens consumed (log scale)', color:'#e6edf3'}},
    scales:{x:{ticks:{color:'#e6edf3'},grid:{color:'#30363d'}}, y:{type:'logarithmic', ticks:{color:'#e6edf3'},grid:{color:'#30363d'}}}
  }
});
</script>
</body></html>
'''

with open(HERE / 'index.html', 'w') as f:
    f.write(html)

print(f"Wrote {HERE / 'index.html'}")
print("CC scenarios pass/partial/fail:")
print("CD scenarios pass/partial/fail:")
print("OC scenarios pass/partial/fail:")
