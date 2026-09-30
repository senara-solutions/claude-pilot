---
title: "An inline env-assignment prefix before cargo is a build variable, not egress"
date: 2026-09-30
module: claude_pilot.policies
component: permission-classifier
problem_type: security_issue
category: security-issues
severity: high
tags: [permissions, policy, bash, cargo, rust, admission, env-assignment, closed-whitelist, egress, default-deny, error-max-turns, claude-pilot-242, mika-2105]
applies_when: "an allow rule anchored on a bare first word denies the standard inline NAME=value prefix that carries a build/log variable, starving the pilot, while a generic prefix relaxation would re-open the egress-disabling axis"
---

# An inline env-assignment prefix before cargo is a build variable, not egress

## The starvation

The `bash-cargo` allow rule anchored on a bare first word:

```
^cargo\s+(build|test|check|clippy|fmt|run|clean|doc|tree)\b
```

The standard shell idiom for passing a build/measure variable to a single
command is an inline `NAME=value` prefix — `CARGO_INCREMENTAL=0 cargo test`,
`CARGO_TARGET_DIR=/x cargo build`, `RUST_LOG=debug cargo run`. With the anchor on
a bare `cargo`, every such invocation fell to **default-deny**. The pilot could
not run a Rust build the way the toolchain documents it, so a Rust step made no
progress and burned out (`error_max_turns` with zero work, mika#2105). Two
read-only subcommands were also missing: `cargo --version` and `cargo metadata`.

Probe facts confirmed at source on the base (`c0af21b`):

| command | BEFORE | AFTER |
|---|---|---|
| `CARGO_INCREMENTAL=0 CARGO_TARGET_DIR=/x cargo test --no-run` | deny | **allow** (bash-cargo) |
| `RUST_LOG=debug cargo build` | deny | **allow** |
| `cargo --version` | deny | **allow** |
| `cargo metadata --format-version 1` | deny | **allow** |
| `PATH=/tmp cargo build` | deny | **deny** |
| `HOME=/x cargo test` | deny | **deny** |
| `LD_PRELOAD=/tmp/x.so cargo build` | deny | **deny** |
| `HTTPS_PROXY= cargo build` | deny | **deny** |
| `FOO=1 cargo build` | deny | **deny** |
| `CARGO_INCREMENTAL=0 bash -c 'cargo build'` | deny | **deny** |

## The fix — a CLOSED five-name prefix, never a generic `\w+=`

Prime-ratified admission (2026-09-30, Vincent delegation). The rule accepts a
chainable prefix of assignments, but **only** from a closed allow-list of five
build/log names, and **only** immediately before a literal `cargo`:

```
^(?:(?:CARGO_INCREMENTAL|CARGO_TARGET_DIR|RUST_LOG|RUST_BACKTRACE|CARGO_TERM_COLOR)=\S*\s+)*cargo\s+(build|test|check|clippy|fmt|run|clean|doc|tree|metadata|--version)\b
```

`metadata` and `--version` are added to the read-only subcommand set.

## SECURITY — the danger a generic prefix would re-open

The over-reach would be a generic `\w+=` prefix. That is not a convenience axis;
it is the **egress / process-environment** axis. `HTTPS_PROXY=`/`HTTP_PROXY=`
redirect the build's network egress; `PATH=`/`HOME=` relocate which binaries and
config a subprocess resolves; `LD_PRELOAD=`/`LD_*` inject arbitrary code into
every dynamically-linked child. Admitting `\w+=` before `cargo` would silently
hand all of that through. So the prefix is a **fixed literal alternation of five
names** whose only effect is on the Rust build itself — incremental compilation,
the target directory, log verbosity, backtraces, colour. Every off-list name
stays **default-denied**; there is no dedicated egress rule to weaken, and this
change does not add one — the egress axis is left exactly where it was.

Two further boundaries proven by test:

- **The prefix is valid only before `cargo`.** The anchor requires a literal
  `cargo` immediately after the closed alternation, so a prefixed *other* binary
  — `CARGO_INCREMENTAL=0 bash -c '…'` — never matches and stays refused. A build
  variable that legitimately precedes `cargo` cannot be borrowed to launch an
  arbitrary command.
- **`RUSTFLAGS` is deliberately off-list** (still refused). It can inject linker
  args and codegen flags, so admitting it is a separate, evidence-gated decision,
  noted in the plan; not done here.

## Admission only — lethality and the tier1 gate are byte-identical

The change is a policy-YAML allow-pattern widening. `_denial_is_terminal`
(lethality) is untouched: a refused off-list `cargo` invocation keeps whatever
terminality it already had. The sovereign tier1 gate `is_tier1_auto_approve` is
untouched. `make` is untouched.

## The learning

**When an allow rule anchored on a bare command denies the standard inline
`NAME=value` prefix, re-admit only a closed literal set of variables whose effect
is confined to the command itself — never a generic `\w+=`, which is the egress /
process-environment axis in disguise.** Anchor the prefix so it is valid only
immediately before the intended binary, so an admitted build variable cannot be
used to launch an arbitrary command.

## References
- Rule: `bash-cargo` in `src/claude_pilot/policies/permissions.yaml`.
- Preserves: the default-deny egress posture (`HTTPS_PROXY=`/`PATH=`/`HOME=`/`LD_*` refused); `_denial_is_terminal` (lethality); `is_tier1_auto_approve` (tier1 gate).
- Follow-up: `RUSTFLAGS` admission (a separate decision).
- Tickets: mika#2105 (the starvation), cpp#242 (this admission).
