"""Run: python3 -m unittest discover tests
GuardTest runs the hook with the judge model switched off: file detection, the patterns and the Bash rules.
JudgeTest replaces the judge model with a fake: what the judge is sent, and which of its answers count."""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "agent_doc_guard.py"


def run(payload: dict) -> dict:
    env = {**os.environ, "AGENT_DOC_GUARD_JUDGE": "off"}
    env.pop("AGENT_DOC_GUARD_ACTIVE", None)
    out = subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps(payload),
                         capture_output=True, text=True, env=env).stdout.strip()
    return json.loads(out) if out else {}


def denied(result: dict) -> bool:
    return result.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "docs").mkdir()
        (root / "AGENTS.md").write_text("# Project\nRead `docs/style.md` before writing.\nx\n")
        (root / "docs" / "style.md").write_text("# Style\n- Keep captions to two lines.\nx\n")
        (root / "notes.md").write_text("scratch\n")
        self.root = root

    def tearDown(self):
        self.tmp.cleanup()

    def edit(self, rel: str, new: str, old: str = "x") -> dict:
        """Edit `rel`, replacing `old`. A file that does not exist yet starts as the line "x"."""
        path = self.root / rel
        if not path.exists():
            path.write_text("x\n")
        return run({"tool_name": "Edit", "cwd": str(self.root),
                    "tool_input": {"file_path": str(path), "old_string": old, "new_string": new}})

    def bash(self, command: str) -> dict:
        return run({"tool_name": "Bash", "cwd": str(self.root), "tool_input": {"command": command}})

    # which files count as instruction files
    def test_agents_md_and_linked_doc_are_checked(self):
        self.assertTrue(denied(self.edit("AGENTS.md", "Fixed on 2026-10-05.")))
        self.assertTrue(denied(self.edit("docs/style.md", "Fixed on 2026-10-05.")))

    def test_bare_name_only_matches_next_to_the_guide(self):
        (self.root / "AGENTS.md").write_text("Put links in upload.md when publishing.\n")
        (self.root / "videos" / "a").mkdir(parents=True)
        self.assertTrue(denied(self.edit("upload.md", "Fixed on 2026-10-05.")))
        self.assertFalse(denied(self.edit("videos/a/upload.md", "Uploaded on 2026-10-05.")))

    def test_changelog_is_ignored(self):
        (self.root / "AGENTS.md").write_text("Record changes in CHANGELOG.md.\n")
        self.assertFalse(denied(self.edit("CHANGELOG.md", "## 2026-10-05\n- Added Codex support.")))

    def test_unlinked_markdown_is_ignored(self):
        self.assertFalse(denied(self.edit("notes.md", "Fixed on 2026-10-05.")))

    def test_non_markdown_is_ignored(self):
        self.assertFalse(denied(self.edit("app.py", "# 2026-10-05")))

    # patterns
    def test_korean_history_is_refused(self):
        for line in ["10월 3일 영상에서 겹쳤다.", "2026-10-05부터 테스트를 돌린다.", "전환이 밋밋하다고 지적받았다.", "넣었다가 다시 뺐다.",
                     "## 9. 네트워크 영상에서 더한 것"]:
            self.assertTrue(denied(self.edit("docs/style.md", line)), line)

    def test_english_history_is_refused(self):
        for line in ["On Oct 3 the titles overlapped.", "The user said the transitions were flat.",
                     "Last time the build broke here.", "## Lessons learned"]:
            self.assertTrue(denied(self.edit("docs/style.md", line)), line)

    def test_plain_rules_pass(self):
        for line in ["Keep transitions moving between scenes.", "장면 전환에는 움직임을 넣는다.",
                     "Run `npm test` before committing."]:
            self.assertFalse(denied(self.edit("docs/style.md", line)), line)

    def test_removing_lines_is_never_checked(self):
        (self.root / "docs" / "style.md").write_text("# Style\nFixed on 2026-10-05.\n")
        self.assertFalse(denied(self.edit("docs/style.md", "", old="Fixed on 2026-10-05.")))

    def test_words_already_in_the_line_are_not_checked_again(self):
        (self.root / "docs" / "style.md").write_text("# Style\n- 지적한 부분만 고친다.\n")
        self.assertFalse(denied(self.edit("docs/style.md", "- 지적한 부분만 고치고 폰트는 그대로 둔다.",
                                          old="- 지적한 부분만 고친다.")))
        self.assertTrue(denied(self.edit("docs/style.md", "- 지적한 부분만 고친다. 10월 5일에 정했다.",
                                         old="- 지적한 부분만 고친다.")))
        (self.root / "docs" / "style.md").write_text("# Style\n- 10월 3일부터 자막은 두 줄로 쓴다.\n")
        self.assertTrue(denied(self.edit("docs/style.md", "- 10월 5일부터 자막은 두 줄로 쓴다.",
                                         old="- 10월 3일부터 자막은 두 줄로 쓴다.")))

    def test_message_names_the_matched_words(self):
        reason = self.edit("docs/style.md", "넣었다가 다시 뺐다.")["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn('[past incident] "넣었다가"', reason)

    def test_message_follows_file_language(self):
        reason = self.edit("docs/style.md", "10월 3일에 겹쳤다.")["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("지침 문서", reason)
        reason = self.edit("docs/style.md", "Fixed on 2026-10-05.")["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("instruction file", reason)

    # Bash
    def test_bash_writes_are_refused(self):
        for c in ["sed -i '' s/a/b/ AGENTS.md", "echo hi >> docs/style.md", "rm docs/style.md",
                  "cp /tmp/x.md AGENTS.md", "python3 -c \"open('AGENTS.md','w').write('x')\""]:
            self.assertTrue(denied(self.bash(c)), c)

    def test_bash_reads_pass(self):
        for c in ["cat AGENTS.md", "grep -n rule AGENTS.md 2>/dev/null", "sed -n 1,5p docs/style.md",
                  "cp AGENTS.md /tmp/copy.md", "echo hi > notes.md"]:
            self.assertFalse(denied(self.bash(c)), c)

    def test_bash_read_with_unrelated_write_passes(self):
        for c in ["diff -u notes.md AGENTS.md > out.diff && rm out.diff", "diff notes.md AGENTS.md > /tmp/x.md",
                  "grep rule AGENTS.md | tee out.txt", "sed -i '' s/a/b/ notes.md && cat AGENTS.md",
                  "mkdir -p out && cp -t out notes.md docs/style.md"]:
            self.assertFalse(denied(self.bash(c)), c)

    def test_backup_copy_of_agent_folder_is_not_an_instruction_file(self):
        backup = self.root / "backup" / "before" / ".claude" / "context"
        backup.mkdir(parents=True)
        (backup / "x.md").write_text("old\n")
        (self.root / ".claude" / "context").mkdir(parents=True)
        (self.root / ".claude" / "context" / "x.md").write_text("rule\n")
        for c in ["cp .claude/context/x.md backup/before/.claude/context/x.md",
                  "diff -u backup/before/.claude/context/x.md .claude/context/x.md > /tmp/o.diff"]:
            self.assertFalse(denied(self.bash(c)), c)
        # the project's own .claude folder still counts
        self.assertTrue(denied(self.bash("cp backup/before/.claude/context/x.md .claude/context/x.md")))
        self.assertTrue(denied(self.bash("cp notes.md .claude/context/")))
        guard = load_guard()
        self.assertTrue(guard.is_reference(Path.home() / ".agents" / "skills" / "x" / "reference.md"))

    def test_bash_paths_follow_cd(self):
        (self.root / "sub").mkdir()
        reason = self.bash("cd sub && echo hi >> AGENTS.md")["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn(str(Path("sub") / "AGENTS.md"), reason)
        self.assertTrue(denied(self.bash("cd docs && sed -i.bak s/a/b/ style.md")))

    # Codex
    def patch(self, body: str) -> dict:
        return run({"tool_name": "apply_patch", "cwd": str(self.root),
                    "transcript_path": "/home/u/.codex/sessions/x.jsonl",
                    "tool_input": {"command": "*** Begin Patch\n" + body + "\n*** End Patch"}})

    def test_codex_patch_adding_history_is_refused(self):
        self.assertTrue(denied(self.patch("*** Update File: docs/style.md\n@@\n-x\n+Fixed on 2026-10-05.")))
        self.assertTrue(denied(self.patch("*** Add File: other/AGENTS.md\n+10월 3일에 겹쳤다.")))

    def test_codex_patch_rules_and_other_files_pass(self):
        self.assertFalse(denied(self.patch("*** Update File: docs/style.md\n@@\n+Keep captions short.")))
        self.assertFalse(denied(self.patch("*** Update File: notes.md\n@@\n+Fixed on 2026-10-05.")))
        self.assertFalse(denied(self.patch("*** Delete File: docs/style.md")))

    def test_codex_bash_message_names_apply_patch(self):
        r = run({"tool_name": "Bash", "cwd": str(self.root), "transcript_path": "/home/u/.codex/sessions/x.jsonl",
                 "tool_input": {"command": "echo hi >> AGENTS.md"}})
        self.assertIn("apply_patch", r["hookSpecificOutput"]["permissionDecisionReason"])

    # Bash: compare files before and after the command
    def run_bash(self, command: str) -> dict:
        payload = {"tool_name": "Bash", "cwd": str(self.root), "session_id": "t", "tool_use_id": "u1",
                   "tool_input": {"command": command}}
        pre = run({**payload, "hook_event_name": "PreToolUse"})
        if denied(pre):
            return pre
        subprocess.run(["bash", "-c", command], cwd=self.root)
        return run({**payload, "hook_event_name": "PostToolUse"})

    def test_hidden_shell_writes_are_reverted(self):
        original = (self.root / "AGENTS.md").read_text()
        (self.root / "fix.py").write_text("open('AGENTS.md','a').write('- Fixed on 2026-10-05.\\n')\n")
        for command in ["python3 fix.py", 'p="AGENTS"; echo "- Fixed on 2026-10-05." >> "$p.md"']:
            result = self.run_bash(command)
            self.assertEqual(result.get("decision"), "block", command)
            self.assertEqual((self.root / "AGENTS.md").read_text(), original, command)

    def test_reverting_a_new_symlink_keeps_its_target(self):
        original = (self.root / "AGENTS.md").read_text()
        (self.root / "old.txt").write_text("Fixed on 2026-10-05.\n")
        result = self.run_bash("ln -sf old.txt AGENTS.md")
        self.assertEqual(result.get("decision"), "block")
        self.assertFalse((self.root / "AGENTS.md").is_symlink())
        self.assertEqual((self.root / "AGENTS.md").read_text(), original)
        self.assertEqual((self.root / "old.txt").read_text(), "Fixed on 2026-10-05.\n")

    def test_shell_write_into_linked_doc_is_reverted(self):
        original = (self.root / "docs" / "style.md").read_text()
        result = self.run_bash("cd docs && printf '%s\\n' '- 10월 3일에 겹쳤다.' | tee -a style.md >/dev/null")
        self.assertTrue(result.get("decision") == "block" or denied(result))
        self.assertEqual((self.root / "docs" / "style.md").read_text(), original)

    def test_clean_shell_write_stays(self):
        result = self.run_bash("printf '%s\\n' '- Keep captions short.' > /tmp/agent-doc-guard-x && cat /tmp/agent-doc-guard-x >/dev/null")
        self.assertEqual(result, {})

    def test_python_heredoc_is_checked(self):
        cmd = "python3 - <<'EOF'\nopen('AGENTS.md','w').write('x')\nEOF"
        self.assertTrue(denied(self.bash(cmd)))

    def test_heredoc_body_is_not_a_path(self):
        commit = "rm -f build.log && git commit -F - <<'EOF'\nUpdate AGENTS.md and CLAUDE.md\nEOF"
        self.assertFalse(denied(self.bash(commit)))
        self.assertTrue(denied(self.bash("cat > AGENTS.md <<'EOF'\n- rule\nEOF")))

    def test_broken_input_never_blocks(self):
        out = subprocess.run([sys.executable, str(SCRIPT)], input="not json", capture_output=True, text=True)
        self.assertEqual(out.returncode, 0)
        self.assertEqual(out.stdout.strip(), "")


def load_guard():
    spec = importlib.util.spec_from_file_location("agent_doc_guard", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class JudgeTest(unittest.TestCase):
    """The judge model is replaced by a fake that records what it is sent and returns a set answer."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.guard = load_guard()
        self.guard.JUDGE_ON = True
        self.messages = []
        self.answer = {"ok": True, "problems": []}

        def fake(message, env):
            self.messages.append(message)
            return self.answer
        self.guard.ask_claude = fake

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, text: str) -> Path:
        path = self.root / "AGENTS.md"
        path.write_text(text)
        return path

    def review(self, tool: str, inp: dict):
        [(path, before, after)] = self.guard.claude_changes(tool, inp, str(self.root))
        return self.guard.review("claude", path, before, after)

    @staticmethod
    def section(message: str, name: str) -> str:
        return message.split(f"[{name}]\n", 1)[1].split("\n\n[", 1)[0]

    def test_with_the_judge_only_dates_are_refused_by_pattern(self):
        path = self.write("# Rules\nx\n")
        self.assertIsNone(self.review("Edit", {"file_path": str(path), "old_string": "x",
                                               "new_string": "- 지적한 부분만 고친다."}))
        self.assertEqual(len(self.messages), 1)
        self.assertIn("2026-10-05", self.review("Edit", {"file_path": str(path), "old_string": "x",
                                                         "new_string": "- Fixed on 2026-10-05."}))
        self.assertEqual(len(self.messages), 1)

    def test_word_patterns_apply_when_the_judge_fails(self):
        def broken(message, env):
            raise RuntimeError
        self.guard.ask_claude = broken
        path = self.write("# Rules\nx\n")
        self.assertIn("넣었다가", self.review("Edit", {"file_path": str(path), "old_string": "x",
                                                    "new_string": "- 넣었다가 다시 뺐다."}))

    def test_partial_edit_shows_whole_old_and_new_line(self):
        path = self.write("# Rules\n- Keep captions to two lines.\n- Run tests before committing.\n")
        self.review("Edit", {"file_path": str(path), "old_string": "two", "new_string": "three"})
        diff = self.section(self.messages[0], "DIFF")
        self.assertIn("\n-- Keep captions to two lines.\n", diff)
        self.assertIn("\n+- Keep captions to three lines.\n", diff)
        self.assertNotIn("[NEW]", self.messages[0])

    def test_problem_on_untouched_line_is_dropped(self):
        path = self.write("# Rules\n- Keep captions to two lines.\n- Run tests before committing.\n")
        edit = {"file_path": str(path), "old_string": "two", "new_string": "three"}
        self.answer = {"ok": False, "problems": [
            {"line": "- Run tests before committing.", "kind": "unnecessary", "fix": "drop it"}]}
        self.assertIsNone(self.review("Edit", edit))
        # the same kind of answer about the changed line still refuses the edit
        self.answer = {"ok": False, "problems": [
            {"line": "+-  Keep captions to three", "kind": "unnecessary", "fix": "drop it"}]}
        self.assertIn("Keep captions to three", self.review("Edit", edit))
        # a quote without the list bullet still points at the changed line
        self.answer = {"ok": False, "problems": [
            {"line": "Keep captions to three lines", "kind": "unnecessary", "fix": "drop it"}]}
        self.assertIsNotNone(self.review("Edit", edit))

    def test_section_removed_in_same_edit_is_only_a_minus_line(self):
        path = self.write("# Rules\n## A\n- Run the tests before every commit.\n\n## B\n- Keep captions short.\n")
        self.review("MultiEdit", {"file_path": str(path), "edits": [
            {"old_string": "## A\n- Run the tests before every commit.\n\n", "new_string": ""},
            {"old_string": "- Keep captions short.\n", "new_string": "- Keep captions short.\n- Run tests before each commit.\n"},
        ]})
        message = self.messages[0]
        diff = self.section(message, "DIFF")
        self.assertIn("\n-- Run the tests before every commit.", diff)
        self.assertIn("\n+- Run tests before each commit.", diff)
        self.assertNotIn("+- Run the tests before every commit.", diff)
        self.assertNotIn("Run the tests before every commit.", self.section(message, "FILE AFTER EDIT"))

    def test_codex_patch_uses_the_same_diff(self):
        path = self.write("# Rules\n- Keep captions to two lines.\n- Run tests before committing.\n")
        patch = ("*** Begin Patch\n*** Update File: AGENTS.md\n@@\n # Rules\n-- Keep captions to two lines.\n"
                 "+- Keep captions to three lines.\n*** End Patch")
        [(p, before, after)] = self.guard.codex_changes(patch, str(self.root))
        self.assertEqual(after, "# Rules\n- Keep captions to three lines.\n- Run tests before committing.\n")
        self.assertEqual(p, path.resolve())

    def test_large_rewrite_is_split_at_hunks(self):
        self.guard.MAX_DIFF_CHARS = 400
        rows = [f"- Rule number {i} stays as it is." for i in range(60)]
        path = self.write("\n".join(rows) + "\n")
        edited = list(rows)
        for i in (5, 30, 55):
            edited[i] = f"- Rule number {i} is reworded."
        self.review("Write", {"file_path": str(path), "content": "\n".join(edited) + "\n"})
        self.assertEqual(len(self.messages), 3)
        reworded = sorted(self.section(m, "DIFF").split("\n+")[1].split("\n")[0] for m in self.messages)
        self.assertEqual(reworded, [f"- Rule number {i} is reworded." for i in (30, 5, 55)])


if __name__ == "__main__":
    unittest.main()
