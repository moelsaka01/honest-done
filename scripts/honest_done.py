#!/usr/bin/env python3
"""honest-done: Claude said it's done. Prove it.

A Claude Code Stop hook that blocks the end of a turn only when it finds
concrete evidence of unfinished work. Pure code: no LLM calls, no network,
no extra tokens unless it blocks (then one short sentence).

Usage
  honest_done.py hook prompt     UserPromptSubmit: snapshot the working tree
  honest_done.py hook stop       Stop: check this turn, block with one reason
  honest_done.py check [--transcript T] [--repo R] [--base REV] [--json]
                                 run every check by hand and print a report

Standard library only, Python 3.8+. Works on Windows, macOS and Linux.
"""
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

VERSION = "0.1.0"
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
PREFIX = "honest-done: "

DEFAULT_CONFIG = {
    "enabled": True,
    "checks": {
        "unfinished_code": True,
        "unverified_claims": True,
        "silent_shortcuts": True,
        "skipped_context": True,
        "partial_task_list": True,
        "leftover_debug": True,
    },
    "ignore": [],          # glob patterns, matched against repo-relative paths
    "max_blocks": 2,       # never block more than this many times per turn
    "strict": False,       # stricter claim checks, flag print() everywhere
}

# Order = priority. The first finding is the one Claude is told about.
CHECK_ORDER = ["silent_shortcuts", "unverified_claims", "unfinished_code",
               "partial_task_list", "skipped_context", "leftover_debug"]

CODE_EXT = {".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".rs",
            ".java", ".kt", ".kts", ".rb", ".php", ".cs", ".c", ".h", ".cc", ".cpp",
            ".hpp", ".swift", ".scala", ".vue", ".svelte", ".sh", ".lua", ".dart"}
VENDOR_DIRS = ("node_modules/", "vendor/", "dist/", "build/", ".venv/", "venv/",
               "__pycache__/", "third_party/", ".git/")
IGNORE_MARK = "honest-done: ignore"

TEST_FILE = re.compile(
    r"(^|/)(tests?|__tests__|spec|specs|testing)/"
    r"|(^|/)test_[^/]*\.py$|_test\.(py|go|rb|exs?)$|(^|/)conftest\.py$"
    r"|\.(test|spec)\.[cm]?[jt]sx?$|(Test|Tests|Spec)\.(java|kt|cs|swift|scala)$|_spec\.rb$")


# --------------------------------------------------------------------------- utils

def is_test_path(path):
    return bool(TEST_FILE.search(path.replace("\\", "/")))


def ext_of(path):
    return os.path.splitext(path)[1].lower()


def is_code(path):
    p = path.replace("\\", "/")
    return ext_of(p) in CODE_EXT and not any(("/" + p).find("/" + d) >= 0 for d in VENDOR_DIRS)


def git(cwd, *args, env=None, check=True):
    r = subprocess.run(["git", *args], cwd=cwd, env=env, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE)
    if check and r.returncode != 0:
        raise RuntimeError("git %s failed: %s" % (args[0], r.stderr.decode("utf-8", "replace")[:200]))
    return r.stdout.decode("utf-8", "replace")


_GIT_DIRS = {}


def git_root(cwd):
    """One git call for both the work tree root and the git dir (each git spawn costs
    ~100 ms on Windows, so the hook keeps them to four per Stop)."""
    try:
        out = git(cwd, "rev-parse", "--show-toplevel", "--absolute-git-dir").splitlines()
    except Exception:
        return None
    if len(out) < 2 or not out[0].strip():
        return None
    _GIT_DIRS[out[0].strip()] = out[1].strip()
    return out[0].strip()


def git_dir(root):
    if root not in _GIT_DIRS:
        _GIT_DIRS[root] = git(root, "rev-parse", "--absolute-git-dir").strip()
    return _GIT_DIRS[root]


def load_config(root):
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if not root:
        return cfg
    path = os.path.join(root, ".honest-done.json")
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                user = json.load(f)
            for k, v in user.items():
                if k == "checks" and isinstance(v, dict):
                    cfg["checks"].update(v)
                else:
                    cfg[k] = v
        except Exception as e:  # a broken config must never break the session
            sys.stderr.write(PREFIX + "ignoring invalid .honest-done.json (%s)\n" % e)
    return cfg


def ignored(path, cfg):
    return any(fnmatch.fnmatch(path, pat) or fnmatch.fnmatch(path, pat.rstrip("/") + "/*")
               for pat in cfg.get("ignore", []))


# ------------------------------------------------------------------- state / snapshot

def state_dir(root):
    if root:
        try:
            d = os.path.join(git_dir(root), "honest-done")
            os.makedirs(d, exist_ok=True)
            return d
        except Exception:
            pass
    d = os.path.join(tempfile.gettempdir(), "honest-done")
    os.makedirs(d, exist_ok=True)
    return d


def state_path(root, session_id, cwd):
    key = re.sub(r"[^A-Za-z0-9_-]", "", session_id or "") or \
        re.sub(r"[^A-Za-z0-9]", "_", cwd or "default")[-60:]
    return os.path.join(state_dir(root), "state-%s.json" % key)


def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def worktree_tree(root):
    """Hash the whole working tree (tracked + untracked, .gitignore respected) into a
    git tree object without touching the user's index. Copies the real index first so
    unchanged files are not re-hashed: fast on large repos."""
    gd = git_dir(root)
    fd, tmp_index = tempfile.mkstemp(prefix="honest-done-index-")
    os.close(fd)
    try:
        real = os.path.join(gd, "index")
        if os.path.isfile(real):
            shutil.copyfile(real, tmp_index)
        else:
            os.remove(tmp_index)
        env = dict(os.environ, GIT_INDEX_FILE=tmp_index)
        git(root, "add", "-A", "--ignore-errors", ".", env=env, check=False)
        return git(root, "write-tree", env=env).strip()
    finally:
        try:
            os.remove(tmp_index)
        except OSError:
            pass


