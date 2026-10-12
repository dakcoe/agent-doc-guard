#!/usr/bin/env python3
"""PreToolUse/PostToolUse hook for Claude Code and Codex.

Agents re-read instruction files such as AGENTS.md, CLAUDE.md and skills on every task.
When an agent edits one of them after a single piece of feedback, it tends to write down the
incident instead of the rule: dates, who said what, what went wrong last time, a special case
for that one situation. Those lines stay in every future prompt and narrow the next agent's
judgement. This hook shows the agent what it just added to such a file, next to the rules for it.
It never refuses an edit or puts a file back.

File edits (Claude Code: Edit/Write/MultiEdit, Codex: apply_patch): before the edit, the text of each
  instruction file it adds lines to is kept. After the edit has run, the file is compared with that text
  and the agent is told:
  - the first time in a session that it adds to a file: the rules and the lines it added;
  - whenever any added line needs fixing: those lines and how to fix them.
  Added lines are checked for dates by pattern, then a small model judges them from the diff, with the
  user's latest messages from the session transcript in view; a line the user wrote out is never flagged.
  The model runs on the agent that called the hook: `claude -p` under Claude Code, `codex exec` under
  Codex. With the model off, or when it cannot be reached, word patterns for feedback history and past
  incidents stand in for it. Edits that only remove lines get no note.
Bash: when a command visibly writes to an instruction file, the agent is told afterwards to review what
  it wrote and to make such changes with its edit tool. Copies, moves and deletions are left alone.

Environment:
  AGENT_DOC_GUARD_JUDGE          set to "off" to use the patterns only (default: on)
  AGENT_DOC_GUARD_CLAUDE_MODEL   judge model under Claude Code (default: haiku)
  AGENT_DOC_GUARD_CODEX_MODEL    judge model under Codex (default: gpt-6-luna)
  AGENT_DOC_GUARD_STATE_DIR      where files already shown and edits in progress are kept
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HOME = Path.home()
GUARD_ENV = "AGENT_DOC_GUARD_ACTIVE"
CLAUDE_MODEL = os.environ.get("AGENT_DOC_GUARD_CLAUDE_MODEL", "haiku")
CODEX_MODEL = os.environ.get("AGENT_DOC_GUARD_CODEX_MODEL", "gpt-6-luna")
JUDGE_ON = os.environ.get("AGENT_DOC_GUARD_JUDGE", "on").lower() != "off"
STATE_DIR = Path(os.environ.get("AGENT_DOC_GUARD_STATE_DIR") or Path(tempfile.gettempdir()) / "agent-doc-guard")
# a session's record of files already shown is dropped once it has been idle this long
STATE_TTL = 24 * 3600
# added lines quoted back to the agent on the first edit
SHOWN_LINES = 30
NAMES = {"AGENTS.md", "CLAUDE.md", "GEMINI.md", "SKILL.md", "MEMORY.md"}
AGENT_HOMES = [HOME / ".claude", HOME / ".codex"]
NOT_REFERENCE = [HOME / ".claude" / "plans"]
# Change logs are meant to hold dates and history
LOG_NAME = re.compile(r"^(?:CHANGELOG|CHANGES|HISTORY|RELEASE[-_]?NOTES)\b.*\.md$", re.IGNORECASE)
MAX_DOC_CHARS = 24_000
# diff text per judge call; a bigger rewrite is judged in several calls, split at hunk boundaries
MAX_DIFF_CHARS = 5_000
# the user's latest messages shown to the judge
USER_MESSAGES = 3
USER_CHARS = 1_500
TRANSCRIPT_TAIL = 2_000_000
HANGUL = re.compile(r"[가-힣]")

DATE_PATTERNS = [
    (r"(?<!\d)20\d{2}-\d{1,2}-\d{1,2}(?!\d)", "date"),
    (r"\d{1,2}월 ?\d{1,2}일", "date"),
    (r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.? \d{1,2}(?:st|nd|rd|th)?\b", "date"),
]
# These words also turn up in general rules, so they are only used when the judge model cannot be reached
WORD_PATTERNS = [
    # who said what
    (r"지적(?:을)? ?받|지적했|지적한", "feedback history"),
    (r"라고 (?:했|말했|하더라)|다고 (?:했|말했)", "quoting the user"),
    (r"\b(?:was|were|got) (?:told|called out)\b|\bpointed out\b", "feedback history"),
    (r"\b(?:the )?user (?:said|complained|mentioned|noted)\b", "quoting the user"),
    # what happened before
    (r"했다가|넣었다가|썼다가|만들었다가", "past incident"),
    (r"(?:한|된|난|생긴) 적(?:이)? 있", "past incident"),
    (r"\blast time\b|\bin a previous (?:session|task|run)\b", "past incident"),
    # sections that log one task's lessons
    (r"에서 더한 것|때 더한 것|에서 배운 것", "per-task log"),
    (r"\blessons? learned\b|\b(?:added|learned) (?:from|after|during) (?:the|this|that) ", "per-task log"),
]

JUDGE_PROMPT = """You review a proposed edit to an instruction file that AI agents read. Give it your full effort.

