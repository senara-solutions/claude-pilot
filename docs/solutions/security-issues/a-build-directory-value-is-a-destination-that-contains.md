---
title: "A build-directory value is a destination that CONTAINS, not a point: cargo's --target-dir / CARGO_TARGET_DIR"
date: 2026-10-26
problem_type: security_issue
track: knowledge
module: permissions
component: cargo-target-dir-containment
tags:
  - claude-pilot
  - permissions
  - tier1
  - cargo
  - worktree-containment
  - control-plane
  - chain-safe-veto
applies_when: "An admitted command takes a DIRECTORY it will own (a build dir, an output dir, a cache dir) as a flag value or an env assignment; or you are adding a containment check for such a value and are tempted to reuse the point-wise control-plane patterns."
---

# A build-directory value is a destination that contains, not a point

## Problem (cpp#248)

`cargo build --target-dir V`, `cargo build --target-dir=V` and
`CARGO_TARGET_DIR=V cargo build` name the directory cargo writes its whole build into.
On `origin/main` ec63e2d none of the three values was read. The flag rode tier1
(`is_safe_build_command`, `AUTO`), the assignment rode `bash-cargo` and the chain-safe
veto. `cargo build --target-dir /home/samidarko/.ssh` and
`CARGO_TARGET_DIR=/tmp/t cargo build` were both admitted. In real pilot logs the
values included `/tmp/mika-check` and `/data/workspace/mika-platform/mika/target`,
which is the deployment checkout's target.

## The non-obvious part: point-wise checks miss the container

The obvious fix applies the cp/mv destination checks to the value: resolve it inside
the worktree and refuse a match against `_CONTROL_PLANE_PATTERNS`. A review probe found
what that misses. `cargo clean --target-dir .` was still admitted, and on a throwaway
project `cargo clean -n -v --target-dir .` lists `./.git`, `./.claude` and `.`
itself. The same holds for `.github`, `skills` and `crates/mika-agent/src`. Each of
these **contains** a control-plane path without **being** one, and each was already
admitted on main through tier1.

A cp/mv destination is a single path, so asking "is this path on the control plane?"
covers it. A build directory is a subtree that cargo owns completely, so the question
becomes "does this subtree hold any of the control plane?". The regexes cannot answer
that. `_CONTROL_PLANE_ROOTS` lists the literal path behind each pattern, and a test pins
the pairing one-to-one. `_contains_control_plane` then refuses the worktree root, a
control-plane path, and any ancestor of one.

## Other decisions worth keeping

- **tier1 refers the case and does not judge it.** `is_safe_build_command` has no
  worktree root, so it no longer admits a cargo command that carries a build directory.
  The policy path (`_bash_allow_is_chain_safe(..., cwd)`) decides, resolving symlinks.
  Without a cwd, an absolute value is refused and a relative one is judged by its
  spelling.
- **The values are read from quote-removed words** (`_lex_shell_words`).
  `'--target-dir' /etc/x` and `--target-dir "/etc/x"` are the same flag to bash. A
  segment that cannot be lexed and mentions cargo is refused.
- **Lethality is left as is.** The refusal goes through the chain-safe veto and is
  non-terminal for these forms. The ticket left lethality to a decision that has not
  been made. Making the refusal terminal changes nothing about the refusal itself.
- **Known side effects:** `cargo bench --target-dir t` is now refused. tier1 used to
  admit it, but `bench` is not in `bash-cargo`, and adding it would widen the rule.
  `cd sub && cargo build --target-dir t` is refused too.

## Out of scope, measured by the review

These are other ways cargo writes outside the worktree. All are admitted today, and all
belong to another class:
- `--manifest-path /tmp/x/Cargo.toml`: writes `/tmp/x/target` and runs that crate's
  `build.rs`.
- `cd /tmp && cargo build`.
- `-C` and `--artifact-dir`: nightly only.
