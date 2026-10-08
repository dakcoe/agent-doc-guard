#!/usr/bin/env python3
"""PreToolUse/PostToolUse hook for Claude Code and Codex.

Agents re-read instruction files such as AGENTS.md, CLAUDE.md and skills on every task.
When an agent edits one of them after a single piece of feedback, it tends to write down the
incident instead of the rule: dates, who said what, what went wrong last time, a special case
for that one situation. Those lines stay in every future prompt and narrow the next agent's
judgement. This hook checks the lines an edit adds to such a file and refuses the edit when
they carry that kind of content, telling the agent which lines to rewrite as general rules.

File edits (Claude Code: Edit/Write/MultiEdit, Codex: apply_patch): added lines go through fixed
  patterns first, then a small model judges the rest. Lines that are only removed are never checked.
Bash: a command that visibly writes to an instruction file is refused before it runs. Whatever it
  misses is caught afterwards: instruction files are saved before the command and compared after it;
  a change that fails the same review is put back and the agent is told why.

The judge runs on the agent that called the hook: `claude -p` under Claude Code, `codex exec`
under Codex. If it cannot be reached, the edit is allowed and the user sees a notice
(set AGENT_DOC_GUARD_STRICT=1 to refuse instead).

Environment:
  AGENT_DOC_GUARD_CLAUDE_MODEL   judge model under Claude Code (default: haiku)
  AGENT_DOC_GUARD_CODEX_MODEL    judge model under Codex (default: gpt-6-luna)
  AGENT_DOC_GUARD_JUDGE          set to "off" to use the patterns only
  AGENT_DOC_GUARD_STRICT         set to "1" to refuse edits when the judge cannot be reached
"""

from __future__ import annotations

import difflib
import json
import os
import re
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
STRICT = os.environ.get("AGENT_DOC_GUARD_STRICT", "") == "1"
NAMES = {"AGENTS.md", "CLAUDE.md", "GEMINI.md", "SKILL.md", "MEMORY.md"}
AGENT_HOMES = [HOME / ".claude", HOME / ".codex"]
NOT_REFERENCE = [HOME / ".claude" / "plans"]
# Change logs are meant to hold dates and history
LOG_NAME = re.compile(r"^(?:CHANGELOG|CHANGES|HISTORY|RELEASE[-_]?NOTES)\b.*\.md$", re.IGNORECASE)
MAX_DOC_CHARS = 24_000
# added lines per judge call; a bigger rewrite is judged in several calls
MAX_ADDED_CHARS = 5_000
HANGUL = re.compile(r"[가-힣]")

PATTERNS = [
    # dates
    (r"\b20\d{2}-\d{1,2}-\d{1,2}\b", "date"),
    (r"\d{1,2}월 ?\d{1,2}일", "date"),
    (r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.? \d{1,2}(?:st|nd|rd|th)?\b", "date"),
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

Find lines in [ADDED LINES] that fall into one of these kinds:
1. provenance: dates, who pointed out what and when, which task/video/file something happened in, quotes of the user.
2. anecdote: a description of something that actually happened ("A and B overlapped before"), including when it is attached as the reason for a rule.
3. example: an example that records a specific case (what happened, in which task). Templates that show the form of good output are fine: a command or path format, or a pair contrasting a wording to avoid with the wording to use.
4. narrow-condition: a special-case branch that came from one situation ("if X, do Y") where one general rule would do.
5. duplicate: a rule that a different line of [FILE AFTER EDIT] already states. In [FILE AFTER EDIT] the added lines are marked [NEW]; a [NEW] line is not a duplicate of itself.
6. unnecessary: a line whose removal would not change what the agent does: background with no rule, a summary, or an instruction the agent already follows by default.

Facts the agent cannot know on its own (locations, commands, paths, environment, measured numbers and limits, known causes and their fixes, how the current system differs from its stated design), general rules, and a short reason that explains why a rule exists are fine.
If nothing is wrong, set ok to true and problems to an empty list.
For each problem, put the first 40 characters of the line in `line` and one sentence on how to fix it in `fix`.
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
                    "kind": {"type": "string", "enum": ["provenance", "anecdote", "example", "narrow-condition", "duplicate", "unnecessary"]},
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
        "header": (
            "{name} is an instruction file that agents re-read on every task. Keep only short, clear rules: "
            "no dates, history, anecdotes, case-specific examples, special cases from one event, repeated rules, or lines that change nothing. "
            "Rewrite these lines as one general rule each, or drop them, then edit again.\n"
        ),
        "bash": (
            "{path} is an instruction file that agents re-read on every task. Do not change it with a shell command; "
            "use {tool} so the change is checked. If the command only reads the file, remove the part that "
            "looks like a write (redirect, sed -i, write call) and run it again."
        ),
        "judge_failed": "agent-doc-guard: could not reach the judge model for {name}; only the pattern check was applied.",
        "judge_failed_strict": "agent-doc-guard: could not reach the judge model for {name}, and strict mode refuses unchecked edits. Try again.",
        "reverted": "Your command changed an instruction file in a way that did not pass review, so the change was put back. Make the edit with {tool} instead, following the notes below.",
    },
    "ko": {
        "header": (
            "{name} 은 에이전트가 작업마다 다시 읽는 지침 문서입니다. 짧고 명확한 규칙만 둡니다. "
            "날짜·경위·사례·특정 사례의 예시·한 번 있었던 일에서 나온 좁은 조건·같은 규칙의 반복·지워도 할 일이 바뀌지 않는 문장은 넣지 않습니다. "
            "아래 줄을 일반 규칙 한 줄로 다시 쓰거나 빼고 다시 고치세요.\n"
        ),
        "bash": (
            "{path} 는 에이전트가 작업마다 다시 읽는 지침 문서입니다. 셸 명령으로 고치지 말고 {tool} 로 고치세요. "
            "그래야 검사를 거칩니다. 읽기만 하는 명령이었다면 쓰기로 보이는 부분(리다이렉트, sed -i, write 호출)을 "
            "빼고 다시 실행하세요."
        ),
        "judge_failed": "agent-doc-guard: {name} 판정 모델 호출에 실패해 패턴 검사만 적용했습니다.",
        "judge_failed_strict": "agent-doc-guard: {name} 판정 모델 호출에 실패했고, 엄격 모드라 검사하지 못한 수정은 막습니다. 다시 시도하세요.",
        "reverted": "명령이 지침 문서를 검사를 통과하지 못하는 내용으로 바꿔서 원래대로 되돌렸습니다. 아래 내용을 반영해 {tool} 로 다시 고치세요.",
    },
}
EDIT_TOOL = {"claude": "the Edit tool", "codex": "apply_patch"}