Many agents re-read this file on every task. Anything unnecessary pollutes every future prompt, and
anything fitted to one past event narrows the judgement of agents working on unrelated tasks.
The file must stay short, clear and simple.

[DIFF] is a unified diff of the edit. Judge only the lines that start with `+`: they are added or changed by this edit.
When a `+` line is a changed version of a nearby `-` line, judge only the words the edit added or changed; the words it kept were already in the file.
Lines that start with `-` are removed by this edit and are not in the file afterwards; never treat them as something a `+` line duplicates.
Lines that start with a space are unchanged context; do not report them.
[FILE AFTER EDIT] is the whole file once the edit is applied, without diff markers.
[USER MESSAGES] are the user's latest messages in the session that made the edit.
A `+` line that carries content the user stated, or told the agent to keep, is fine.
A request to remember or record something ("remember this", "add this to AGENTS.md") does not make the agent's
own account of what happened fine: that still has to become a general rule.

The test for every `+` line: would it still make sense, and still help, when an agent reads this file for an
unfamiliar task that has nothing to do with the one that prompted this edit, within the kind of work this file
covers? A line that only makes sense in light of the task that prompted the edit fails this test.
Rules that hold for the whole kind of work the file covers pass it, even when that work is narrow.

Report `+` lines that fail the test, under the kind that fits best:
0. task-fitted: fails the test but fits none of the kinds below (names, values, triggers or steps that only
   matter for the task that prompted the edit).
1. provenance: dates, who pointed out what and when, which task/video/file something happened in, quotes of the user.
2. anecdote: a description of something that actually happened ("A and B overlapped before"), including when it is attached as the reason for a rule.
3. example: an example that records a specific case (what happened, in which task). Templates that show the form of good output are fine: a command or path format, or a pair contrasting a wording to avoid with the wording to use.
4. narrow-condition: a special-case branch that came from one situation ("if X, do Y") where one general rule would do.
5. duplicate: a rule that a different line of [FILE AFTER EDIT] already states. The `+` line itself also appears in [FILE AFTER EDIT]; that copy is not a duplicate.
6. unnecessary: a line whose removal would not change what the agent does: background with no rule, a summary, or an instruction the agent already follows by default.

