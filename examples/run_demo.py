"""30-second demo: three "All done!" turns, what honest-done says, and one honest turn it lets through.

    python examples/run_demo.py

Each scenario is a bench case (bench/cases/<name>): a temp git repo is built, the real
UserPromptSubmit and Stop hooks run against it with a real-format transcript, and the
hook's own output is printed. Nothing in this repository is modified.
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bench"))
import run_bench  # noqa: E402

SCENARIOS = ["test-file-deleted", "tests-pass-never-run", "existing-code-placeholder", "ok-fix-with-tests"]


def main():
    for name in SCENARIOS:
        c, verdict, reason, ms = run_bench.run_case(os.path.join(ROOT, "bench", "cases", name))
        final = ""
        with open(os.path.join(ROOT, "bench", "cases", name, "transcript.jsonl"), encoding="utf-8") as f:
            for line in f:
                for b in (json.loads(line).get("message", {}).get("content") or []):
                    if isinstance(b, dict) and b.get("type") == "text":
                        final = b["text"]
        print("-" * 78)
        print("User   : " + c["prompt"].strip().splitlines()[0])
        print("Claude : " + final)
        if verdict == "block":
            print("Stop   : BLOCKED -> " + reason)
        else:
            print("Stop   : allowed (no evidence of unfinished work)")
        print("         %.0f ms, 0 tokens" % ms)
    print("-" * 78)


if __name__ == "__main__":
    main()
