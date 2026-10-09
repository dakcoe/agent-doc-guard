---
name: doc-cleanup
description: Clean existing instruction files (AGENTS.md, CLAUDE.md, skills, the docs they name) down to rules and facts.
disable-model-invocation: true
---

# Doc cleanup

Remove **sediment** from instruction files until each holds only rules and facts. Sediment is provenance (dates, who said what), anecdotes, case-specific examples, special cases from one event, and the same rule said twice.

A **fact** is something the agent cannot find by looking: a location, a command and its flags, an account limit, a measured number, a gotcha. Facts stay, even when they arrived inside an anecdote; keep the fact and drop the story around it.

## 1. Inventory

List every instruction file in scope (the current project unless the user names more):

- `AGENTS.md`, `CLAUDE.md`, `GEMINI.md`, `SKILL.md`, `MEMORY.md`, files under `~/.claude`, `~/.codex`, `.claude/`, `.agents/`, and `.md` files a guide names by path.

Mark each one **rewrite**, **clean**, or **skip**:

- **skip** change logs, research reports whose purpose is recorded findings, and skills installed from someone else's source (resolve symlinks; a target outside the user's own folders is someone else's).
- **rewrite** when it carries sediment.

Done when every file in scope has a mark, and every skip has a reason. Show the list to the user.

## 2. Back up

Put every **rewrite** file into one dated archive outside any instruction location (for example `tar czf ~/Documents/doc-cleanup-backup-<date>.tgz <files>`).

Done when `tar tzf` lists every rewrite file.

## 3. Rewrite, one file at a time

For each **rewrite** file:

- Turn each feedback record into the general rule it implies, written so it also fits the next, unrelated task.
- Fold sections that log one task's lessons into the topic sections they belong to.
- Keep each meaning in one place; delete restatements, background with no rule, and closing summaries.
- Move a dated history section ("what changed on which day") into `CHANGELOG.md` next to the file and leave one line pointing to it. Change logs are meant to hold dates.
- Move a long list of collected cases (words that fail, known errors) into a data file such as a `.tsv`, and point to it from the file with one line saying when to look it up and when to add to it.
- Keep templates that show the form of good output (a command format, a pair contrasting a wording to avoid with the wording to use); drop only the story attached to them.
- Keep the file's language, voice, headings that still serve, tables, commands and paths exactly.
- Leave the frontmatter fields `originSessionId` and `modified` in Claude Code memory files as they are; the harness rewrites them on every save.
- Write with the edit tool (Claude Code: Edit/Write, Codex: apply_patch), so agent-doc-guard reviews the new lines. Follow its notes. When a note would delete a fact, keep the fact and reword it as a plain statement.

Rewrite the first file, show it to the user with a short list of what was dropped, and wait for their go-ahead before the rest. Their correction calibrates the remaining files.

Done for a file when the guard accepts the write and every fact from the backup copy is either still present or listed as deliberately dropped.

## 4. Report

For each file: lines before → after, facts deliberately dropped, and anything left for the user to decide. Give the backup path. In a public repository, leave committing to the user.

Done when the report, checked against the step 1 list, misses no file and every rewrite file has its line.
