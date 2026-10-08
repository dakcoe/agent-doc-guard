# agent-doc-guard

Stops agents from turning your AGENTS.md into a diary.

[![Claude Code plugin](https://img.shields.io/badge/Claude_Code-plugin-D97757)](#install)
[![Codex plugin](https://img.shields.io/badge/Codex-plugin-412991)](#install)
[![Version](https://img.shields.io/github/v/release/dakcoe/agent-doc-guard)](https://github.com/dakcoe/agent-doc-guard/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

[한국어](README.ko.md)

<table>
<tr><th>What the agent wrote into AGENTS.md</th><th>What it writes with agent-doc-guard</th></tr>
<tr>
<td><pre>- Tests failed on Oct 5 because the DB
  container was down. The user had to
  start it by hand. If you run tests right
  after a reboot, start the DB container
  first.</pre></td>
<td><pre>- Start the DB container before
  running tests.</pre></td>
</tr>
</table>

## Why

Agents re-read `AGENTS.md`, `CLAUDE.md` and skills on every task. Each time one gets feedback, it tends to write down what happened instead of the rule. The file grows, every future task pays for those tokens, and the agent starts fitting itself to one past event.

Google researchers measured this in [RRSI (2026)](https://arxiv.org/abs/2609.24972). When an agent's instructions were tuned over and over from feedback, scores rose on the tasks used for tuning and vanished on new ones. Screening each change for task-specific content changed that:

| | Tuned without screening | Tuned with screening |
|---|---|---|
| Gain on unseen tasks | +0.6 points | **+3.9 points** |
| Tokens per task | 3.80M | **2.42M (−36%)** |

<sub>RRSI Table 2, agentic workspace tasks. Unseen tasks: JobBench, GDPval, APEX-Agents.</sub>

agent-doc-guard applies that screen to your instruction files, at the moment an agent tries to edit them.

## Install

**Claude Code**

    /plugin marketplace add dakcoe/agent-doc-guard
    /plugin install agent-doc-guard@agent-doc-guard

<details>
<summary><b>Codex</b></summary>

    codex plugin marketplace add dakcoe/agent-doc-guard
    codex plugin add agent-doc-guard@agent-doc-guard

Codex runs plugin hooks only after you trust them: open Codex, go to the hooks list (`/hooks`) and trust the agent-doc-guard hook.

</details>

<details>
<summary><b>Updates</b></summary>

Third-party marketplaces do not update on their own. In Claude Code, turn on auto-update under `/plugin` → Marketplaces, or run `/plugin marketplace update agent-doc-guard`.

</details>

Needs `python3`. The check runs on the agent you are using: `claude -p` under Claude Code, `codex exec` under Codex.

## What it checks

- Edits to `AGENTS.md`, `CLAUDE.md`, `SKILL.md`, files under `.claude/`, and docs your AGENTS.md links to.
- New lines with dates, past incidents, one-off special cases or repeated rules are sent back to the agent with the reason.
- Shell commands are covered too: those files are compared before and after each command, and a change that fails the check is put back.
- Deleting lines is always allowed. English and Korean.

## Cleaning existing files

The hook stops new sediment. For files that already have it, the plugin ships a skill: run `/agent-doc-guard:doc-cleanup` in Claude Code, or ask Codex to use the `doc-cleanup` skill. It lists your instruction files, backs them up, and rewrites them one at a time, showing you the first one before going on.

## Settings

- `AGENT_DOC_GUARD_JUDGE=off`: pattern check only, no model call.
- `AGENT_DOC_GUARD_CLAUDE_MODEL`: judge model under Claude Code (default `haiku`).
- `AGENT_DOC_GUARD_CODEX_MODEL`: judge model under Codex (default `gpt-6-luna`).
- `AGENT_DOC_GUARD_STRICT=1`: refuse the edit when the judge cannot be reached (default: allow and warn).

## License

MIT
