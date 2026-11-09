"""False-positive check on real code: run the diff checks over the last N commits of
real repositories, as if each commit were one Claude turn. Commits written by careful
humans should almost never be flagged; every flag is printed so you can judge it.

    python bench/real_history.py PATH_OR_URL [PATH_OR_URL ...] [--n 40]

Remote URLs are cloned into a temp folder. Your own checkouts are never modified:
each commit is checked out in a temporary `git worktree`.
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(os.path.dirname(HERE), "scripts", "honest_done.py")


def git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          check=True).stdout.decode("utf-8", "replace")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("repos", nargs="+")
    p.add_argument("--n", type=int, default=40)
    a = p.parse_args()
    tmp = tempfile.mkdtemp(prefix="hd-hist-")
    total = flagged = 0
    try:
        for i, src in enumerate(a.repos):
            repo = src
            if "://" in src:
                repo = os.path.join(tmp, "src%d" % i)
                subprocess.run(["git", "clone", "-q", src, repo], check=True)
            wt = os.path.join(tmp, "wt%d" % i)
            git(repo, "worktree", "add", "-q", "--detach", wt)
            n = f = 0
            try:
                for c in git(repo, "rev-list", "--no-merges", "HEAD").split()[:a.n]:
                    try:
                        git(repo, "rev-parse", "-q", "--verify", c + "^")
                    except subprocess.CalledProcessError:
                        continue
                    git(wt, "checkout", "-q", "--detach", c)
                    r = subprocess.run([sys.executable, HOOK, "check", "--base", c + "^"], cwd=wt,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                    n += 1
                    if r.returncode:
                        f += 1
                        print("== %s %s %s" % (os.path.basename(src.rstrip("/")), c[:9],
                                                git(repo, "log", "-1", "--format=%s", c).strip()[:60]))
                        for line in r.stdout.decode("utf-8", "replace").splitlines():
                            if line.strip().startswith("["):
                                print("   " + line.strip()[:160])
            finally:
                git(repo, "worktree", "remove", "--force", wt)
            print("%s: %d of %d commits flagged" % (src, f, n))
            total += n
            flagged += f
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\nTotal: %d of %d real commits flagged (%.0f%%)" % (flagged, total, 100.0 * flagged / max(total, 1)))


if __name__ == "__main__":
    main()