def language(text: str) -> str:
    return "ko" if HANGUL.search(text) else "en"


def emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False))
    sys.exit(0)


def emit_deny(reason: str) -> None:
    emit({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    })


def under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


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
    if {".claude", ".agents", ".codex"} & set(path.parts):
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


def added_lines(old: str, new: str) -> list[str]:
    return [line[2:] for line in difflib.ndiff(old.splitlines(), new.splitlines())
            if line.startswith("+ ") and line[2:].strip()]


Change = "tuple[Path, list[str], str]"  # file, lines the edit adds, file text after the edit


def claude_changes(tool: str, inp: dict, cwd: str) -> list[Change]:
    path = resolve(inp.get("file_path", ""), cwd)
    before = read(path)
    if tool == "Write":
        after = inp.get("content", "")
        return [(path, added_lines(before, after), after)]
    edits = inp.get("edits") if tool == "MultiEdit" else [inp]
    lines: list[str] = []
    after = before
    for e in edits or []:
        old, new = e.get("old_string", ""), e.get("new_string", "")
        lines += added_lines(old, new)
        after = after.replace(old, new) if e.get("replace_all") else after.replace(old, new, 1)
    return [(path, lines, after)]


PATCH_FILE = re.compile(r"^\*\*\* (Add|Update|Delete) File: (.+)$")


def codex_changes(patch: str, cwd: str) -> list[Change]:
    """Files an apply_patch touches, with the lines it adds. Deleting a file adds nothing.
    The text after the edit is approximate: removed lines dropped, added lines appended."""
    files: dict[Path, tuple[list[str], list[str]]] = {}
    current = None
    for line in patch.splitlines():
        m = PATCH_FILE.match(line)
        if m:
            current = resolve(m.group(2).strip(), cwd)
            files.setdefault(current, ([], []))
            continue
        if line.startswith("*** Move to: ") and current is not None:
            current = resolve(line[len("*** Move to: "):].strip(), cwd)
            files.setdefault(current, ([], []))
            continue
        if current is None or line.startswith("***") or line.startswith("@@"):
            continue
        if line.startswith("+"):
            files[current][1].append(line[1:])
        elif line.startswith("-"):
            files[current][0].append(line[1:])
    changes = []
    for p, (old, new) in files.items():
        kept = read(p).splitlines()
        for line in old:
            if line in kept:
                kept.remove(line)
        changes.append((p, added_lines("\n".join(old), "\n".join(new)), "\n".join(kept + new)))
    return changes


def pattern_problems(lines: list[str]) -> list[str]:
    found = []
    for line in lines:
        for pattern, kind in PATTERNS:
            if re.search(pattern, line, re.IGNORECASE):
                found.append(f"- [{kind}] {line.strip()[:80]}")
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


