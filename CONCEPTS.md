# Concepts

> Shared domain vocabulary for this project — entities, named processes, and status concepts with project-specific meaning. Seeded with core domain vocabulary, then accretes as ce-compound and ce-compound-refresh process learnings; direct edits are fine. Glossary only, not a spec or catch-all.

## Permission classifier

### Tier1

The deterministic fast path of the permission classifier that auto-approves a tool call outright when every part of it matches a closed-world allowlist of known-safe shapes; anything it does not approve falls through to the policy.

### Policy

The second tier of the permission classifier: an ordered rules file whose first matching rule decides allow, deny, or deny-with-notify, with deny as the default when nothing matches.

### Chain-safe check

The veto that runs after the policy allows a compound shell command: the command is split into its statements and each one must be independently safe, so a dangerous statement cannot ride on an allowed prefix. Because it judges statements one at a time, it cannot see relationships between them, such as a variable set in one statement and used in another.

### Admission

The decision whether a tool call may run at all. Distinct from **lethality**: widening admission lets more calls run; it is the side of the classifier that needs operator ratification.

### Lethality

Whether a refused call also ends the pilot's session (terminal) or comes back to the model as a refusal it can adapt to (non-terminal). Decided separately from **admission**: changing lethality never makes a refused call run.

### Containment attestation

The signal, set by the dispatch environment, that the pilot runs inside filesystem, network and kernel isolation; some **tier1** shapes are admitted only when it is present.
