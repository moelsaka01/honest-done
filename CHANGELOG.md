# Changelog

## 0.1.0 (2026-11-09)

- First release: Stop + UserPromptSubmit hooks, six deterministic checks, `check` CLI,
  `.honest-done.json` config, 34-case benchmark (18/18 caught, 0/16 false positives),
  real-history false-positive replay (2 of 213 commits flagged).
- Pre-release fixes: untracked files over 2 MB are no longer hashed on every prompt
  (a 150 MB stray file made each prompt take 3-16 s and wrote 144 MB into `.git/objects`),
  and the snapshot keeps a private per-session index, so unchanged files aren't re-hashed;
  `TODO`/`breakpoint()` inside string literals are no longer flagged; `cat pytest.ini` or
  `cat webpack.config.js` no longer counts as a test/build run; `check --base <bad-rev>`
  prints a clean error (exit 2) instead of a traceback.
