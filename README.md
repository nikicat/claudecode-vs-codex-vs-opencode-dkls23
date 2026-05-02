# Opus 4.7 vs GPT-5 vs DeepSeek v4-pro — building dkls23ctl

> **Live report:** https://nikicat.github.io/claudecode-vs-codex-vs-opencode-dkls23/

A side-by-side evaluation of three frontier coding models, each running in its
own agent harness, given the same brief — build a Rust CLI (`dkls23ctl`) for
t-of-n threshold ECDSA signing on top of Silence Laboratories'
[`dkls23`](https://github.com/silence-laboratories/dkls23) with
[`iroh`](https://github.com/n0-computer/iroh) / mDNS for peer discovery.

| Model | Harness | Reasoning effort | Session log location |
|-------|---------|------------------|----------------------|
| **Anthropic Opus 4.7** (1M ctx) | [Claude Code](https://claude.com/claude-code) | x-high | `~/.claude-personal/projects/<project>/<session>.jsonl` |
| **OpenAI GPT-5**                | [Codex](https://github.com/openai/codex)     | high   | `~/.codex/sessions/<date>/rollout-<id>.jsonl` |
| **DeepSeek v4-pro**             | [OpenCode](https://github.com/sst/opencode)  | high (or max) | `~/.local/share/opencode/opencode.db` (sqlite) |

Internally the analysis scripts use short keys `cc`, `cd`, `oc` for the three
runs (matching the directory layout of the original test). The HTML report
surfaces model names; the JSON files keep the short keys.

The brief is reproduced in [`tz.txt`](tz.txt) (verbatim user prompt). The three
implementations themselves (`cc/`, `cd/`, `oc/` source trees) are **not**
included in this repo — only the analysis artifacts.

## Index of files

| File | Purpose |
|------|---------|
| `index.html`        | **Final HTML report** (served via GitHub Pages). Embeds Chart.js. |
| `report-data.json`  | Data the HTML consumes — timelines, tool breakdown, tokens, etc. |
| `qa-results.json`   | Pass / fail / partial per QA scenario, machine-readable. |
| `sessions.json`     | First-pass session metrics from `parse_sessions.py`. |
| `tz.txt`            | The original brief given to all three models (Russian for "tech spec"). |
| `build_report.py`   | Rebuilds `index.html` + `report-data.json` from the session logs. |
| `parse_sessions.py` | Per-model counts: tool calls, user prompts, wall vs. active time. |
| `find_gaps.py`      | Idle-gap detector — distinguishes "model stuck" from "user away". |

## How to re-run

The scripts read three session logs whose paths must be supplied via env vars
(no built-in defaults — the original log file paths contained the author's
local username and were stripped before publishing):

```bash
# Adjust to your local paths
export CC_LOG=~/.claude/projects/<project>/<session>.jsonl
export CD_LOG=~/.codex/sessions/<YYYY/MM/DD>/rollout-<id>.jsonl
export OC_DB=~/.local/share/opencode/opencode.db   # has a sane default
export OC_SID=ses_<your_session_id>

python3 parse_sessions.py     # writes sessions.json
python3 find_gaps.py          # prints idle gaps
python3 build_report.py       # writes index.html + report-data.json
```

No external Python deps — stdlib only. The HTML pulls Chart.js from a CDN.

## Top-level findings

- **GPT-5 (Codex, high effort)** is the most spec-complete — the only model to
  ship the full reshare matrix: (1,1)→(t,n), (t,n)→(1,1) export via x25519,
  and committee-size changes. Did it in **26 minutes of active session time**
  (vs. Opus's 65, DeepSeek's 95). Single 1254-line `main.rs` — dense but it
  works.
- **Opus 4.7 (Claude Code, x-high effort)** delivered the cleanest engineering:
  modular source tree, real README, integration test, four shell QA scripts.
  Skipped the two reshare modes that need committee splits/merges and explicitly
  errored on them rather than mis-implementing. *Operator caveat*: the Claude
  Code harness asked for tool-permission approval far more often than the other
  two, and most of the "always allow" entries it persisted are too narrow
  (absolute paths, exact-string commands) to apply to similar future invocations.
- **DeepSeek v4-pro (OpenCode, high/max effort)** failed to meet the spec. It
  picked the wrong upstream library (`dkls23-secp256k1` instead of the canonical
  `sl-dkls23`), regressed to filesystem-based discovery instead of the required
  mDNS, and even after the user pushed back —
  *"file-based discovery is a critical flaw … iroh provides all the required
  functionality, you just didn't manage to use it correctly"* — shipped code
  that still uses `/tmp/dkls23ctl/<key>/<peer>.json`. Reshare hangs. No tests.

See [`index.html`](index.html) for the full breakdown with charts.