def head_tree(root):
    r = subprocess.run(["git", "rev-parse", "--verify", "-q", "HEAD^{tree}"], cwd=root,
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return r.stdout.decode().strip() or EMPTY_TREE


# ------------------------------------------------------------------------------ diff

class FileDiff:
    def __init__(self, path):
        self.path = path
        self.status = "M"
        self.added = []     # (new_lineno, text)
        self.removed = []   # (old_lineno, text)


def parse_diff(text):
    files, cur, new_ln, old_ln = [], None, 0, 0
    for line in text.splitlines():
        if line.startswith("diff --git "):
            m = re.match(r'diff --git "?a/(.*?)"? "?b/(.*?)"?$', line)
            cur = FileDiff(m.group(2) if m else line.split(" b/")[-1])
            files.append(cur)
        elif cur is None:
            continue
        elif line.startswith("new file mode"):
            cur.status = "A"
        elif line.startswith("deleted file mode"):
            cur.status = "D"
        elif line.startswith("rename from "):
            cur.status = "R"
        elif line.startswith("@@"):
            m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
            if m:
                old_ln, new_ln = int(m.group(1)), int(m.group(2))
        elif line.startswith("+++") or line.startswith("---"):
            if line.startswith("--- a/") and cur.status == "D":
                cur.path = line[6:]
            continue
        elif line.startswith("+"):
            cur.added.append((new_ln, line[1:]))
            new_ln += 1
        elif line.startswith("-"):
            cur.removed.append((old_ln, line[1:]))
            old_ln += 1
    return files


def turn_diff(root, base_tree):
    cur = worktree_tree(root)
    if cur == base_tree:
        return []
    out = git(root, "-c", "core.quotepath=off", "diff", "--no-color", "--no-ext-diff",
              "-U0", "-M", base_tree, cur)
    return parse_diff(out)


# ------------------------------------------------------------------------ transcript

def _text_of(content):
    if isinstance(content, str):
        return content
    out = []
    for b in content or []:
        if isinstance(b, dict) and b.get("type") == "text":
            out.append(b.get("text", ""))
        elif isinstance(b, str):
            out.append(b)
    return "\n".join(out)


def _is_real_prompt(e):
    if e.get("type") != "user" or e.get("isMeta") or e.get("isCompactSummary"):
        return False
    c = (e.get("message") or {}).get("content")
    if isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
        return False
    t = _text_of(c).strip()
    return bool(t) and PREFIX not in t and not t.startswith("Stop hook feedback")


class Turn:
    def __init__(self):
        self.prompt = ""
        self.tool_uses = []        # dicts: name, input, id, ok, output
        self.final_text = ""
        self.texts = []


def read_transcript(path):
    entries = []
    if not path or not os.path.isfile(path):
        return entries
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except ValueError:
                    pass
    return entries


def extract_turn(entries, prompt_id=None, prompt_text=None):
    """Everything after the last real user prompt (or the one with prompt_id)."""
    start = None
    for i in range(len(entries) - 1, -1, -1):
        e = entries[i]
        if not _is_real_prompt(e):
            continue
        if prompt_id and e.get("promptId") and e.get("promptId") != prompt_id:
            continue
        start = i
        break
    if start is None and prompt_id:
        return extract_turn(entries, None, prompt_text)
    t = Turn()
    if start is None:
        t.prompt = prompt_text or ""
        return t
    t.prompt = _text_of(entries[start]["message"].get("content"))
    by_id = {}
    for e in entries[start + 1:]:
        if e.get("isSidechain"):
            continue
        msg = e.get("message") or {}
        content = msg.get("content")
        if e.get("type") == "assistant" and isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    tu = {"name": b.get("name", ""), "input": b.get("input") or {},
                          "id": b.get("id"), "ok": None, "output": ""}
                    t.tool_uses.append(tu)
                    by_id[tu["id"]] = tu
                elif b.get("type") == "text" and b.get("text", "").strip():
                    t.texts.append(b["text"])
                    t.final_text = b["text"]
        elif e.get("type") == "user" and isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    tu = by_id.get(b.get("tool_use_id"))
                    if tu is None:
                        continue
                    out = _text_of(b.get("content"))
                    tu["output"] = out
                    m = re.match(r"\s*(?:Error: )?Exit code (\d+)", out)
                    tu["ok"] = not (b.get("is_error") or (m and m.group(1) != "0"))
    return t


def command_of(tu):
    inp = tu["input"]
    if isinstance(inp, dict):
        return str(inp.get("command") or inp.get("cmd") or "")
    return ""


# ----------------------------------------------------------------------------- checks

class Finding:
    def __init__(self, check, message, path=None, line=None):
        self.check, self.message, self.path, self.line = check, message, path, line

    def where(self):
        if self.path and self.line:
            return "%s:%s" % (self.path, self.line)
        return self.path or ""

    def as_dict(self):
        return {"check": self.check, "message": self.message, "path": self.path, "line": self.line}


def _added_code(files, cfg, tests=None):
    """Yield (FileDiff, lineno, text) for added lines in code files."""
    for fd in files:
        if fd.status == "D" or not is_code(fd.path) or ignored(fd.path, cfg):
            continue
        if tests is not None and is_test_path(fd.path) != tests:
            continue
        removed = {t.strip() for _, t in fd.removed}
        for ln, text in fd.added:
            if IGNORE_MARK in text or text.strip() in removed:
                continue   # moved / re-indented lines are not new
            yield fd, ln, text


def _read_lines(root, path):
    try:
        with open(os.path.join(root, path), encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()
    except OSError:
        return []


COMMENT = r"(?:#|//|/\*|\*|<!--|--)"
TODO_RE = re.compile(COMMENT + r".*?\b(TODO|FIXME|XXX)\b")
PLACEHOLDER_RE = re.compile(
    COMMENT + r"\s*(?:\.\.\.\s*(?:existing|rest|remaining|other|previous|more|same)\b"
    r"|(?:the )?rest of (?:the )?(?:code|file|implementation|function|logic|methods)"
    r"|existing code\b|(?:your |actual |real )?implementation (?:goes )?here"
    r"|(?:add|put|insert) (?:your |the )?(?:code|logic|implementation) here"
    r"|(?:same|unchanged) as (?:before|above)|\.\.\.\s*$)", re.I)
NOT_IMPL_PY = re.compile(r"\braise\s+NotImplementedError\b")
NOT_IMPL_JS = re.compile(r"""throw\s+new\s+\w*Error\(\s*['"`][^'"`]*not\s+(?:yet\s+)?implemented""", re.I)
NOT_IMPL_OTHER = re.compile(r"\b(?:todo!\(|unimplemented!\(|panic\(\s*\"not implemented|"
                            r"throw\s+new\s+(?:UnsupportedOperation|NotImplemented)\w*\()")
PY_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(\w+)\s*\(")
PY_STUB_BODY = re.compile(r"^\s*(?:pass|\.\.\.)\s*(?:#.*)?$")
JS_EMPTY_FN = re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s*\*?\s*(\w+)\s*\([^)]*\)\s*(?::\s*[\w<>\[\]| ]+)?\s*\{\s*\}\s*;?\s*$")


def _abstract_context(lines, idx):
    """True when the def at 0-based idx is abstract/overload/protocol, where a stub is fine."""
    for j in range(max(0, idx - 4), idx):
        if re.search(r"@(?:abc\.)?abstract|@overload|@(?:typing\.)?override", lines[j]):
            return True
    for j in range(idx - 1, -1, -1):
        m = re.match(r"^class\s+\w+\s*\((.*)\)\s*:", lines[j])
        if m:
            return bool(re.search(r"\b(Protocol|ABC|ABCMeta|Interface)\b", m.group(1)))
        if re.match(r"^\S", lines[j]) and not lines[j].lstrip().startswith(("@", "#")):
            return False
    return False


STUB_REQUESTED = re.compile(r"\b(?:stubs?|stubbed|placeholders?|skeleton|scaffold\w*|NotImplementedError|TODOs?|"
                            r"not implemented|implement (?:it|them|this) later|fill (?:it|them|this) in later)\b", re.I)


def check_unfinished(ctx):
    out, cfg, root = [], ctx["cfg"], ctx["root"]
    if ctx["turn"] and STUB_REQUESTED.search(ctx["turn"].prompt or "") and not cfg.get("strict"):
        return out      # the user asked for stubs / TODOs this turn
    for fd, ln, text in _added_code(ctx["files"], cfg):
        s = text.strip()
        if TODO_RE.search(text):
            word = TODO_RE.search(text).group(1)
            out.append(Finding("unfinished_code", "%s added at %s:%d (\"%s\"). Finish it or remove the marker."
                               % (word, fd.path, ln, s[:60]), fd.path, ln))
        elif PLACEHOLDER_RE.search(text) and not is_test_path(fd.path):
            out.append(Finding("unfinished_code", "placeholder comment at %s:%d (\"%s\"): the real code is missing."
                               % (fd.path, ln, s[:60]), fd.path, ln))
        elif NOT_IMPL_JS.search(text) or NOT_IMPL_OTHER.search(text):
            out.append(Finding("unfinished_code", "%s:%d throws \"not implemented\". Implement it."
                               % (fd.path, ln), fd.path, ln))
        elif NOT_IMPL_PY.search(text) and ext_of(fd.path) in (".py",):
            lines = _read_lines(root, fd.path)
            if not _abstract_context(lines, ln - 1):
                out.append(Finding("unfinished_code", "%s:%d raises NotImplementedError. Implement it."
                                   % (fd.path, ln), fd.path, ln))
        elif JS_EMPTY_FN.match(text) and not is_test_path(fd.path):
            name = JS_EMPTY_FN.match(text).group(1)
            out.append(Finding("unfinished_code", "function %s() at %s:%d has an empty body."
                               % (name, fd.path, ln), fd.path, ln))
    # Python stubs: an added def whose only body line is `pass` or `...`
    for fd in ctx["files"]:
        if fd.status == "D" or ext_of(fd.path) != ".py" or ignored(fd.path, cfg) or is_test_path(fd.path):
            continue
        added = dict(fd.added)
        lines = None
        for ln, text in fd.added:
            m = PY_DEF.match(text)
            if not m or not text.rstrip().endswith(":"):
                continue
            nxt = ln + 1
            while nxt in added and (not added[nxt].strip() or added[nxt].strip().startswith(("#", '"""', "'''"))
                                    and added[nxt].strip().count('"""') != 1):
                nxt += 1
            if nxt not in added or not PY_STUB_BODY.match(added[nxt]):
                continue
            after = added.get(nxt + 1)
            indent = len(added[nxt]) - len(added[nxt].lstrip())
            if after is not None and after.strip() and len(after) - len(after.lstrip()) >= indent:
                continue   # body continues: not a stub
            if lines is None:
                lines = _read_lines(root, fd.path)
            if _abstract_context(lines, ln - 1):
                continue
            out.append(Finding("unfinished_code", "%s() at %s:%d is a stub (body is `%s`). Implement it."
                               % (m.group(1), fd.path, ln, added[nxt].strip()), fd.path, ln))
    return out


# Unconditional skips / focus only: skipIf/skipUnless/skipif gate on the environment and are fine.
SKIP_RE = re.compile(
    r"(?<![.\w])(?:it|test|describe|context|suite)\.(?:skip|only)\s*\(|(?<![.\w])x(?:it|describe|test)\s*\("
    r"|(?<![.\w])f(?:it|describe)\s*\(|@pytest\.mark\.(?:skip|xfail)\b(?!if)|@unittest\.(?:skip|expectedFailure)\b(?!If|Unless)"
    r"|#\[ignore\]|@Disabled\b|@Ignore\b")
SKIP_STRICT = re.compile(r"\bpytest\.(?:skip|xfail)\s*\(|\bself\.skipTest\s*\(|\bt\.Skip(?:Now|f)?\s*\(|@pytest\.mark\.skipif")
# Blanket suppressions. A rule-specific one (`# noqa: E731`, `# type: ignore[attr]`,
# `eslint-disable-next-line no-console`) is a deliberate decision and is only flagged in strict mode.
SUPPRESS_RE = re.compile(
    r"@ts-ignore|@ts-nocheck|eslint-disable(?:-next-line|-line)?\s*(?:\*/|-->|$)|#\s*noqa\s*(?:$|[^:\s])"
    r"|#\s*type:\s*ignore(?!\[)|//\s*nolint\s*$|#\s*pylint:\s*disable\s*=\s*all|@phpstan-ignore-line|//\s*NOSONAR\s*$")
SUPPRESS_STRICT = re.compile(
    r"@ts-expect-error|eslint-disable|#\s*noqa\b|#\s*type:\s*ignore|#\s*pylint:\s*disable|//\s*nolint\b|@SuppressWarnings"
    r"|#!?\[allow\(|#\s*pragma:\s*no\s*cover|istanbul ignore|c8 ignore|@phpstan-ignore|rubocop:disable")
ASSERT_RE = re.compile(
    r"\bassert\w*\b|\bexpect\s*\(|\.should\b|\bt\.(?:Error|Errorf|Fatal|Fatalf|Fail)\b|\brequire\.\w+\(|\bassert_eq!|\bassert!")
# An explained swallow (`except Exception:  # best effort`) is allowed; a silent one is not.
PY_SWALLOW_1 = re.compile(r"^\s*except\s*(?:\(?\s*(?:Exception|BaseException)\s*\)?(?:\s+as\s+\w+)?)?\s*:\s*(?:pass|\.\.\.)\s*$")
PY_EXCEPT = re.compile(r"^\s*except\s*(?:\(?\s*(?:Exception|BaseException)\s*\)?(?:\s+as\s+\w+)?)?\s*:\s*$")
PY_BARE_EXCEPT = re.compile(r"^\s*except\s*:")
PY_SILENT = re.compile(r"^\s*(?:pass|\.\.\.)\s*$")
JS_EMPTY_CATCH = re.compile(r"catch\s*(?:\([^)]*\))?\s*\{\s*\}")
JS_CATCH_OPEN = re.compile(r"catch\s*(?:\([^)]*\))?\s*\{\s*$")
STRING_LIT = re.compile(r'"(?:[^"\\\n]|\\.)*"' + r"|'(?:[^'\\\n]|\\.)*'|`(?:[^`\\\n]|\\.)*`")


def _code_only(text):
    """Blank out string literal contents so `"@pytest.mark.skip"` in a string is not a skip."""
    return STRING_LIT.sub(lambda m: m.group(0)[0] * 2, text)


def _wraps_old_code(fd, except_ln):
    """True when the try-block above this except re-indents lines that existed before the turn:
    the "make the crash go away" pattern, as opposed to new best-effort cleanup code."""
    added = dict(fd.added)
    removed = {t.strip() for _, t in fd.removed if t.strip()}
    ln = except_ln - 1
    body = []
    while ln in added and not re.match(r"^\s*try\s*:", added[ln]):
        body.append(added[ln].strip())
        ln -= 1
    return any(b and b in removed for b in body)


def check_shortcuts(ctx):
    out, cfg = [], ctx["cfg"]
    # Lines re-added anywhere in the diff: tests split into another file are moved, not lost.
    readded = {t.strip() for fd in ctx["files"] for _, t in fd.added if len(t.strip()) > 8}
    for fd in ctx["files"]:
        if ignored(fd.path, cfg):
            continue
        if fd.status == "D" and is_test_path(fd.path) and is_code(fd.path):
            body = [t.strip() for _, t in fd.removed if len(t.strip()) > 8]
            if body and sum(t in readded for t in body) >= len(body) / 2:
                continue
            n = sum(1 for _, t in fd.removed if ASSERT_RE.search(t))
            out.append(Finding("silent_shortcuts", "test file %s was deleted (%d assertion%s). Restore it, or say why it had to go."
                               % (fd.path, n, "" if n == 1 else "s"), fd.path))
    strict = cfg.get("strict")
    swallowed = "exception silently swallowed at %s:%d. Handle it, re-raise it, or add a comment saying why it is safe to ignore."
    for fd, ln, text in _added_code(ctx["files"], cfg):
        code = _code_only(text)
        if SKIP_RE.search(code) or (strict and SKIP_STRICT.search(code)):
            out.append(Finding("silent_shortcuts", "test skipped/focused at %s:%d (\"%s\"). Make it pass instead."
                               % (fd.path, ln, text.strip()[:60]), fd.path, ln))
        elif SUPPRESS_RE.search(code) or (strict and SUPPRESS_STRICT.search(code)):
            out.append(Finding("silent_shortcuts", "blanket checker suppression added at %s:%d (\"%s\"). Fix the underlying error."
                               % (fd.path, ln, text.strip()[:60]), fd.path, ln))
        elif (PY_SWALLOW_1.match(text) and (PY_BARE_EXCEPT.match(text) or strict or _wraps_old_code(fd, ln))) \
                or (JS_EMPTY_CATCH.search(code) and (strict or not is_test_path(fd.path))):
            out.append(Finding("silent_shortcuts", swallowed % (fd.path, ln), fd.path, ln))
    for fd in ctx["files"]:   # two-line swallow: `except:` / `catch (e) {` then pass / }
        if fd.status == "D" or not is_code(fd.path) or ignored(fd.path, cfg):
            continue
        added = dict(fd.added)
        for ln, text in fd.added:
            nxt = added.get(ln + 1, "")
            after = added.get(ln + 2, "")
            body_continues = after.strip() and len(after) - len(after.lstrip()) >= len(nxt) - len(nxt.lstrip())
            py = (PY_EXCEPT.match(text) and PY_SILENT.match(nxt) and not body_continues
                  and (PY_BARE_EXCEPT.match(text) or strict or _wraps_old_code(fd, ln)))
            js = (JS_CATCH_OPEN.search(_code_only(text)) and nxt.strip() == "}"
                  and (strict or not is_test_path(fd.path)))
            if py or js:
                out.append(Finding("silent_shortcuts", swallowed % (fd.path, ln), fd.path, ln))
    for fd in ctx["files"]:   # assertions removed from a test file that still exists
        if fd.status == "D" or not is_test_path(fd.path) or not is_code(fd.path) or ignored(fd.path, cfg):
            continue
        rem = [t for _, t in fd.removed if ASSERT_RE.search(t) and IGNORE_MARK not in t
               and not t.lstrip().startswith(("#", "//")) and t.strip() not in readded]
        add = [t for _, t in fd.added if ASSERT_RE.search(t)]
        lost = len(rem) - len(add)
        if lost >= (1 if cfg.get("strict") else 2) or (rem and not add and lost >= 1):
            out.append(Finding("silent_shortcuts", "%d assertion%s removed from %s (e.g. \"%s\"). Put them back or replace them."
                               % (lost, "" if lost == 1 else "s", fd.path, rem[0].strip()[:50]), fd.path))
    return out


TEST_CMD = re.compile(
    r"\b(?:pytest|py\.test|unittest|nose2|tox|nox|jest|vitest|mocha|ava|jasmine|karma|playwright\s+test|cypress\s+run"
    r"|go\s+test|cargo\s+(?:test|nextest)|mvnw?\b[^|;&]*\b(?:test|verify)|gradlew?\b[^|;&]*\b(?:test|check)"
    r"|rspec|minitest|phpunit|pest|dotnet\s+test|ctest|swift\s+test|mix\s+test|deno\s+test|bun\s+test"
    r"|(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?test\b|make\s+(?:test|check)|manage\.py\s+test|rake\s+test)", re.I)
BUILD_CMD = re.compile(
    r"\b(?:(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?build|tsc\b|cargo\s+(?:build|check)|go\s+(?:build|vet)|make\b"
    r"|mvnw?\b[^|;&]*\b(?:package|install|compile|verify)|gradlew?\b[^|;&]*\b(?:build|assemble)|dotnet\s+build"
    r"|python\s+-m\s+(?:build|compileall|py_compile)|vite\s+build|next\s+build|webpack|cmake\s+--build|swift\s+build|ninja)", re.I)

NEGATION = re.compile(r"\b(?:not|n't|never|no|unable|cannot|could ?n.t|without|unverified|untested|should|would|will|may|might|"
                      r"expect|once|if|until|after you|please run|to run|you can|before|check|ensure|make sure|verify|run)\b", re.I)
CLAIM_TESTS = re.compile(
    r"\b(?:all\s+|the\s+|\d+\s+|both\s+|every\s+)*(?:unit\s+|integration\s+|e2e\s+)?(?:tests?|specs?|test\s+suite|suite)"
    r"(?:\s+\w+){0,2}?\s+(?:now\s+|still\s+|all\s+)*(?:pass(?:es|ed|ing)?|green|succeed(?:s|ed)?)\b"
    r"|\b(?:tests?|suite)\s*(?:are|is)\s+(?:all\s+)?(?:passing|green)\b|✅\s*(?:all\s+)?tests", re.I)
CLAIM_BUILD = re.compile(
    r"\b(?:the\s+)?build\s+(?:now\s+)?(?:succeeds|succeeded|passes|passed|is\s+green|works|is\s+clean)\b"
    r"|\b(?:compiles|builds)\s+(?:cleanly|successfully|without\s+(?:errors|warnings))\b|\btype-?checks?\s+(?:pass|clean)", re.I)
CLAIM_VERIFIED = re.compile(r"\b(?:I(?:'ve|\s+have)?\s+)(?:verified|confirmed|tested)\b|\bverified\s+(?:that\s+)?(?:it|the\s+fix|this|everything)\s+works", re.I)
CLAIM_DONE = re.compile(r"\b(?:all\s+done|(?:it'?s|is|everything(?:'s| is))\s+(?:now\s+)?(?:done|fixed|working|complete)|fixed\s+the|task\s+(?:is\s+)?complete)\b", re.I)


# "test_x now fails" in the same sentence = Claude is reporting a failure honestly.
FAILURE_ADMITTED = re.compile(r"\b(?:still|now)\s+(?:fail(?:s|ing)?|broken|red)\b|\bfails\b", re.I)


def _sentences(text):
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    return [s for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]


def _claimed(text, pattern):
    for s in _sentences(text):
        m = pattern.search(s)
        if m and not NEGATION.search(s[:m.end()] + " " + s[m.end():m.end() + 25])                 and not FAILURE_ADMITTED.search(s):
            return s.strip()
    return None


def check_claims(ctx):
    out, turn, cfg = [], ctx["turn"], ctx["cfg"]
    text = turn.final_text if turn else ""
    if not text:
        return out
    cmds = [(command_of(tu), tu) for tu in turn.tool_uses if command_of(tu)]
    for kind, claim_re, cmd_re in (("tests", CLAIM_TESTS, TEST_CMD), ("build", CLAIM_BUILD, BUILD_CMD)):
        s = _claimed(text, claim_re)
        if not s:
            continue
        runs = [(c, tu) for c, tu in cmds if cmd_re.search(c)]
        if not runs:
            out.append(Finding("unverified_claims", "you said \"%s\" but no %s command ran this turn. Run it and report the real result."
                               % (s[:70], "test" if kind == "tests" else "build")))
        else:
            c, tu = runs[-1]
            if tu["ok"] is False:
                m = re.match(r"\s*(?:Error: )?Exit code (\d+)", tu["output"] or "")
                out.append(Finding("unverified_claims", "you said \"%s\" but the last %s run (`%s`) failed%s. Fix it or report the failure."
                                   % (s[:60], kind[:-1] if kind == "tests" else kind, c.strip()[:60],
                                      " with exit code " + m.group(1) if m else "")))
    s = _claimed(text, CLAIM_VERIFIED)
    if s and not cmds:
        out.append(Finding("unverified_claims", "you said \"%s\" but ran no command this turn. Show the evidence." % s[:70]))
    if cfg.get("strict") and not out:
        s = _claimed(text, CLAIM_DONE)
        if s and ctx["files"] and not cmds:
            out.append(Finding("unverified_claims", "you said \"%s\" after editing code but ran nothing to check it." % s[:70]))
    return out


PATH_RE = re.compile(r"(?<![\w/@.\\-])@?((?:[\w.-]+[/\\])*[\w-][\w.-]*\.[A-Za-z][A-Za-z0-9]{0,5})(?![\w/\\(])")
URL_RE = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+|\bwww\.\S+", re.I)
DONT_TOUCH = re.compile(r"\b(?:don'?t|do not|never|without|ignore|skip|except|not)\b[^.\n]{0,40}$", re.I)


def mentioned_paths(prompt, root):
    if not prompt or not root:
        return []
    clean = re.sub(r"<pasted_content.*?</pasted_content[^>]*>", " ", prompt, flags=re.S)
    clean = URL_RE.sub(" ", clean)
    found = []
    for m in PATH_RE.finditer(clean):
        raw = m.group(0)
        p = m.group(1).replace("\\", "/").strip("./") if not m.group(1).startswith("../") else None
        if not p or raw.startswith("@") or p in found:
            continue          # @file mentions are attached by Claude Code itself
        if DONT_TOUCH.search(clean[max(0, m.start() - 50):m.start()]):
            continue
        if re.fullmatch(r"\d+(\.\d+)+", p) or ext_of(p) in (".com", ".org", ".io", ".net", ".dev", ".ai"):
            continue
        if os.path.isfile(os.path.join(root, p)):
            found.append(p)
    return found


def _haystack(turn):
    parts = []
    for tu in turn.tool_uses:
        try:
            parts.append(json.dumps(tu["input"], ensure_ascii=False))
        except (TypeError, ValueError):
            parts.append(str(tu["input"]))
    return "\n".join(parts).replace("\\\\", "/").replace("\\", "/")


def check_skipped_context(ctx):
    turn, root, cfg = ctx["turn"], ctx["root"], ctx["cfg"]
    if not turn or not turn.prompt or not turn.tool_uses:
        return []     # a pure chat answer is not judged
    hay = _haystack(turn).lower()
    changed = {fd.path.lower() for fd in ctx["files"]}
    missed = []
    for p in mentioned_paths(turn.prompt, root):
        if ignored(p, cfg):
            continue
        pl = p.lower()
        if pl in changed or pl in hay or (os.path.basename(pl) in hay and len(os.path.basename(pl)) > 6):
            continue
        missed.append(p)
    if not missed:
        return []
    extra = " (and %d more)" % (len(missed) - 1) if len(missed) > 1 else ""
    return [Finding("skipped_context", "you were pointed to %s%s but never opened it this turn. Read it before calling this done."
                    % (missed[0], extra), missed[0])]


LIST_ITEM = re.compile(r"^\s{0,6}(?:\d{1,2}[.)]|[-*•]|\[ \])\s+(.{3,})$")
SPECIFIC = re.compile(r"`([^`]{2,60})`|((?:[\w.-]+/)*[\w-]+\.[A-Za-z][A-Za-z0-9]{0,5})\b|\b([a-z]+_[a-z0-9_]+|[a-z]+[A-Z]\w+|[A-Z][a-z]+[A-Z]\w+)\b")


def list_items(prompt):
    prompt = re.sub(r"```.*?```", " ", prompt or "", flags=re.S)
    blocks, cur = [], []
    for line in prompt.splitlines():
        m = LIST_ITEM.match(line)
        if m:
            cur.append(m.group(1).strip())
        elif line.strip():
            if cur:
                blocks.append(cur)
            cur = []
    if cur:
        blocks.append(cur)
    return max(blocks, key=len) if blocks else []


def check_partial_list(ctx):
    turn = ctx["turn"]
    if not turn or not ctx["files"]:
        return []           # only judge turns that changed code
    items = list_items(turn.prompt)
    if len(items) < 2:
        return []
    hay = (_haystack(turn) + "\n" + "\n".join(turn.texts)).lower()
    for fd in ctx["files"]:
        hay += "\n" + fd.path.lower() + "\n" + "\n".join(t for _, t in fd.added + fd.removed).lower()
    judged, missing = 0, []
    for idx, item in enumerate(items, 1):
        toks = set()
        for m in SPECIFIC.finditer(item):
            tok = next(g for g in m.groups() if g)
            tok = tok.strip().lower()
            if len(tok) >= 3 and not re.fullmatch(r"[\d.]+|e\.g|i\.e|etc", tok):
                toks.add(tok)
        if not toks:
            continue
        judged += 1
        if not any(t in hay or os.path.basename(t) in hay for t in toks):
            missing.append((idx, item, sorted(toks)[0]))
    if not missing or judged - len(missing) < 1:
        return []           # nothing addressed at all is ambiguous: stay quiet
    idx, item, tok = missing[0]
    more = " (%d items unaddressed)" % len(missing) if len(missing) > 1 else ""
    return [Finding("partial_task_list", "item %d of %d (\"%s\") has no trace in this turn: `%s` was never touched or mentioned%s. Do it, or say why not."
                    % (idx, len(items), item[:50], tok, more))]


DEBUG_ANY = re.compile(r"^\s*debugger\s*;?\s*$|\bbreakpoint\(\s*\)|\b(?:i?pdb|pudb)\.set_trace\(|\bimport\s+i?pdb\b|\bbinding\.pry\b"
                       r"|\bbyebug\b|\bdbg!\(|\bvar_dump\(|\bdd\(|\bconsole\.trace\(")
CONSOLE_LOG = re.compile(r"\bconsole\.(?:log|debug)\(")
PY_PRINT = re.compile(r"^\s*print\(")
CLI_HINT = re.compile(r"__main__|\bargparse\b|\bimport\s+(?:click|typer|fire)\b|\bsys\.argv\b|\bif\s+__name__")
SCRIPTY = re.compile(r"(^|/)(scripts?|bin|cli|tools?|examples?|demos?|benchmarks?|bench|docs?)/|(^|/)(cli|main|__main__|manage|setup)\.\w+$|\.config\.[cm]?[jt]s$")


def _comment_lines(lines, ext):
    """1-based line numbers inside /* */ blocks or Python docstrings (rough, string-unaware)."""
    inside, out = None, set()
    for i, line in enumerate(lines, 1):
        if inside:
            out.add(i)
            if inside in line:
                inside = None
            continue
        if ext == ".py":
            for q in ('"""', "'''"):
                if line.count(q) == 1:
                    inside = q
                    break
        elif "/*" in line and "*/" not in line[line.index("/*"):]:
            inside = "*/"
    return out


def check_debug(ctx):
    out, cfg, root, cache, comments = [], ctx["cfg"], ctx["root"], {}, {}
    for fd, ln, text in _added_code(ctx["files"], cfg, tests=False):
        s = text.strip()
        if s.startswith(("#", "//", "*")):
            continue
        if fd.path not in comments:
            comments[fd.path] = _comment_lines(_read_lines(root, fd.path), ext_of(fd.path))
        if ln in comments[fd.path]:
            continue      # e.g. a console.log inside a JSDoc usage example
        hit = None
        if DEBUG_ANY.search(text):
            hit = s
        elif SCRIPTY.search(fd.path):
            continue
        elif CONSOLE_LOG.search(text) and ext_of(fd.path) in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte"):
            if fd.status == "A" and not cfg.get("strict"):
                full = "\n".join(_read_lines(root, fd.path))
                if len(CONSOLE_LOG.findall(full)) > 3:
                    continue   # a new file that logs a lot is a logger-style script
            hit = s
        elif PY_PRINT.match(text) and ext_of(fd.path) == ".py":
            if fd.path not in cache:
                cache[fd.path] = bool(CLI_HINT.search("\n".join(_read_lines(root, fd.path))))
            if not cache[fd.path] or cfg.get("strict"):
                hit = s
        if hit:
            out.append(Finding("leftover_debug", "debug statement left in %s:%d (\"%s\"). Remove it."
                               % (fd.path, ln, hit[:60]), fd.path, ln))
    return out


CHECKS = {
    "unfinished_code": check_unfinished,
    "unverified_claims": check_claims,
    "silent_shortcuts": check_shortcuts,
    "skipped_context": check_skipped_context,
    "partial_task_list": check_partial_list,
    "leftover_debug": check_debug,
}


def run_checks(ctx):
    findings = []
    for name in CHECK_ORDER:
        if ctx["cfg"]["checks"].get(name, True):
            try:
                findings.extend(CHECKS[name](ctx))
            except Exception as e:   # one broken check must not break the others
                if os.environ.get("HONEST_DONE_DEBUG"):
                    raise
                sys.stderr.write(PREFIX + "check %s crashed: %s\n" % (name, e))
    return findings


def build_context(root, cwd, base_tree, turn, cfg):
    files = []
    if root and base_tree:
        files = [fd for fd in turn_diff(root, base_tree) if not ignored(fd.path, cfg)]
    return {"root": root or cwd, "files": files, "turn": turn, "cfg": cfg}


# ------------------------------------------------------------------------------ hooks

def _stdin_json():
    try:
        data = sys.stdin.buffer.read().decode("utf-8", "replace")
        return json.loads(data) if data.strip() else {}
    except ValueError:
        return {}


def hook_prompt(inp):
    cwd = inp.get("cwd") or os.getcwd()
    root = git_root(cwd)
    if not load_config(root).get("enabled", True):
        return 0
    state = {"session": inp.get("session_id"), "prompt_id": inp.get("prompt_id"),
             "prompt": (inp.get("prompt") or "")[:20000], "time": time.time(),
             "tree": None, "blocks": 0, "reasons": []}
    if root:
        try:
            state["tree"] = worktree_tree(root)
        except Exception:
            state["tree"] = None
    save_state(state_path(root, inp.get("session_id"), cwd), state)
    return 0


def hook_stop(inp, out=sys.stdout):
    cwd = inp.get("cwd") or os.getcwd()
    root = git_root(cwd)
    cfg = load_config(root)
    if not cfg.get("enabled", True):
        return 0
    spath = state_path(root, inp.get("session_id"), cwd)
    state = load_state(spath)
    if state is None:
        # No UserPromptSubmit snapshot (hook not installed, or first turn after install).
        if inp.get("stop_hook_active"):
            return 0          # cannot count our own blocks: be safe and allow
        state = {"tree": None, "blocks": 0, "reasons": [], "prompt": "", "prompt_id": None}
    if state.get("blocks", 0) >= int(cfg.get("max_blocks", 2)):
        return 0
    base = state.get("tree")
    if root and not base:
        base = head_tree(root)    # fallback: everything since the last commit
    entries = read_transcript(inp.get("transcript_path"))
    turn = extract_turn(entries, inp.get("prompt_id") or state.get("prompt_id"), state.get("prompt"))
    if not turn.prompt and state.get("prompt"):
        turn.prompt = state["prompt"]
    if inp.get("last_assistant_message"):
        turn.final_text = inp["last_assistant_message"]
        if not turn.texts or turn.texts[-1] != turn.final_text:
            turn.texts.append(turn.final_text)
    findings = run_checks(build_context(root, cwd, base, turn, cfg))
    given = set(state.get("reasons", []))
    fresh = [f for f in findings if f.message not in given]
    if not fresh:
        return 0              # nothing new: same reason twice means Claude already answered it
    f = fresh[0]
    more = len(fresh) - 1
    reason = PREFIX + f.message
    if more:
        reason += " (%d more issue%s after this one.)" % (more, "" if more == 1 else "s")
    reason += " If this is intentional, say so explicitly in your reply instead."
    state["blocks"] = state.get("blocks", 0) + 1
    state.setdefault("reasons", []).append(f.message)
    try:
        save_state(spath, state)
    except OSError:
        if inp.get("stop_hook_active"):
            return 0
    out.write(json.dumps({"decision": "block", "reason": reason}))
    out.flush()
    return 0


# -------------------------------------------------------------------------------- CLI

def cmd_check(args):
    cwd = os.path.abspath(args.repo or os.getcwd())
    root = git_root(cwd)
    cfg = load_config(root)
    if args.strict:
        cfg["strict"] = True
    t0 = time.perf_counter()
    base = None
    if root:
        if args.base:
            base = git(root, "rev-parse", args.base + "^{tree}").strip()
        else:
            base = head_tree(root)
    entries = read_transcript(args.transcript)
    turn = extract_turn(entries) if entries else Turn()
    if args.prompt:
        turn.prompt = args.prompt
    findings = run_checks(build_context(root, cwd, base, turn, cfg))
    ms = (time.perf_counter() - t0) * 1000
    if args.json:
        print(json.dumps({"findings": [f.as_dict() for f in findings], "ms": round(ms, 1),
                          "verdict": "block" if findings else "allow"}, indent=2))
    else:
        print("honest-done %s  repo: %s  base: %s  transcript: %s" % (
            VERSION, root or "(not a git repo)", args.base or "HEAD", args.transcript or "(none)"))
        if not findings:
            print("\nNo evidence of unfinished work. Verdict: ALLOW  (%.0f ms, 0 tokens)" % ms)
        else:
            print()
            for f in findings:
                print("  [%s] %s" % (f.check, f.message))
            print("\n%d finding%s. Verdict: BLOCK, Claude would be told: %s%s  (%.0f ms, 0 tokens)" % (
                len(findings), "" if len(findings) == 1 else "s", PREFIX, findings[0].message, ms))
    return 1 if findings else 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["hook"]:
        event = argv[1] if len(argv) > 1 else ""
        try:
            inp = _stdin_json()
            if event in ("prompt", "UserPromptSubmit"):
                return hook_prompt(inp)
            if event in ("stop", "Stop", "SubagentStop"):
                return hook_stop(inp)
        except Exception as e:     # never break the user's session
            if os.environ.get("HONEST_DONE_DEBUG"):
                raise
            sys.stderr.write(PREFIX + "internal error, allowing stop: %s\n" % e)
        return 0
    import argparse
    p = argparse.ArgumentParser(prog="honest-done", description="Claude said it's done. Prove it.")
    p.add_argument("--version", action="version", version=VERSION)
    sub = p.add_subparsers(dest="cmd")
    c = sub.add_parser("check", help="run every check against a repo (+ optional transcript) and print a report")
    c.add_argument("--transcript", help="Claude Code transcript .jsonl (the last turn is judged)")
    c.add_argument("--repo", help="repository path (default: current directory)")
    c.add_argument("--base", help="git revision the turn started from (default: HEAD; untracked files included)")
    c.add_argument("--prompt", help="override the user prompt text")
    c.add_argument("--strict", action="store_true")
    c.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    if a.cmd == "check":
        return cmd_check(a)
    p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