def mark_new(after: str, lines: list[str]) -> str:
    new = set(lines)
    return "\n".join(f"[NEW] {line}" if line in new else line for line in after.splitlines())


def chunks(lines: list[str]) -> list[list[str]]:
    """Added lines in batches small enough for one judge call."""
    out, size = [[]], 0
    for line in lines:
        if out[-1] and size + len(line) > MAX_ADDED_CHARS:
            out.append([])
            size = 0
        out[-1].append(line)
        size += len(line)
    return out


def judge_once(host: str, path: Path, context: str, lines: list[str], lang: str) -> list[str] | None:
    message = (
        f"[FILE] {path}\n\n[FIX LANGUAGE] {'Korean' if lang == 'ko' else 'English'}\n\n"
        f"[FILE AFTER EDIT]\n{context}\n\n"
        "[ADDED LINES]\n" + "\n".join(lines)
    )
    env = {**os.environ, GUARD_ENV: "1"}
    ask = ask_codex if host == "codex" else ask_claude
    for _ in range(2):
        try:
            data = ask(message, env)
            problems = data["problems"]
        except Exception:
            continue
        return [f"- [{p['kind']}] {p['line'][:80]} → {p['fix']}" for p in problems]
    return None


def judge(host: str, path: Path, after: str, lines: list[str], lang: str) -> list[str] | None:
    """Problems found by the judge model; [] if none, None if any batch could not be judged.
    A large rewrite is judged in batches, several at once."""
    marked = mark_new(after, lines)
    batches = chunks(lines)
    if len(batches) == 1:
        contexts = [marked[:MAX_DOC_CHARS]]
    else:
        # each batch sees only the part of the file around its own lines
        rows = marked.splitlines()
        contexts = []
        for b in batches:
            hits = [i for i, row in enumerate(rows) if row.startswith("[NEW] ") and row[6:] in set(b)]
            lo, hi = (max(0, min(hits) - 40), max(hits) + 40) if hits else (0, 80)
            contexts.append("\n".join(rows[lo:hi])[:MAX_DOC_CHARS])
    with ThreadPoolExecutor(max_workers=min(8, len(batches))) as pool:
        results = list(pool.map(lambda bc: judge_once(host, path, bc[1], bc[0], lang), zip(batches, contexts)))
    problems = [p for r in results if r for p in r]
    if problems:
        return problems
    return None if any(r is None for r in results) else []


# A write inside a shell command. Heredocs and cat are left out because they are used for reading too.
WRITE_IN_BASH = re.compile(
    r"sed\s+-i|perl\s+-[a-z]*i|\btee\b|>\s*[^&\s|]*\.md|\.write\(|write_text|open\([^)]*['\"][wa]|\brm\b"
)
# cp/mv: only the last argument (the destination) is written
COPY_OR_MOVE = re.compile(r"\b(?:cp|mv)\b[^;&|]*?\s(\S+\.md)\s*(?:[;&|]|$)")
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


def check_bash(host: str, command: str, cwd: str) -> None:
    """Quick refusal for commands that visibly write to an instruction file. Anything it misses is
    caught after the command runs, by comparing the files (see check_bash_result)."""
    command = strip_heredocs(command)
    targets = [resolve(m.group(1), cwd) for m in COPY_OR_MOVE.finditer(command)]
    if WRITE_IN_BASH.search(command):
        targets += [resolve(t, cwd) for t in MD_TOKEN.findall(command)]
    for p in targets:
        if is_reference(p):
            emit_deny(MESSAGES[language(read(p) + command)]["bash"].format(path=p, tool=EDIT_TOOL[host]))


def review(host: str, path: Path, lines: list[str], after: str) -> str | None:
    """Reason to refuse an edit that adds `lines` to `path`, or None if it may stay."""
    if not lines or not is_reference(path):
        return None
    lang = language(after + "\n".join(lines))
    msg = MESSAGES[lang]
    header = msg["header"].format(name=path.name)
    found = pattern_problems(lines)
    if found:
        return header + "\n".join(found)
    if not JUDGE_ON:
        return None
    problems = judge(host, path, after, lines, lang)
    if problems is None:
        if STRICT:
            return msg["judge_failed_strict"].format(name=path.name)
        print(json.dumps({"systemMessage": msg["judge_failed"].format(name=path.name)}, ensure_ascii=False))
        return None
    return header + "\n".join(problems) if problems else None


def check_file(host: str, path: Path, lines: list[str], after: str) -> None:
    reason = review(host, path, lines, after)
    if reason:
        emit_deny(reason)


# --- Bash: compare instruction files before and after the command -------------------------------

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "repos", "cache", "plugins",
             "projects", "dist", "build", ".next", "site-packages"}
