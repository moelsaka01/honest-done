"""Runs every bench case through the real hook, exactly as Claude Code would.

For each case: build a temp git repo with the `base` files, fire the UserPromptSubmit
hook (snapshot), write the `after` files (Claude's turn), fire the Stop hook with the
case's transcript on stdin, and compare the verdict with the expected one.

    python bench/run_bench.py            # table
    python bench/run_bench.py --markdown # table for the README
"""
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HOOK = os.path.join(ROOT, "scripts", "honest_done.py")
CASES = os.path.join(HERE, "cases")


def sh(cwd, *args):
    subprocess.run(args, cwd=cwd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def write(root, files):
    for path, body in files.items():
        full = os.path.join(root, path)
        if body is None:
            os.remove(full)
            continue
        os.makedirs(os.path.dirname(full) or root, exist_ok=True)
        with open(full, "w", encoding="utf-8", newline="\n") as f:
            f.write(body)


def hook(event, payload, cwd):
    t0 = time.perf_counter()
    r = subprocess.run([sys.executable, HOOK, "hook", event], input=json.dumps(payload).encode(),
                       cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return r, (time.perf_counter() - t0) * 1000


def run_case(d):
    with open(os.path.join(d, "case.json"), encoding="utf-8") as f:
        c = json.load(f)
    transcript = os.path.join(d, "transcript.jsonl")
    final = ""
    with open(transcript, encoding="utf-8") as f:
        for line in f:
            e = json.loads(line)
            for b in e.get("message", {}).get("content", []) if isinstance(e.get("message", {}).get("content"), list) else []:
                if b.get("type") == "text":
                    final = b["text"]
    with tempfile.TemporaryDirectory(prefix="hd-bench-") as repo:
        sh(repo, "git", "init", "-q")
        sh(repo, "git", "config", "user.email", "bench@example.com")
        sh(repo, "git", "config", "user.name", "bench")
        sh(repo, "git", "config", "core.autocrlf", "false")
        write(repo, dict(c["base"], **{"README.md": "# bench\n"}))
        sh(repo, "git", "add", "-A")
        sh(repo, "git", "commit", "-q", "-m", "base")
        common = {"session_id": "bench-" + c["name"], "transcript_path": transcript, "cwd": repo,
                  "prompt_id": "prompt-1"}
        hook("prompt", dict(common, hook_event_name="UserPromptSubmit", prompt=c["prompt"]), repo)
        write(repo, c["after"])
        r, ms = hook("stop", dict(common, hook_event_name="Stop", stop_hook_active=False,
                                  last_assistant_message=final), repo)
    out = r.stdout.decode("utf-8", "replace").strip()
    verdict, reason = "allow", ""
    if out:
        j = json.loads(out)
        if j.get("decision") == "block":
            verdict, reason = "block", j.get("reason", "")
    if r.returncode not in (0,) or r.stderr.strip():
        reason += "  [stderr: %s]" % r.stderr.decode("utf-8", "replace").strip()[:200]
    return c, verdict, reason, ms


def main():
    md = "--markdown" in sys.argv
    rows = []
    for name in sorted(os.listdir(CASES)):
        d = os.path.join(CASES, name)
        if os.path.isfile(os.path.join(d, "case.json")):
            rows.append(run_case(d))
    caught = missed = fp = tn = 0
    lines = []
    for c, verdict, reason, ms in rows:
        if c["kind"] == "incomplete":
            ok = verdict == "block"
            caught += ok
            missed += not ok
            res = "caught" if ok else "MISSED"
        else:
            ok = verdict == "allow"
            tn += ok
            fp += not ok
            res = "allowed" if ok else "FALSE POSITIVE"
        lines.append((c["name"], c["kind"], c["check"], res, ms, reason))
    inc = caught + missed
    com = tn + fp
    times = sorted(r[4] for r in lines)
    if md:
        print("| Case | Type | Check | Result | Time |")
        print("|---|---|---|---|---|")
        for n, k, ch, res, ms, _ in lines:
            print("| `%s` | %s | %s | %s | %.0f ms |" % (n, k, ch, "**%s**" % res if res.isupper() else res, ms))
    else:
        w = max(len(l[0]) for l in lines)
        print("%-*s  %-10s  %-18s  %-15s  %s" % (w, "case", "type", "check", "result", "time"))
        print("-" * (w + 60))
        for n, k, ch, res, ms, reason in lines:
            print("%-*s  %-10s  %-18s  %-15s  %4.0f ms" % (w, n, k, ch, res, ms))
            if reason and ("-v" in sys.argv or res in ("MISSED", "FALSE POSITIVE") or k == "incomplete"):
                print("    -> %s" % reason[:220])
    print()
    print("Incomplete turns caught : %d / %d" % (caught, inc))
    print("Missed                  : %d" % missed)
    print("False positives         : %d / %d complete turns" % (fp, com))
    print("LLM calls / extra tokens: 0 / 0 (the hook imports no network module)")
    print("Stop hook time          : median %.0f ms, max %.0f ms (includes Python start-up and git)"
          % (times[len(times) // 2], times[-1]))
    return 0 if (missed == 0 and fp == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
