#!/usr/bin/env python3
"""PreToolUse/PostToolUse hook for Claude Code and Codex.

Agents re-read instruction files such as AGENTS.md, CLAUDE.md and skills on every task.
When an agent edits one of them after a single piece of feedback, it tends to write down the
incident instead of the rule: dates, who said what, what went wrong last time, a special case
for that one situation. Those lines stay in every future prompt and narrow the next agent's
judgement. This hook checks the lines an edit adds to such a file and refuses the edit when
they carry that kind of content, telling the agent which lines to rewrite as general rules.

File edits (Claude Code: Edit/Write/MultiEdit, Codex: apply_patch): the whole file after the edit is
  built first and compared with the file before it. Added and changed lines go through fixed patterns,
  then a small model judges them from the diff. Lines that are only removed are never checked, and lines
  the edit leaves alone are left to the doc-cleanup skill.
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
STRICT = os.environ.get("AGENT_DOC_GUARD_STRICT", "") == "1"
NAMES = {"AGENTS.md", "CLAUDE.md", "GEMINI.md", "SKILL.md", "MEMORY.md"}
AGENT_HOMES = [HOME / ".claude", HOME / ".codex"]
NOT_REFERENCE = [HOME / ".claude" / "plans"]
# Change logs are meant to hold dates and history
LOG_NAME = re.compile(r"^(?:CHANGELOG|CHANGES|HISTORY|RELEASE[-_]?NOTES)\b.*\.md$", re.IGNORECASE)
MAX_DOC_CHARS = 24_000
# diff text per judge call; a bigger rewrite is judged in several calls, split at hunk boundaries
MAX_DIFF_CHARS = 5_000
HANGUL = re.compile(r"[가-힣]")

PATTERNS = [
    # dates
    (r"(?<!\d)20\d{2}-\d{1,2}-\d{1,2}(?!\d)", "date"),
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

[DIFF] is a unified diff of the edit. Judge only the lines that start with `+`: they are added or changed by this edit.
Lines that start with `-` are removed by this edit and are not in the file afterwards; never treat them as something a `+` line duplicates.
Lines that start with a space are unchanged context; do not report them.
[FILE AFTER EDIT] is the whole file once the edit is applied, without diff markers.

Find `+` lines that fall into one of these kinds:
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


def on_added_line(problem: dict, lines: list[str]) -> bool:
    """Whether the judge's `line` points at one of the added lines: equal or the start of one once
    spaces and leading marks are normalized. A problem pointing elsewhere is about a line this edit
    did not touch."""
    quoted = squash(problem.get("line", "")).rstrip(".…").strip()
    if not quoted:
        return False
    return any(squash(l).startswith(quoted) for l in lines)


def judge_once(host: str, path: Path, after: str, batch: list[list[str]], lang: str) -> list[str] | None:
    diff = "\n".join(l for h in batch for l in h)
    message = (
        f"[FILE] {path}\n\n[FIX LANGUAGE] {'Korean' if lang == 'ko' else 'English'}\n\n"
        f"[DIFF]\n{diff}\n\n"
        f"[FILE AFTER EDIT]\n{after[:MAX_DOC_CHARS]}"
    )
    lines = added_lines(batch)
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


def judge(host: str, path: Path, after: str, hunks: list[list[str]], lang: str) -> list[str] | None:
    """Problems found by the judge model; [] if none, None if any batch could not be judged.
    A large rewrite is judged in batches, several at once."""
    groups = batches(hunks)
    with ThreadPoolExecutor(max_workers=min(8, len(groups))) as pool:
        results = list(pool.map(lambda b: judge_once(host, path, after, b, lang), groups))
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
    """Files one simple command writes, as far as its words show: redirect targets, tee files,
    sed -i / perl -i files, rm arguments, the cp/mv destination, and .md paths in code that writes."""
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
    if not args:
        return [resolve(t, cwd) for t in targets]
    name = os.path.basename(args[0])
    files = [a for a in args[1:] if not a.startswith("-")]
    if name == "tee":
        targets += files
    elif name in ("sed", "gsed", "perl") and any(re.match(r"^-[A-Za-z]*i|^--in-place", a) for a in args[1:]):
        targets += [a for a in files if a.endswith(".md")]
    elif name == "rm":
        targets += files
    elif name in ("cp", "mv", "install") and len(files) >= 2:
        dest = resolve(files[-1], cwd)
        if dest.is_dir():
            return [resolve(t, cwd) for t in targets] + [dest / Path(f).name for f in files[:-1]]
        targets.append(files[-1])
    return [resolve(t, cwd) for t in targets]


def check_bash(host: str, command: str, cwd: str) -> None:
    """Quick refusal for commands that visibly write to an instruction file. Anything it misses is
    caught after the command runs, by comparing the files (see check_bash_result)."""
    targets = [p for words, folder, line in simple_commands(command, cwd) for p in write_targets(words, folder, line)]
    for p in targets:
        if is_reference(p):
            emit_deny(MESSAGES[language(read(p) + command)]["bash"].format(path=p, tool=EDIT_TOOL[host]))


def review(host: str, path: Path, before: str, after: str) -> str | None:
    """Reason to refuse an edit that turns `path` from `before` into `after`, or None if it may stay."""
    if not is_reference(path):
        return None
    hunks = diff_hunks(before, after)
    lines = added_lines(hunks)
    if not lines:
        return None
    lang = language(after + "\n".join(lines))
    msg = MESSAGES[lang]
    header = msg["header"].format(name=path.name)
    found = pattern_problems(lines)
    if found:
        return header + "\n".join(found)
    if not JUDGE_ON:
        return None
    problems = judge(host, path, after, hunks, lang)
    if problems is None:
        if STRICT:
            return msg["judge_failed_strict"].format(name=path.name)
        print(json.dumps({"systemMessage": msg["judge_failed"].format(name=path.name)}, ensure_ascii=False))
        return None
    return header + "\n".join(problems) if problems else None


def check_file(host: str, path: Path, before: str, after: str) -> None:
    reason = review(host, path, before, after)
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
    commands = simple_commands(command, cwd)
    guides = []
    for start in {Path(cwd).resolve(), *(Path(folder) for _, folder, _ in commands)}:
        guides += guides_below(start)
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
    files.update(resolve(t, folder) for _, folder, line in commands for t in MD_TOKEN.findall(line))
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
        reason = review(host, p, old, new)
        if reason:
            if p.is_symlink():
                # saved paths are resolved, so a link here was made by the command; writing through
                # it would overwrite the file it points to
                p.unlink()
            if name in before:
                p.write_text(old)
            elif p.exists():
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
        for path, before, after in codex_changes(inp.get("command", ""), cwd):
            check_file(host, path, before, after)
    elif tool in ("Edit", "Write", "MultiEdit"):
        for path, before, after in claude_changes(tool, inp, cwd):
            check_file(host, path, before, after)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        # A broken hook must never block the user's work
        sys.exit(0)