MAX_FILE_BYTES = 300_000
MAX_FILES = 400


def guides_below(root: Path, depth: int = 4) -> list[Path]:
    found = []
    base = len(root.parts)
    for folder, dirs, files in os.walk(root):
        here = Path(folder)
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and len(here.parts) - base < depth]
        found += [here / f for f in files if f in ("AGENTS.md", "CLAUDE.md", "GEMINI.md")]
    return found


def named_docs(guide: Path) -> list[Path]:
    """Markdown files a guide names by path."""
    text = read(guide)
    out = []
    for token in set(MD_TOKEN.findall(text)):
        p = (guide.parent / token).resolve() if not token.startswith(("/", "~")) else resolve(token, "/")
        if p.is_file():
            out.append(p)
    return out


def watched_files(cwd: str, command: str) -> set[Path]:
    """Instruction files a shell command in `cwd` could plausibly change."""
    start = Path(cwd).resolve()
    guides = list(guides_below(start))
    for folder in [start, *start.parents]:
        guides += [folder / n for n in ("AGENTS.md", "CLAUDE.md", "GEMINI.md") if (folder / n).is_file()]
        if folder == HOME:
            break
    for home in AGENT_HOMES:
        guides += [p for p in (home / "CLAUDE.md", home / "AGENTS.md") if p.is_file()]
        for sub in ("context", "skills", "rules", "agents", "commands"):
            if (home / sub).is_dir():
                guides += list(guides_below(home / sub, depth=3))
                guides += [p for p in (home / sub).rglob("*.md") if p.is_file()][:MAX_FILES]
    files = set()
    for g in guides:
        files.add(g.resolve())
        files.update(named_docs(g))
    files.update(resolve(t, cwd) for t in MD_TOKEN.findall(command))
    return {p for p in list(files)[: MAX_FILES * 2] if is_reference(p)}


def snapshot_path(data: dict) -> Path:
    key = data.get("tool_use_id") or str(abs(hash(json.dumps(data.get("tool_input", {}), sort_keys=True))))
    folder = Path(tempfile.gettempdir()) / "agent-doc-guard"
    folder.mkdir(exist_ok=True)
    return folder / f"{data.get('session_id', 'session')}-{re.sub(r'[^A-Za-z0-9_-]', '', key)}.json"


def take_snapshot(data: dict, cwd: str, command: str) -> None:
    # snapshots of commands that never reached PostToolUse (refused, failed) are dropped after an hour
    for old in snapshot_path(data).parent.glob("*.json"):
        try:
            if old.stat().st_mtime < time.time() - 3600:
                old.unlink()
        except OSError:
            continue
    files = {}
    for p in watched_files(cwd, command):
        try:
            if p.stat().st_size <= MAX_FILE_BYTES:
                files[str(p)] = p.read_text(errors="ignore")
        except OSError:
            continue
    snapshot_path(data).write_text(json.dumps({"cwd": cwd, "command": command, "files": files}))


def check_bash_result(host: str, data: dict) -> None:
    """After a shell command: any instruction file it changed gets the same review as an edit.
    A refused change is put back and the agent is told why."""
    snap = snapshot_path(data)
    if not snap.is_file():
        return
    saved = json.loads(snap.read_text())
    snap.unlink()
    before = saved["files"]
    paths = set(before) | {str(p) for p in watched_files(saved["cwd"], saved["command"])}
    reasons = []
    for name in sorted(paths):
        p = Path(name)
        old = before.get(name, "")
        new = read(p)
        if new == old or not p.exists():
            continue  # unchanged, or deleted
        reason = review(host, p, added_lines(old, new), new)
        if reason:
            if name in before:
                p.write_text(old)
            else:
                p.unlink()
            reasons.append(reason)
    if reasons:
        lang = language("\n".join(reasons))
        note = MESSAGES[lang]["reverted"].format(tool=EDIT_TOOL[host])
        emit({
            "decision": "block",
            "reason": note + "\n\n" + "\n\n".join(reasons),
            "hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": note},
        })


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

    if tool == "Bash" and event == "PostToolUse":
        check_bash_result(host, data)
    elif tool == "Bash":
        command = inp.get("command", "")
        check_bash(host, command, cwd)
        take_snapshot(data, cwd, command)
    elif event != "PreToolUse":
        return
    elif tool == "apply_patch":
        for path, lines, after in codex_changes(inp.get("command", ""), cwd):
            check_file(host, path, lines, after)
    elif tool in ("Edit", "Write", "MultiEdit"):
        for path, lines, after in claude_changes(tool, inp, cwd):
            check_file(host, path, lines, after)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        # A broken hook must never block the user's work
        sys.exit(0)
