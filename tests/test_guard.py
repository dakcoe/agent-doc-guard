"""Run: python3 -m unittest discover tests
The judge model is switched off here; these tests cover file detection, the patterns and the Bash rules."""

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
        (root / "AGENTS.md").write_text("# Project\nRead `docs/style.md` before writing.\n")
        (root / "docs" / "style.md").write_text("# Style\n- Keep captions to two lines.\n")
        (root / "notes.md").write_text("scratch\n")
        self.root = root

    def tearDown(self):
        self.tmp.cleanup()

    def edit(self, rel: str, new: str, old: str = "x") -> dict:
        return run({"tool_name": "Edit", "cwd": str(self.root),
                    "tool_input": {"file_path": str(self.root / rel), "old_string": old, "new_string": new}})

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
        for line in ["10월 3일 영상에서 겹쳤다.", "전환이 밋밋하다고 지적받았다.", "넣었다가 다시 뺐다.",
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
        self.assertFalse(denied(self.edit("docs/style.md", "", old="Fixed on 2026-10-05.")))

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


if __name__ == "__main__":
    unittest.main()
