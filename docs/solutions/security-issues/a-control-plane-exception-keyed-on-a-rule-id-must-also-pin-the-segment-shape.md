---
title: "A control-plane exception keyed on a rule_id must also pin the segment shape"
date: 2026-11-27
problem_type: security_issue
track: knowledge
module: permissions
component: control-plane-exception
tags:
  - claude-pilot
  - permissions
  - control-plane
  - destination-veto
  - rule-id-coupling
applies_when: "You open a narrow exception in `_destination_veto_reason` (or any destination check) for writes admitted by one named policy rule, and you key the exception on that rule's rule_id."
---

# A control-plane exception keyed on a rule_id must also pin the segment shape

## Context

cpp#325 admits `sed -n '<N>,<M>p' <aggregate> >[>] skills/bundled/_shared/tests/test_dispatch_lib_<nom>.sh`
under the rule `bash-sed-range-extract-3178`. The target is on the cpp#42
control plane (`^skills/bundled(/|$)`), so the destination veto refused every
admitted command in terminal mode. The operator ratified one exception, coupled
to the rule's rule_id.

## What went wrong in the first draft

The first draft of `_is_cpp325_control_plane_exception` checked three things:
the write kind is `bash-redirect`, the rule_id passed by the admission caller is
the rule's, and the resolved path matches the anchored target pattern. A test
then called `_destination_veto_reason("cat x > skills/bundled/_shared/tests/test_dispatch_lib_a.sh", cwd, rule_id=RULE_ID)`
and got `None`: the exception accepted another redirect form.

In production that call cannot happen today, since the policy only yields that
rule_id for the exact shape. But the exception then holds only because of a
fact proven in another file (the YAML pattern). One edit to the YAML, or a
second caller passing a rule_id, and the exception silently widens to any
redirect form.

## The rule

An exception keyed on a rule_id also checks, on its own, that the segment has
the rule's exact shape. Keep that shape byte-identical to the YAML pattern, and
pin the equality with a test, so the two cannot drift apart. Resolve the path
before matching it, so a symlinked directory or target cannot pass under the
anchored name.

## Where

`src/claude_pilot/permissions.py`: `_CPP325_RANGE_EXTRACT_RULE_ID`,
`_CPP325_RANGE_EXTRACT_TARGET_RE`, `_CPP325_RANGE_EXTRACT_SEGMENT_RE`,
`_is_cpp325_control_plane_exception`. Tests:
`tests/test_cpp325_sed_range_extract.py`.
