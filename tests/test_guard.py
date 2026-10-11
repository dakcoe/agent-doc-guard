"""Run: python3 -m unittest discover tests
GuardTest runs the hook with the judge model switched off: file detection, the first-edit refusal and the Bash note.
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


def hook_env(state: Path) -> dict:
    env = {**os.environ, "AGENT_DOC_GUARD_JUDGE": "off", "AGENT_DOC_GUARD_STATE_DIR": str(state)}
    env.pop("AGENT_DOC_GUARD_ACTIVE", None)
    return env


def run(payload: dict, state: Path) -> dict:
    out = subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps(payload),
                         capture_output=True, text=True, env=hook_env(state)).stdout.strip()
    return json.loads(out) if out else {}


def denied(result: dict) -> bool:
    return result.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


def reason(result: dict) -> str:
    return result["hookSpecificOutput"]["permissionDecisionReason"]


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name) / "project"
        (root / "docs").mkdir(parents=True)
        (root / "AGENTS.md").write_text("# Project\nRead `docs/style.md` before writing.\nx\n")
        (root / "docs" / "style.md").write_text("# Style\n- Keep captions to two lines.\nx\n")
        (root / "notes.md").write_text("scratch\n")
        self.root = root
        self.state = Path(self.tmp.name) / "state"
        self.session = "s1"

    def tearDown(self):
        self.tmp.cleanup()

    def hook(self, payload: dict) -> dict:
        return run({"cwd": str(self.root), "session_id": self.session, **payload}, self.state)

    def edit(self, rel: str, new: str, old: str = "x") -> dict:
        """Edit `rel`, replacing `old`. A file that does not exist yet starts as the line "x"."""
        path = self.root / rel
        if not path.exists():
            path.write_text("x\n")
        return self.hook({"tool_name": "Edit", "tool_input": {"file_path": str(path), "old_string": old, "new_string": new}})

    def gated(self, rel: str, new: str = "- Keep transitions moving.") -> bool:
        """Whether the first edit adding `new` to `rel` in a fresh session is refused."""
        self.session = f"s{abs(hash((rel, new)))}"
        return denied(self.edit(rel, new))

    # which files count as instruction files
    def test_agents_md_and_linked_doc_are_checked(self):
        self.assertTrue(self.gated("AGENTS.md"))
        self.assertTrue(self.gated("docs/style.md"))

    def test_bare_name_only_matches_next_to_the_guide(self):
        (self.root / "AGENTS.md").write_text("Put links in upload.md when publishing.\n")
        (self.root / "videos" / "a").mkdir(parents=True)
        self.assertTrue(self.gated("upload.md"))
        self.assertFalse(self.gated("videos/a/upload.md"))

    def test_changelog_is_ignored(self):
        (self.root / "AGENTS.md").write_text("Record changes in CHANGELOG.md.\n")
        self.assertFalse(self.gated("CHANGELOG.md", "## 2026-10-05\n- Added Codex support."))

    def test_unlinked_markdown_and_other_files_are_ignored(self):
        self.assertFalse(self.gated("notes.md"))
        self.assertFalse(self.gated("app.py", "# 2026-10-05"))

    def test_backup_copy_of_agent_folder_is_not_an_instruction_file(self):
        guard = load_guard()
        (self.root / "backup" / "before" / ".claude").mkdir(parents=True)
        (self.root / ".claude").mkdir()
        self.assertFalse(guard.is_reference(self.root / "backup" / "before" / ".claude" / "x.md"))
        self.assertTrue(guard.is_reference(self.root / ".claude" / "x.md"))
        self.assertTrue(guard.is_reference(Path.home() / ".agents" / "skills" / "x" / "reference.md"))

    # the first edit in a session
    def test_first_edit_is_refused_once_per_file_and_session(self):
        self.assertTrue(denied(self.edit("AGENTS.md", "- Keep transitions moving.")))
        self.assertFalse(denied(self.edit("AGENTS.md", "- Keep transitions moving.")))
        self.assertTrue(denied(self.edit("docs/style.md", "- Keep captions short.")))
        self.session = "s2"
        self.assertTrue(denied(self.edit("AGENTS.md", "- Keep transitions moving.")))

    def test_refusal_states_the_rules_and_quotes_the_added_lines(self):
        text = reason(self.edit("AGENTS.md", "- Keep transitions moving.\n- Fixed on 2026-10-05."))
        self.assertIn("instruction file", text)
        self.assertIn("+ - Keep transitions moving.", text)
        self.assertIn('[date] "2026-10-05"', text)
        self.assertNotIn("[date]", reason(self.edit("docs/style.md", "- Keep captions short.")))

    def test_message_follows_file_language(self):
        self.assertIn("지침 문서", reason(self.edit("docs/style.md", "- 10월 3일에 겹쳤다.")))
        self.assertIn("instruction file", reason(self.edit("AGENTS.md", "- Fixed on 2026-10-05.")))

    def test_retry_with_a_date_goes_through(self):
        self.edit("AGENTS.md", "- Fixed on 2026-10-05.")
        self.assertFalse(denied(self.edit("AGENTS.md", "- Fixed on 2026-10-05.")))

    def test_removing_lines_is_never_stopped(self):
        (self.root / "docs" / "style.md").write_text("# Style\nFixed on 2026-10-05.\n")
        self.assertFalse(denied(self.edit("docs/style.md", "", old="Fixed on 2026-10-05.")))
        # and does not use up the first-edit refusal
        self.assertTrue(denied(self.edit("docs/style.md", "- Keep captions short.", old="# Style")))

    def test_new_file_written_whole_is_refused_once(self):
        payload = {"tool_name": "Write", "tool_input": {"file_path": str(self.root / "sub" / "CLAUDE.md"),
                                                         "content": "# Rules\n- Keep it short.\n"}}
        self.assertTrue(denied(self.hook(payload)))
        self.assertFalse(denied(self.hook(payload)))

    def test_parallel_edits_each_get_their_own_record(self):
        payloads = [{"cwd": str(self.root), "session_id": "p", "tool_name": "Edit",
                     "tool_input": {"file_path": str(self.root / rel), "old_string": "x", "new_string": "- Rule."}}
                    for rel in ("AGENTS.md", "docs/style.md")]
        procs = [subprocess.Popen([sys.executable, str(SCRIPT)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  text=True, env=hook_env(self.state)) for _ in payloads]
        for proc, payload in zip(procs, payloads):
            proc.stdin.write(json.dumps(payload))
            proc.stdin.close()
        results = [json.loads(proc.stdout.read()) for proc in procs]
        for proc in procs:
            proc.wait()
            proc.stdout.close()
        self.assertTrue(all(denied(r) for r in results))
        self.session = "p"
        self.assertFalse(denied(self.edit("AGENTS.md", "- Rule.")))
        self.assertFalse(denied(self.edit("docs/style.md", "- Rule.")))

    # Codex
    def patch(self, body: str) -> dict:
        return self.hook({"tool_name": "apply_patch", "transcript_path": "/home/u/.codex/sessions/x.jsonl",
                          "tool_input": {"command": "*** Begin Patch\n" + body + "\n*** End Patch"}})

    def test_codex_patch_is_refused_once_for_all_files_in_it(self):
        body = "*** Update File: docs/style.md\n@@\n-x\n+Keep captions short.\n*** Add File: other/AGENTS.md\n+- Rule."
        text = reason(self.patch(body))
        self.assertIn("style.md", text)
        self.assertIn("AGENTS.md", text)
        self.assertFalse(denied(self.patch(body)))

    def test_codex_patch_on_other_files_and_deletions_pass(self):
        self.assertFalse(denied(self.patch("*** Update File: notes.md\n@@\n+Fixed on 2026-10-05.")))
        self.assertFalse(denied(self.patch("*** Delete File: docs/style.md")))

    # Bash
    def bash(self, command: str, transcript: str = "") -> str:
        """The note added after `command` ran, or "" if none."""
        r = self.hook({"tool_name": "Bash", "hook_event_name": "PostToolUse", "transcript_path": transcript,
                       "tool_input": {"command": command}})
        self.assertFalse(denied(r))
        self.assertNotIn("decision", r)
        return r.get("hookSpecificOutput", {}).get("additionalContext", "")

    def test_bash_is_never_refused_before_it_runs(self):
        r = self.hook({"tool_name": "Bash", "hook_event_name": "PreToolUse", "tool_input": {"command": "echo hi >> AGENTS.md"}})
        self.assertEqual(r, {})

    def test_bash_writes_get_a_note(self):
        for c in ["sed -i '' s/a/b/ AGENTS.md", "printf x | tee -a AGENTS.md",
                  "python3 -c \"open('AGENTS.md','w').write('x')\"",
                  "python3 - <<'EOF'\nopen('AGENTS.md','w').write('x')\nEOF", "cat > AGENTS.md <<'EOF'\n- rule\nEOF"]:
            self.assertIn("AGENTS.md", self.bash(c), c)
        self.assertIn("style.md", self.bash("echo hi >> docs/style.md"))

    def test_copies_moves_deletions_and_reads_get_no_note(self):
        for c in ["cat AGENTS.md", "grep -n rule AGENTS.md 2>/dev/null", "sed -n 1,5p docs/style.md",
                  "rm docs/style.md", "cp /tmp/theirs/CLAUDE.md CLAUDE.md", "cp -r /tmp/theirs ./vendor",
                  "mv AGENTS.md OLD.md", "git clone https://example.com/x.git", "echo hi > notes.md",
                  "diff -u notes.md AGENTS.md > out.diff", "git commit -F - <<'EOF'\nUpdate AGENTS.md and CLAUDE.md\nEOF"]:
            self.assertEqual(self.bash(c), "", c)

    def test_bash_paths_follow_cd(self):
        (self.root / "sub").mkdir()
        self.assertIn(str(Path("sub") / "AGENTS.md"), self.bash("cd sub && echo hi >> AGENTS.md"))
        self.assertIn("style.md", self.bash("cd docs && sed -i.bak s/a/b/ style.md"))

    def test_codex_bash_note_names_apply_patch(self):
        self.assertIn("apply_patch", self.bash("echo hi >> AGENTS.md", "/home/u/.codex/sessions/x.jsonl"))

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

    def transcript(self, *rows: dict) -> list[str]:
        path = self.root / "session.jsonl"
        path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
        return self.guard.user_messages(str(path))

    def test_user_messages_from_claude_transcript(self):
        said = self.transcript(
            {"type": "user", "message": {"role": "user", "content": "벤 다이어그램 예시는 남겨 둬."}},
            {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]}},
            {"type": "user", "isMeta": True, "message": {"role": "user", "content": "meta"}},
            {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "<system-reminder>x</system-reminder>"},
                                                                      {"type": "text", "text": "그대로 두고 커밋해."}]}},
            {"type": "assistant", "message": {"role": "assistant", "content": "done"}})
        self.assertEqual(said, ["벤 다이어그램 예시는 남겨 둬.", "그대로 두고 커밋해."])

    def test_user_messages_from_codex_transcript(self):
        def user(text):
            return {"type": "response_item", "payload": {"type": "message", "role": "user",
                                                         "content": [{"type": "input_text", "text": text}]}}
        said = self.transcript(user("# AGENTS.md instructions for /tmp\n..."), user("<environment_context>x"),
                               user("여백은 넉넉히 두라고 적어 줘."))
        self.assertEqual(said, ["여백은 넉넉히 두라고 적어 줘."])
        self.assertEqual(self.guard.user_messages(str(self.root / "missing.jsonl")), [])

    def test_line_the_user_wrote_out_is_never_refused(self):
        path = self.write("# Rules\nx\n")
        line = "- 10월 5일 이후 만든 영상은 자막을 두 줄로 쓴다."
        said = [f"AGENTS.md에 '{line}' 줄을 넣어 줘."]
        edit = ("Edit", {"file_path": str(path), "old_string": "x", "new_string": line})
        self.assertIsNotNone(self.review(*edit))
        self.answer = {"ok": False, "problems": [{"line": line, "kind": "provenance", "fix": "drop the date"}]}
        [(p, before, after)] = self.guard.claude_changes(*edit, str(self.root))
        self.assertIsNone(self.guard.review("claude", p, before, after, said))

    def test_judge_sees_the_user_messages(self):
        path = self.write("# Rules\nx\n")
        [(p, before, after)] = self.guard.claude_changes(
            "Edit", {"file_path": str(path), "old_string": "x", "new_string": "- Keep the Venn diagram example."},
            str(self.root))
        self.guard.review("claude", p, before, after, ["Keep the example as it is."])
        self.assertEqual(self.section(self.messages[0] + "\n\n[", "USER MESSAGES"), "---\nKeep the example as it is.")

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
