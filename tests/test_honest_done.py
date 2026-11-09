"""Hook protocol, loop guard, config, CLI and the bench itself."""
import ast
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(ROOT, "scripts", "honest_done.py")
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import honest_done as hd  # noqa: E402


def load(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def run(args, payload=None, cwd=None):
    r = subprocess.run([sys.executable, HOOK, *args], cwd=cwd, input=json.dumps(payload or {}).encode(),
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")


class Repo:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hd-test-")
        self.path = self.tmp.name
        for a in (["init", "-q"], ["config", "user.email", "t@e.st"], ["config", "user.name", "t"],
                  ["config", "core.autocrlf", "false"]):
            subprocess.run(["git", *a], cwd=self.path, check=True)
        self.write("calc.py", "def add(a, b):\n    return a + b\n")
        subprocess.run(["git", "add", "-A"], cwd=self.path, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=self.path, check=True)
        self.transcript = os.path.join(self.path, ".t.jsonl")
        with open(os.path.join(self.path, ".git", "info", "exclude"), "a") as f:
            f.write(".t.jsonl\n")
        self.say("Done.")

    def write(self, rel, body):
        with open(os.path.join(self.path, rel), "w", encoding="utf-8", newline="\n") as f:
            f.write(body)

    def say(self, final, prompt="Edit calc.py"):
        lines = [{"type": "user", "message": {"role": "user", "content": prompt}},
                 {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": final}]}}]
        with open(self.transcript, "w", encoding="utf-8") as f:
            f.write("\n".join(json.dumps(l) for l in lines) + "\n")

    def payload(self, **kw):
        p = {"session_id": "s1", "cwd": self.path, "transcript_path": self.transcript, "stop_hook_active": False}
        p.update(kw)
        return p

    def prompt(self, text="Edit calc.py"):
        return run(["hook", "prompt"], self.payload(prompt=text, hook_event_name="UserPromptSubmit"))

    def stop(self, **kw):
        code, out, err = run(["hook", "stop"], self.payload(hook_event_name="Stop", **kw))
        return (json.loads(out) if out.strip() else None), code, err


class HookProtocol(unittest.TestCase):
    def setUp(self):
        self.r = Repo()
        self.addCleanup(self.r.tmp.cleanup)

    def test_clean_turn_allows(self):
        self.r.prompt()
        self.r.write("calc.py", "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n")
        out, code, err = self.r.stop()
        self.assertIsNone(out)
        self.assertEqual(code, 0)
        self.assertEqual(err, "")

    def test_block_format_is_the_documented_one(self):
        self.r.prompt()
        self.r.write("calc.py", "def add(a, b):\n    # TODO: overflow\n    return a + b\n")
        out, code, _ = self.r.stop()
        self.assertEqual(code, 0)
        self.assertEqual(set(out), {"decision", "reason"})
        self.assertEqual(out["decision"], "block")
        self.assertTrue(out["reason"].startswith("honest-done: TODO added at calc.py:2"))

    def test_same_reason_is_never_given_twice(self):
        self.r.prompt()
        self.r.write("calc.py", "def add(a, b):\n    # TODO: overflow\n    return a + b\n")
        self.assertIsNotNone(self.r.stop()[0])
        self.assertIsNone(self.r.stop(stop_hook_active=True)[0])

    def test_max_blocks_caps_a_turn(self):
        self.r.prompt()
        self.r.write("calc.py", "def add(a, b):\n    # TODO: a\n    # FIXME: b\n    # XXX: c\n    return a + b\n")
        self.assertIsNotNone(self.r.stop()[0])
        self.assertIsNotNone(self.r.stop(stop_hook_active=True)[0])     # a different, new reason
        self.assertIsNone(self.r.stop(stop_hook_active=True)[0])        # default max_blocks = 2

    def test_new_prompt_resets_the_counter(self):
        self.r.prompt()
        self.r.write("calc.py", "def add(a, b):\n    # TODO: a\n    return a + b\n")
        self.assertIsNotNone(self.r.stop()[0])
        self.r.prompt("Next task")
        self.r.write("calc.py", "def add(a, b):\n    # TODO: a\n    # TODO: b\n    return a + b\n")
        out = self.r.stop()[0]
        self.assertIn("calc.py:3", out["reason"])                     # only this turn's change

    def test_stop_hook_active_without_state_allows(self):
        self.r.write("calc.py", "def add(a, b):\n    # TODO: a\n    return a + b\n")
        self.assertIsNone(self.r.stop(stop_hook_active=True)[0])

    def test_without_snapshot_falls_back_to_head(self):
        self.r.write("calc.py", "def add(a, b):\n    # TODO: a\n    return a + b\n")
        self.assertIsNotNone(self.r.stop()[0])

    def test_config_disables_a_check(self):
        self.r.write(".honest-done.json", json.dumps({"checks": {"unfinished_code": False}}))
        self.r.prompt()
        self.r.write("calc.py", "def add(a, b):\n    # TODO: a\n    return a + b\n")
        self.assertIsNone(self.r.stop()[0])

    def test_config_ignore_paths(self):
        self.r.write(".honest-done.json", json.dumps({"ignore": ["legacy/*"]}))
        os.makedirs(os.path.join(self.r.path, "legacy"))
        self.r.prompt()
        self.r.write("legacy/old.py", "def f():\n    # TODO: someday\n    return 1\n")
        self.assertIsNone(self.r.stop()[0])

    def test_inline_ignore_marker(self):
        self.r.prompt()
        self.r.write("calc.py", "def add(a, b):\n    # TODO: overflow  honest-done: ignore\n    return a + b\n")
        self.assertIsNone(self.r.stop()[0])

    def test_broken_config_never_breaks_the_session(self):
        self.r.write(".honest-done.json", "{not json")
        self.r.prompt()
        out, code, err = self.r.stop()
        self.assertEqual(code, 0)
        self.assertIn("invalid .honest-done.json", err)

    def test_garbage_stdin_allows(self):
        code, out, _ = run(["hook", "stop"], None)
        self.assertEqual((code, out), (0, ""))
        r = subprocess.run([sys.executable, HOOK, "hook", "stop"], input=b"\xff not json", stdout=subprocess.PIPE)
        self.assertEqual((r.returncode, r.stdout), (0, b""))

    def test_not_a_git_repo(self):
        with tempfile.TemporaryDirectory() as d:
            code, out, _ = run(["hook", "stop"], {"cwd": d, "session_id": "nogit-%d" % os.getpid(), "last_assistant_message": "All tests pass."})
            self.assertEqual(code, 0)
            self.assertIn("no test command ran", json.loads(out)["reason"])

    def test_user_index_is_never_touched(self):
        idx = os.path.join(self.r.path, ".git", "index")
        with open(idx, "rb") as f:
            before = f.read()
        self.r.write("new.py", "x = 1\n")
        self.r.prompt()
        self.r.stop()
        with open(idx, "rb") as f:
            self.assertEqual(f.read(), before)

    def test_cli_check(self):
        self.r.write("calc.py", "def add(a, b):\n    breakpoint()\n    return a + b\n")
        code, out, _ = run(["check", "--repo", self.r.path])
        self.assertEqual(code, 1)
        self.assertIn("[leftover_debug]", out)
        code, out, _ = run(["check", "--repo", self.r.path, "--json"])
        self.assertEqual(json.loads(out)["verdict"], "block")


class Unit(unittest.TestCase):
    def claim(self, text):
        t = hd.Turn()
        t.final_text = text
        cfg = json.loads(json.dumps(hd.DEFAULT_CONFIG))
        return hd.check_claims({"turn": t, "cfg": cfg, "files": [], "root": "."})

    def test_claims_and_hedges(self):
        for s in ["All tests pass.", "The 12 tests now pass!", "Tests are green.", "The build succeeds."]:
            self.assertTrue(self.claim(s), s)
        for s in ["I did not run the tests.", "Tests should pass once you run them.", "The tests don't pass yet.",
                  "test_x now fails, so the suite is not green.", "Run `pytest` to check the tests pass."]:
            self.assertFalse(self.claim(s), s)

    def test_strings_are_not_skips(self):
        self.assertFalse(hd.SKIP_RE.search(hd._code_only('x = "@pytest.mark.skip"')))
        self.assertTrue(hd.SKIP_RE.search(hd._code_only('test.skip("name", () => {')))
        self.assertFalse(hd.SKIP_RE.search("model.fit(X, y)"))
        self.assertFalse(hd.SKIP_RE.search('@unittest.skipUnless(NODE, "needs node")'))

    def test_rule_specific_suppressions_are_allowed(self):
        self.assertFalse(hd.SUPPRESS_RE.search("f = lambda: 1  # noqa: E731"))
        self.assertFalse(hd.SUPPRESS_RE.search("x = y  # type: ignore[attr-defined]"))
        self.assertTrue(hd.SUPPRESS_RE.search("x = y  # type: ignore"))
        self.assertTrue(hd.SUPPRESS_RE.search("// @ts-ignore"))

    def test_no_network_or_llm(self):
        tree = ast.parse(load(HOOK))
        mods = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        mods |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        self.assertFalse(mods & {"socket", "urllib", "http", "requests", "ssl", "anthropic", "openai", "asyncio"}, mods)


class Bench(unittest.TestCase):
    def test_bench_has_no_misses_and_no_false_positives(self):
        r = subprocess.run([sys.executable, os.path.join(ROOT, "bench", "run_bench.py")],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertEqual(r.returncode, 0, r.stdout.decode("utf-8", "replace")[-2000:])


class Packaging(unittest.TestCase):
    def test_plugin_files(self):
        plugin = json.loads(load(os.path.join(ROOT, ".claude-plugin", "plugin.json")))
        market = json.loads(load(os.path.join(ROOT, ".claude-plugin", "marketplace.json")))
        hooks = json.loads(load(os.path.join(ROOT, "hooks", "hooks.json")))
        self.assertEqual(plugin["name"], "honest-done")
        self.assertEqual(plugin["version"], hd.VERSION)
        self.assertEqual(market["plugins"][0]["name"], "honest-done")
        self.assertEqual(set(hooks["hooks"]), {"UserPromptSubmit", "Stop"})
        for ev in hooks["hooks"].values():
            self.assertIn("${CLAUDE_PLUGIN_ROOT}/bin/honest-done", ev[0]["hooks"][0]["command"])


if __name__ == "__main__":
    unittest.main()