Facts the agent cannot know on its own (locations, commands, paths, environment, measured numbers and limits, known causes and their fixes, how the current system differs from its stated design), general rules, and a short reason that explains why a rule exists are fine.
If nothing is wrong, set ok to true and problems to an empty list.
For each problem, put the first 40 characters of the `+` line, without the `+`, in `line` and one sentence on how to fix it in `fix`.
Write `fix` in the language named in [FIX LANGUAGE].
"""

JUDGE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "ok": {"type": "boolean"},
        "problems": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "line": {"type": "string"},
                    "kind": {"type": "string", "enum": ["task-fitted", "provenance", "anecdote", "example", "narrow-condition", "duplicate", "unnecessary"]},
                    "fix": {"type": "string"},
                },
                "required": ["line", "kind", "fix"],
            },
        },
    },
    "required": ["ok", "problems"],
}

MESSAGES = {
    "en": {
        "rules": (
            "{name} is an instruction file that agents re-read on every task, so every line in it costs tokens and "
            "steers unrelated work. Check each line this edit added:\n"
            "- Would it still make sense, and still help, on an unfamiliar task unrelated to the one you are doing now?\n"
            "- It states a general rule, or a fact an agent cannot know on its own (a location, command, path, environment detail, limit).\n"
            "- No dates, who said what, or an account of what happened. Turn the lesson from one event into a general rule.\n"
            "- No special case that came from one situation, no example that records a specific case, "
            "no rule the file already states, no line whose removal would change nothing.\n"
            "- Text the user wrote out, or a file brought in from elsewhere, stays as it is.\n"
            "- If the date or the story behind a rule is worth keeping, write it in CHANGELOG.md next to this file instead.\n\n"
            "Lines this edit added:\n{lines}\n\n"
            "If a line breaks these rules, rewrite or drop it with {tool}."
        ),
        "problems": (
            "Lines this edit added to {name} that need fixing. Rewrite each as a general rule or drop it with {tool}. "
            "A date or story worth keeping goes in CHANGELOG.md next to the file:\n{problems}"
        ),
        "more": "… and {n} more lines",
        "bash": (
            "{paths}: instruction files that agents re-read on every task. If this command added lines to them, "
            "check those lines against the rules for such files (general rules and facts only; no dates, history "
            "or one-off special cases) and fix them with {tool}. Make future changes to these files with {tool}."
        ),
        "judge_failed": "agent-doc-guard: could not reach the judge model for {name}; only the pattern check was applied.",
    },
    "ko": {
        "rules": (
            "{name} 은 에이전트가 작업마다 다시 읽는 지침 문서입니다. 한 줄 한 줄이 매 작업의 토큰이 되고 상관없는 작업의 판단에도 영향을 줍니다. "
            "이번 수정이 넣은 줄을 하나씩 확인하세요.\n"
            "- 지금 하는 작업과 상관없는 낯선 작업에서도 맞고 도움이 되는지.\n"
            "- 일반 규칙이거나, 에이전트가 스스로 알 수 없는 사실(위치, 명령, 경로, 환경, 한계값)인지.\n"
            "- 날짜, 누가 무엇을 말했는지, 있었던 일의 경위가 없는지. 한 번 있었던 일에서 얻은 교훈은 일반 규칙으로 바꿉니다.\n"
            "- 한 상황에서 나온 좁은 조건, 특정 사례를 기록한 예시, 문서에 이미 있는 규칙의 반복, 지워도 할 일이 바뀌지 않는 문장이 없는지.\n"
            "- 사용자가 직접 쓴 문장이나 다른 곳에서 가져온 파일은 그대로 둡니다.\n"
            "- 규칙의 날짜나 경위를 남겨야 하면 이 파일 대신 같은 폴더의 CHANGELOG.md 에 적습니다.\n\n"
            "이번 수정이 넣은 줄:\n{lines}\n\n"
            "규칙에 어긋나는 줄이 있으면 {tool} 로 다시 쓰거나 빼세요."
        ),
        "problems": (
            "이번 수정이 {name} 에 넣은 줄 가운데 고칠 줄입니다. {tool} 로 일반 규칙 한 줄로 다시 쓰거나 빼세요. "
            "남겨야 할 날짜나 경위는 같은 폴더의 CHANGELOG.md 에 적습니다.\n{problems}"
        ),
        "more": "… 외 {n}줄",
        "bash": (
            "{paths}: 에이전트가 작업마다 다시 읽는 지침 문서입니다. 이 명령이 여기에 줄을 넣었다면 그 줄이 "
            "지침 문서 규칙(일반 규칙과 사실만, 날짜·경위·한 번 있었던 일에서 나온 좁은 조건은 넣지 않음)에 맞는지 확인하고 "
            "{tool} 로 고치세요. 앞으로 이 문서는 {tool} 로 고치세요."
        ),
        "judge_failed": "agent-doc-guard: {name} 판정 모델 호출에 실패해 패턴 검사만 적용했습니다.",
    },
}
EDIT_TOOL = {"claude": {"en": "the Edit tool", "ko": "Edit 도구"}, "codex": {"en": "apply_patch", "ko": "apply_patch"}}


def language(text: str) -> str:
    return "ko" if HANGUL.search(text) else "en"


def emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False))
    sys.exit(0)


def under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def agent_folder(folder: Path) -> bool:
    """A .claude, .agents or .codex folder at the root of a project. A copy of one elsewhere, such as
    a backup, has no project files next to it."""
    if folder.name not in (".claude", ".agents", ".codex"):
        return False
    root = folder.parent
    return root == HOME or any((root / n).exists() for n in (".git", "AGENTS.md", "CLAUDE.md", "GEMINI.md"))


def is_reference(path: Path) -> bool:
    """Instruction files: known names, anything under ~/.claude or ~/.codex, .claude/ or .agents/
    folders, and any .md that an AGENTS.md or CLAUDE.md in a parent folder names."""
    if path.suffix.lower() != ".md" or LOG_NAME.match(path.name):
        return False
    if any(under(path, root) for root in NOT_REFERENCE):
        return False
    if path.name in NAMES:
        return True
    if any(under(path, root) for root in AGENT_HOMES):
        return True
    if any(agent_folder(folder) for folder in path.parents):
        return True
    for folder in path.parents:
        for name in ("AGENTS.md", "CLAUDE.md"):
            guide = folder / name
            if guide == path or not guide.is_file():
                continue
            try:
                text = guide.read_text(errors="ignore")
            except OSError:
                continue
            rel = str(path.relative_to(folder))
            # a bare file name only counts for files next to the guide; deeper files must be named by path
            if rel in text and (os.sep in rel or path.parent == folder):
                return True
        if folder in (HOME, Path(path.anchor)):
            break
    return False


def resolve(token: str, cwd: str) -> Path:
    p = Path(os.path.expanduser(token.strip("'\"")))
    return (p if p.is_absolute() else Path(cwd) / p).resolve()


def read(path: Path) -> str:
    return path.read_text(errors="ignore") if path.is_file() else ""


Change = "tuple[Path, str, str]"  # file, text before the edit, text after the edit


def claude_changes(tool: str, inp: dict, cwd: str) -> list[Change]:
    path = resolve(inp.get("file_path", ""), cwd)
    before = read(path)
    if tool == "Write":
        return [(path, before, inp.get("content", ""))]
    edits = inp.get("edits") if tool == "MultiEdit" else [inp]
    after = before
    for e in edits or []:
        old, new = e.get("old_string", ""), e.get("new_string", "")
        after = after.replace(old, new) if e.get("replace_all") else after.replace(old, new, 1)
    return [(path, before, after)]


PATCH_FILE = re.compile(r"^\*\*\* (Add|Update|Delete) File: (.+)$")


def find_block(lines: list[str], block: list[str], start: int) -> int:
    """Index where `block` occurs in `lines` at or after `start`, comparing like apply_patch does:
    exactly, then ignoring trailing spaces, then ignoring surrounding spaces. -1 if absent."""
    for norm in (lambda s: s, str.rstrip, str.strip):
        want = [norm(b) for b in block]
        for i in range(start, len(lines) - len(block) + 1):
            if [norm(x) for x in lines[i:i + len(block)]] == want:
                return i
    return -1


def apply_hunks(text: str, hunks: list[list[str]]) -> str | None:
    """`text` with apply_patch hunks applied, or None if a hunk does not match the file."""
    lines = text.splitlines()
    pos = 0
    for hunk in hunks:
        header, body = hunk[0], hunk[1:]
        anchor = header[2:].strip()
        if anchor:
            i = find_block(lines, [anchor], pos)
            if i < 0:
                return None
            pos = i + 1
        old = [l[1:] for l in body if l[:1] in (" ", "-")]
        new = [l[1:] for l in body if l[:1] in (" ", "+")]
        if not old:
            # a hunk with nothing to match adds its lines at the end of the file
            lines += new
            pos = len(lines)
            continue
        i = find_block(lines, old, pos)
        if i < 0:
            return None
        lines[i:i + len(old)] = new
        pos = i + len(new)
    return "\n".join(lines) + ("\n" if lines else "")


def codex_changes(patch: str, cwd: str) -> list[Change]:
    """Files an apply_patch adds or updates, with their text before and after the patch. Deleting a
    file adds nothing. A hunk that does not match the file is applied approximately (its removed lines
    dropped where found, its added lines appended); apply_patch itself would reject such a patch."""
    files: dict[Path, tuple[Path | None, list[list[str]]]] = {}  # target: (file read for "before", hunks)
    current = None
    for line in patch.splitlines():
        m = PATCH_FILE.match(line)
        if m:
            path = resolve(m.group(2).strip(), cwd)
            current = None if m.group(1) == "Delete" else path
            if current is not None:
                files[current] = (path if m.group(1) == "Update" else None, [["@@"]])
            continue
        if line.startswith("*** Move to: ") and current is not None:
            source, hunks = files.pop(current)
            current = resolve(line[len("*** Move to: "):].strip(), cwd)
            files[current] = (source, hunks)
            continue
        if current is None or line.startswith("***"):
            continue
        hunks = files[current][1]
        if line.startswith("@@"):
            hunks.append([line])
        elif line[:1] in (" ", "+", "-") or line == "":
            hunks[-1].append(line or " ")
    changes = []
    for target, (source, hunks) in files.items():
        hunks = [h for h in hunks if len(h) > 1]
        before = read(source) if source else ""
        after = apply_hunks(before, hunks)
        if after is None:
            kept = before.splitlines()
            for h in hunks:
                for l in h[1:]:
                    if l.startswith("-") and l[1:] in kept:
                        kept.remove(l[1:])
            kept += [l[1:] for h in hunks for l in h[1:] if l.startswith("+")]
            after = "\n".join(kept) + "\n"
        changes.append((target, before, after))
    return changes


def pattern_problems(added: list[tuple[str, list[tuple[int, int]]]], patterns: list[tuple[str, str]]) -> list[str]:
    """One entry per added line with a pattern match that takes in at least one new word, naming the match.
    The whole line is searched, so a match made of a kept word and a new one ("10월" + "5일") still counts."""
    found = []
    for line, spans in added:
        for pattern, kind in patterns:
            m = next((m for m in re.finditer(pattern, line, re.IGNORECASE)
                      if any(m.start() < end and start < m.end() for start, end in spans)), None)
            if m:
                found.append(f"- [{kind}] \"{m.group(0)}\": {line.strip()[:80]}")
                break
    return found


def ask_claude(message: str, env: dict) -> dict:
    r = subprocess.run(
        ["claude", "-p", "--model", CLAUDE_MODEL, "--setting-sources", "", "--tools", "",
         "--no-session-persistence", "--system-prompt", JUDGE_PROMPT,
         "--output-format", "json", "--json-schema", json.dumps(JUDGE_SCHEMA)],
        input=message, capture_output=True, text=True, timeout=90, env=env, cwd=str(HOME),
    )
    return json.loads(r.stdout)["structured_output"]


def ask_codex(message: str, env: dict) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        schema = Path(tmp) / "schema.json"
        answer = Path(tmp) / "answer.json"
        schema.write_text(json.dumps(JUDGE_SCHEMA))
        subprocess.run(
            ["codex", "exec", "-m", CODEX_MODEL, "--skip-git-repo-check", "--sandbox", "read-only",
             "--ephemeral", "--output-schema", str(schema), "-o", str(answer),
             JUDGE_PROMPT + "\n\n" + message],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120, env=env, cwd=tmp,
        )
        return json.loads(answer.read_text())


def diff_hunks(before: str, after: str) -> list[list[str]]:
    """The unified diff of an edit, as hunks of lines (each starting with its @@ header)."""
    hunks: list[list[str]] = []
    for line in difflib.unified_diff(before.splitlines(), after.splitlines(), n=3, lineterm=""):
        if line.startswith("@@"):
            hunks.append([line])
        elif hunks:
            hunks[-1].append(line)
    return hunks


def added_lines(hunks: list[list[str]]) -> list[str]:
    return [l[1:] for h in hunks for l in h[1:] if l.startswith("+") and l[1:].strip()]


def new_words(hunks: list[list[str]]) -> list[tuple[str, list[tuple[int, int]]]]:
    """Each added line with the character spans of the words it brings in. A `+` line that rewrites a
    `-` line next to it is compared with that line word by word, so the words it keeps are not new."""
    out = []
    for h in hunks:
        removed: list[str] = []
        for l in h[1:]:
            if l.startswith("-"):
                removed.append(l[1:])
                continue
            if not l.startswith("+"):
                removed = []
                continue
            line = l[1:]
            if not line.strip():
                continue
            found = list(re.finditer(r"\S+", line))
            words = [w.group(0) for w in found]
            best = max(removed, key=lambda r: difflib.SequenceMatcher(None, r.split(), words).ratio(), default=None)
            matcher = difflib.SequenceMatcher(None, best.split() if best else [], words)
            if best is None or matcher.ratio() < 0.5:
                out.append((line, [(0, len(line))]))
                continue
            spans = [(found[j1].start(), found[j2 - 1].end())
                     for op, _, _, j1, j2 in matcher.get_opcodes() if op in ("insert", "replace")]
            if spans:
                out.append((line, spans))
    return out


def batches(hunks: list[list[str]]) -> list[list[list[str]]]:
    """Hunks in batches small enough for one judge call. A hunk too big for one call is cut into pieces."""
    pieces = []
    for h in hunks:
        piece, size = [h[0]], 0
        for line in h[1:]:
            if size + len(line) > MAX_DIFF_CHARS and len(piece) > 1:
                pieces.append(piece)
                piece, size = [h[0]], 0
            piece.append(line)
            size += len(line) + 1
        pieces.append(piece)
    # a piece that only removes lines gives the judge nothing to judge
    pieces = [p for p in pieces if any(l.startswith("+") for l in p[1:])]
    out, size = [[]], 0
    for piece in pieces:
        n = sum(len(l) + 1 for l in piece)
        if out[-1] and size + n > MAX_DIFF_CHARS:
            out.append([])
            size = 0
        out[-1].append(piece)
        size += n
    return out


# list bullets, quote marks, heading marks and numbering at the start of a line
LINE_MARKS = re.compile(r"^(?:[+\-*>#]+|\d+[.)])\s*")


def squash(text: str) -> str:
    """A line with spaces collapsed and its leading diff and Markdown marks removed."""
    text = " ".join(text.split())
    while LINE_MARKS.match(text) and LINE_MARKS.sub("", text, 1) != text:
        text = LINE_MARKS.sub("", text, 1)
    return text


def user_messages(transcript: str | None) -> list[str]:
    """The user's latest messages from a Claude Code or Codex session transcript, newest last.
    Text the host adds in the user's place (reminders, injected AGENTS.md, command output) starts
    with a tag or heading and is skipped."""
    if not transcript:
        return []
    try:
        with open(transcript, "rb") as f:
            f.seek(max(0, os.path.getsize(transcript) - TRANSCRIPT_TAIL))
            rows = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []
    out = []
    for row in rows:
        try:
            d = json.loads(row)
        except ValueError:
            continue
        if not isinstance(d, dict):
            continue
        payload = d.get("payload") or {}
        if d.get("type") == "user" and not d.get("isMeta"):
            content = (d.get("message") or {}).get("content")  # Claude Code
        elif d.get("type") == "response_item" and payload.get("type") == "message" and payload.get("role") == "user":
            content = payload.get("content")  # Codex
        else:
            continue
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        for item in content or []:
            text = (item.get("text") or "").strip() if isinstance(item, dict) and item.get("type") in ("text", "input_text") else ""
            if text and not text.startswith(("<", "# AGENTS.md instructions")):
                out.append(text[:USER_CHARS])
    return out[-USER_MESSAGES:]


def dictated(line: str, said: list[str]) -> bool:
    """Whether the user wrote this line out in one of their messages."""
    text = squash(line)
    return len(text) >= 8 and any(text in squash(m) for m in said)


def on_added_line(problem: dict, lines: list[str]) -> bool:
    """Whether the judge's `line` points at one of the added lines: equal or the start of one once
    spaces and leading marks are normalized. A problem pointing elsewhere is about a line this edit
    did not touch."""
    quoted = squash(problem.get("line", "")).rstrip(".…").strip()
    if not quoted:
        return False
    return any(squash(l).startswith(quoted) for l in lines)


def judge_once(host: str, path: Path, after: str, batch: list[list[str]], lang: str, said: list[str]) -> list[str] | None:
    diff = "\n".join(l for h in batch for l in h)
    message = (
        f"[FILE] {path}\n\n[FIX LANGUAGE] {'Korean' if lang == 'ko' else 'English'}\n\n"
        f"[DIFF]\n{diff}\n\n"
        f"[FILE AFTER EDIT]\n{after[:MAX_DOC_CHARS]}\n\n"
        f"[USER MESSAGES]\n{chr(10).join('---' + chr(10) + m for m in said) or '(none)'}"
    )
    lines = [l for l in added_lines(batch) if not dictated(l, said)]
    env = {**os.environ, GUARD_ENV: "1"}
    ask = ask_codex if host == "codex" else ask_claude
    for _ in range(2):
        try:
            data = ask(message, env)
            problems = data["problems"]
        except Exception:
            continue
        return [f"- [{p['kind']}] {p['line'][:80]} → {p['fix']}" for p in problems if on_added_line(p, lines)]
    return None


def judge(host: str, path: Path, after: str, hunks: list[list[str]], lang: str, said: list[str]) -> list[str] | None:
    """Problems found by the judge model; [] if none, None if any batch could not be judged.
    A large rewrite is judged in batches, several at once."""
    groups = batches(hunks)
    with ThreadPoolExecutor(max_workers=min(8, len(groups))) as pool:
        results = list(pool.map(lambda b: judge_once(host, path, after, b, lang, said), groups))
    problems = [p for r in results if r for p in r]
    if problems:
        return problems
    return None if any(r is None for r in results) else []


# A write from code run inside a shell command (python -c, node -e, an interpreter heredoc)
WRITE_IN_CODE = re.compile(r"\.write\(|write_text|writeFileSync|appendFileSync|open\([^)]*['\"][wa]")
REDIRECTS = {">", ">>", ">|", "&>", "&>>"}
SEPARATORS = {";", "&&", "||", "|", "|&", "&", ";;", "(", ")"}
PREFIXES = {"sudo", "command", "env", "builtin", "exec", "nohup", "time"}
MD_TOKEN = re.compile(r"[~\w./가-힣()-]*\.md\b")
# Heredoc bodies fed to a command (a commit message, a here-string) are input text, not paths, and
# are skipped. Bodies fed to an interpreter (python3 - <<EOF) are code and stay in the check.
HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?\n\s*\2\s*(?:\n|$)", re.S)
INTERPRETER = re.compile(r"\b(?:python[\d.]*|node|deno|bun|ruby|perl|php|sh|bash|zsh|osascript)\b[^\n|;&]*$")


def strip_heredocs(command: str) -> str:
    def body(m: re.Match) -> str:
        line_start = command.rfind("\n", 0, m.start()) + 1
        if INTERPRETER.search(command[line_start:m.start()]):
            return m.group(0)
        return m.group(0).split("\n", 1)[0] + "\n"
    return HEREDOC.sub(body, command)


def simple_commands(command: str, cwd: str) -> list[tuple[list[str], str, str]]:
    """The simple commands in a shell command, as (words, folder they run in, source line). A `cd`
    changes the folder for the commands after it."""
    out = []
    for line in strip_heredocs(command).splitlines():
        lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        try:
            tokens = list(lexer)
        except ValueError:
            tokens = line.split()
        words: list[str] = []
        for t in tokens + [";"]:
            if t not in SEPARATORS:
                words.append(t)
                continue
            while words and (words[0] in PREFIXES or re.match(r"^\w+=", words[0])):
                words.pop(0)
            if words and words[0] == "cd":
                arg = words[1] if len(words) > 1 else "~"
                if arg != "-":
                    cwd = str(resolve(arg, cwd))
            elif words:
                out.append((words, cwd, line))
            words = []
    return out


def write_targets(words: list[str], cwd: str, line: str) -> list[Path]:
    """Files one simple command writes text into, as far as its words show: redirect targets, tee
    files, sed -i / perl -i files, and .md paths in code that writes. Copies, moves and deletions bring
    in or drop whole files and are left alone."""
    targets, args = [], []
    skip = False
    for i, w in enumerate(words):
        if skip:
            skip = False
        elif w in REDIRECTS:
            if i + 1 < len(words):
                targets.append(words[i + 1])
            skip = True
        elif w in ("<", ">&", "<&", "<<", "<<<", "<<-"):
            skip = True
        else:
            args.append(w)
    if WRITE_IN_CODE.search(line):
        # code is not shell words; take the .md paths from the line as written
        targets += MD_TOKEN.findall(line)
    if args:
        name = os.path.basename(args[0])
        files = [a for a in args[1:] if not a.startswith("-")]
        if name == "tee":
            targets += files
        elif name in ("sed", "gsed", "perl") and any(re.match(r"^-[A-Za-z]*i|^--in-place", a) for a in args[1:]):
            targets += [a for a in files if a.endswith(".md")]
    return [resolve(t, cwd) for t in targets]


def check_bash(host: str, command: str, cwd: str) -> None:
    """After a command that visibly wrote to instruction files: ask the agent to review what it wrote."""
    targets = [p for words, folder, line in simple_commands(command, cwd) for p in write_targets(words, folder, line)]
    paths = list(dict.fromkeys(str(p) for p in targets if is_reference(p)))
    if not paths:
        return
    lang = language(command)
    note = MESSAGES[lang]["bash"].format(paths=", ".join(paths), tool=EDIT_TOOL[host][lang])
    emit({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": note}})


# --- File edits: record before, warn after -------------------------------------------------------

def session_folder(data: dict) -> Path:
    """Where this session keeps the files already shown to the agent and the edits in progress.
    Old sessions are dropped."""
    key = data.get("session_id") or data.get("transcript_path") or data.get("cwd") or "session"
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", key):
        key = hashlib.sha256(key.encode()).hexdigest()[:32]
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    for old in STATE_DIR.iterdir():
        try:
            if old.is_file():
                old.unlink()  # a command snapshot left by version 0.1
            elif old.stat().st_mtime < time.time() - STATE_TTL:
                for f in old.iterdir():
                    f.unlink()
                old.rmdir()
        except OSError:
            continue
    folder = STATE_DIR / key
    folder.mkdir(exist_ok=True)
    return folder


def shown(folder: Path, path: Path) -> bool:
    """Whether the rules for `path` were already shown in this session; records that they now are.
    One marker file per path, so hooks for edits made in parallel do not overwrite each other."""
    marker = folder / ("shown-" + hashlib.sha256(str(path).encode()).hexdigest()[:32])
    if marker.exists():
        return True
    marker.touch()
    os.utime(folder)
    return False


# notices for the user (not the agent) gathered while checking one edit
NOTICES: list[str] = []


def pending_path(folder: Path, data: dict) -> Path:
    """Where the files an edit is about to change are kept until the edit has run. Both hook events
    of one tool call get the same tool_use_id, or failing that the same tool input."""
    key = data.get("tool_use_id") or json.dumps([data.get("tool_name"), data.get("tool_input")], sort_keys=True)
    return folder / ("pending-" + hashlib.sha256(str(key).encode()).hexdigest()[:32] + ".json")


def problems(host: str, path: Path, before: str, after: str, said: list[str]) -> list[str]:
    """Lines an edit added that break the rules: dates by pattern, then the judge's findings. When the
    judge is off or cannot be reached, the word patterns stand in for it."""
    hunks = diff_hunks(before, after)
    added = [(line, spans) for line, spans in new_words(hunks) if not dictated(line, said)]
    found = pattern_problems(added, DATE_PATTERNS)
    judged = judge(host, path, after, hunks, language(after), said) if JUDGE_ON else None
    if judged is None:
        if JUDGE_ON:
            NOTICES.append(MESSAGES[language(after)]["judge_failed"].format(name=path.name))
        judged = pattern_problems(added, WORD_PATTERNS)
    return found + [p for p in judged if p not in found]


def warning(host: str, path: Path, before: str, after: str, said: list[str], first: bool) -> str:
    """What to tell the agent after an edit added lines to `path`: the rules and the added lines the
    first time in a session, and the lines that need fixing whenever there are any."""
    lines = added_lines(diff_hunks(before, after))
    lang = language(after + "\n".join(lines))
    msg = MESSAGES[lang]
    tool = EDIT_TOOL[host][lang]
    parts = []
    if first:
        quoted = "\n".join(f"+ {l}" for l in lines[:SHOWN_LINES])
        if len(lines) > SHOWN_LINES:
            quoted += "\n" + msg["more"].format(n=len(lines) - SHOWN_LINES)
        parts.append(msg["rules"].format(name=path.name, lines=quoted, tool=tool))
    found = problems(host, path, before, after, said)
    if found:
        parts.append(msg["problems"].format(name=path.name, problems="\n".join(found), tool=tool))
    return "\n\n".join(parts)


def before_edit(data: dict, changes: list[Change]) -> None:
    """Keep the text of the instruction files this edit adds lines to, for the check after it."""
    files = {str(p): b for p, b, a in changes if is_reference(p) and added_lines(diff_hunks(b, a))}
    if files:
        pending_path(session_folder(data), data).write_text(json.dumps(files))


def after_edit(host: str, data: dict) -> None:
    """Compare each recorded file with what the edit left and tell the agent what to look at.
    Nothing is refused or put back."""
    folder = session_folder(data)
    pending = pending_path(folder, data)
    if not pending.is_file():
        return
    files = json.loads(pending.read_text())
    pending.unlink()
    said = user_messages(data.get("transcript_path"))
    notes = []
    for name, before in files.items():
        path = Path(name)
        after = read(path)
        if not added_lines(diff_hunks(before, after)):
            continue
        note = warning(host, path, before, after, said, not shown(folder, path))
        if note:
            notes.append(note)
    out = {}
    if notes:
        out["hookSpecificOutput"] = {"hookEventName": "PostToolUse", "additionalContext": "\n\n---\n\n".join(notes)}
    if NOTICES:
        out["systemMessage"] = "\n".join(NOTICES)
    if out:
        emit(out)


def main() -> None:
    if os.environ.get(GUARD_ENV):
        return
    data = json.load(sys.stdin, strict=False)
    tool = data.get("tool_name", "")
    inp = data.get("tool_input", {}) or {}
    cwd = data.get("cwd") or os.getcwd()
    codex = tool == "apply_patch" or "/.codex/" in (data.get("transcript_path") or "")
    host = "codex" if codex else "claude"
    event = data.get("hook_event_name", "PreToolUse")

    if tool == "Bash":
        if event == "PostToolUse":
            check_bash(host, inp.get("command", ""), cwd)
    elif tool not in ("apply_patch", "Edit", "Write", "MultiEdit"):
        return
    elif event == "PostToolUse":
        after_edit(host, data)
    elif tool == "apply_patch":
        before_edit(data, codex_changes(inp.get("command", ""), cwd))
    else:
        before_edit(data, claude_changes(tool, inp, cwd))


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        # A broken hook must never block the user's work
        sys.exit(0)
