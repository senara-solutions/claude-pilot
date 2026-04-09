import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { mkdirSync, rmSync, symlinkSync, existsSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import {
  isTier1AutoApprove,
  isTier3Dangerous,
  isSafeBashCommand,
  isSafeGitCommand,
  isSafeBuildCommand,
  isSafeShellCommand,
  isSafeGhCommand,
  isWithinProject,
} from "../src/tier1.js";

const CWD = process.cwd();

// ── Read-only tools (always auto-approve) ────────────────────────────────────

describe("Read-only tools", () => {
  it.each(["Read", "Glob", "Grep"])("auto-approves %s regardless of input", (tool) => {
    expect(isTier1AutoApprove(tool, {}, CWD)).toBe(true);
    expect(isTier1AutoApprove(tool, { file_path: "/etc/passwd" }, CWD)).toBe(true);
    expect(isTier1AutoApprove(tool, { pattern: "**/*.ts" }, CWD)).toBe(true);
  });
});

// ── Never auto-approve ──────────────────────────────────────────────────────

describe("Never auto-approve", () => {
  it("relays AskUserQuestion", () => {
    expect(isTier1AutoApprove("AskUserQuestion", { questions: [] }, CWD)).toBe(false);
  });

  it("relays unknown tools", () => {
    expect(isTier1AutoApprove("UnknownTool", {}, CWD)).toBe(false);
    expect(isTier1AutoApprove("WebSearch", {}, CWD)).toBe(false);
    expect(isTier1AutoApprove("CustomAgent", { foo: "bar" }, CWD)).toBe(false);
  });
});

// ── Bash: deny-list (Tier 3 dangerous) ──────────────────────────────────────

describe("Tier 3 deny-list", () => {
  it.each([
    ["rm -rf /tmp", "rm -rf"],
    ["rm -fr /tmp", "rm -fr"],
    ["git push --force origin main", "git push --force"],
    ["git push -f origin feat", "git push -f"],
    ["git push origin main", "git push to main"],
    ["git push origin master", "git push to master"],
    ["git reset --hard HEAD~1", "git reset --hard"],
    ["git branch -D feat/old", "git branch -D"],
    ["DROP TABLE users", "DROP TABLE"],
    ["drop table users", "drop table (case insensitive)"],
    ["DELETE FROM users WHERE id = 1", "DELETE FROM"],
    ["cargo publish --allow-dirty", "cargo publish"],
    ["sed -i 's/foo/bar/' file.txt", "sed -i"],
    ["sed -ie 's/foo/bar/' file.txt", "sed -i variant"],
    ["gh label delete bug", "gh label delete"],
    ["gh label edit bug", "gh label edit"],
    ['bash -c "echo hello"', "bash -c"],
    ['sh -c "echo hello"', "sh -c"],
    ['eval "echo hello"', "eval"],
    ["echo hello | xargs rm", "xargs"],
    ["find . -exec rm {} \\;", "find -exec"],
    ["find . -execdir grep foo {} +", "find -execdir"],
    ["find . -delete", "find -delete"],
    ["echo secret > /tmp/exfil", "output redirect >"],
    ["cat file >> /tmp/exfil", "output redirect >>"],
    ["awk '{print}' file > /outside", "redirect after safe command"],
    ["echo $(rm -rf /)", "command substitution $(...)"],
    ["echo `rm -rf /`", "backtick substitution"],
    ["diff <(cat /etc/shadow) <(echo x)", "process substitution <("],
  ])("blocks: %s (%s)", (command) => {
    expect(isTier3Dangerous(command)).toBe(true);
    expect(isSafeBashCommand(command)).toBe(false);
    expect(isTier1AutoApprove("Bash", { command }, CWD)).toBe(false);
  });
});

// ── Bash: safe git commands ─────────────────────────────────────────────────

describe("Safe git commands", () => {
  it.each([
    "git status",
    "git log --oneline -10",
    "git diff HEAD~1",
    "git diff --staged",
    "git branch --show-current",
    "git branch -a",
    "git show HEAD",
    "git commit -m 'fix: something'",
    "git push origin feat/my-branch",
    "git push -u origin feat/my-branch",
    "git push", // no args = push current branch
    "git checkout -b feat/new",
    "git checkout main", // checkout is safe (not push)
    "git worktree add ../worktree feat",
    "git rev-parse --git-dir",
    "git remote -v",
    "git fetch origin",
    "git pull origin main",
    "git add src/tier1.ts",
    "git stash",
    "git stash pop",
    "git tag v1.0.0",
    "git merge feat/branch",
    "git rebase main",
  ])("auto-approves: %s", (command) => {
    expect(isSafeGitCommand(command)).toBe(true);
    expect(isTier1AutoApprove("Bash", { command }, CWD)).toBe(true);
  });

  it.each([
    "git push --force origin feat",
    "git push -f origin feat",
    "git push origin main",
    "git push origin master",
    "git reset --hard",
    "git branch -D feat/old",
    "git config core.hooksPath /evil",
    "git config alias.st '!rm -rf /'",
  ])("does NOT auto-approve: %s", (command) => {
    expect(isTier1AutoApprove("Bash", { command }, CWD)).toBe(false);
  });
});

// ── Bash: safe build/test commands ──────────────────────────────────────────

describe("Safe build/test commands", () => {
  it.each([
    "cargo test",
    "cargo build",
    "cargo clippy",
    "cargo fmt",
    "cargo check",
    "cargo clean",
    "cargo doc",
    "cargo bench",
    "cargo tree",
    "cargo metadata",
    "cargo metadata --format-version 1",
    "cargo doc --no-deps",
    "cargo clean --release",
    "cargo bench -- --ignored",
    "npm test",
    "npm start",
    "npx prettier --write src/",
    "npx prettier --check .",
    "npx eslint src/",
    "npx eslint --fix .",
    "npm run build",
    "npm run test",
    "npm run dev",
    "npm run lint",
    "npm run fmt",
    "npm install",
    "npm ci",
    "npx tsc --noEmit",
    "npx vitest",
  ])("auto-approves: %s", (command) => {
    expect(isSafeBuildCommand(command)).toBe(true);
    expect(isTier1AutoApprove("Bash", { command }, CWD)).toBe(true);
  });
});

// ── Bash: safe shell commands ───────────────────────────────────────────────

describe("Safe shell commands", () => {
  it.each([
    "ls -la",
    "cat README.md",
    "head -10 file.txt",
    "tail -f log.txt",
    "wc -l file.txt",
    "find . -name '*.ts'",
    "grep -r pattern src/",
    "sed 's/foo/bar/' file.txt",
    "awk '{print $1}' file.txt",
    "echo hello",
    "printf '%s\\n' hello",
    "dirname /path/to/file",
    "basename /path/to/file.ts",
    "pwd",
    "date",
    "sort file.txt",
    "uniq file.txt",
    "tr '[:lower:]' '[:upper:]'",
    "cut -d: -f1 /etc/passwd",
    "diff file1.txt file2.txt",
    "which node",
    "stat file.txt",
    "file image.png",
  ])("auto-approves: %s", (command) => {
    expect(isSafeShellCommand(command)).toBe(true);
    expect(isTier1AutoApprove("Bash", { command }, CWD)).toBe(true);
  });

  it.each([
    ["cp src/a.ts /tmp/exfil", "cp can write outside project"],
    ["mv important.ts /dev/null", "mv can destroy files"],
    ["touch /tmp/signal", "touch can create files outside project"],
    ["env python3 -c 'import os'", "env can execute arbitrary commands"],
    ["mkdir -p /tmp/exfil", "mkdir can create dirs outside project"],
  ])("does NOT auto-approve: %s (%s)", (command) => {
    expect(isTier1AutoApprove("Bash", { command }, CWD)).toBe(false);
  });

  it("does NOT auto-approve sed -i", () => {
    expect(isTier1AutoApprove("Bash", { command: "sed -i 's/a/b/' f" }, CWD)).toBe(false);
  });

  it("does NOT auto-approve find -exec", () => {
    expect(isTier1AutoApprove("Bash", { command: "find . -exec ls {} \\;" }, CWD)).toBe(false);
  });

  it("does NOT auto-approve find -delete", () => {
    expect(isTier1AutoApprove("Bash", { command: "find . -name '*.tmp' -delete" }, CWD)).toBe(false);
  });

  it("does NOT auto-approve find -execdir", () => {
    expect(isTier1AutoApprove("Bash", { command: "find . -execdir grep foo {} +" }, CWD)).toBe(false);
  });
});

// ── Bash: safe GitHub CLI commands ──────────────────────────────────────────

describe("Safe GitHub CLI commands", () => {
  it.each([
    "gh pr create --title 'fix' --body 'desc'",
    "gh pr view 42",
    "gh pr list",
    "gh pr checkout 42",
    "gh pr diff 42",
    "gh pr checks 42",
    "gh issue view 14",
    "gh issue list",
    // gh issue create removed — side-effect visible to others (Tier 2)
    "gh run view 123",
    "gh run list",
    "gh repo view",
    "gh repo view senara-solutions/claude-pilot",
    "gh release view v1.0.0",
    "gh release list",
    "gh workflow view build.yml",
    "gh workflow list",
    "gh api repos/owner/repo/pulls",
  ])("auto-approves: %s", (command) => {
    expect(isSafeGhCommand(command)).toBe(true);
    expect(isTier1AutoApprove("Bash", { command }, CWD)).toBe(true);
  });

  it.each([
    ["gh api repos/owner/repo -X DELETE", "gh api with DELETE method"],
    ["gh api repos/owner/repo --method POST", "gh api with POST method"],
    ["gh api repos/owner/repo -f state=closed", "gh api with field input (implies mutation)"],
    ["gh api repos/owner/repo --field body=test", "gh api with --field"],
    ["gh issue create --title 'new'", "gh issue create is visible to others (Tier 2)"],
    ["gh api repos/owner/repo/issues --input body.json", "gh api with --input (implies mutation)"],
    ["gh repo create my-repo", "gh repo create is a mutation"],
    ["gh repo delete my-repo", "gh repo delete is destructive"],
    ["gh release create v2.0.0", "gh release create is a mutation"],
    ["gh release delete v1.0.0", "gh release delete is destructive"],
    ["gh workflow run build.yml", "gh workflow run triggers dispatch"],
    ["gh run rerun 123", "gh run rerun triggers execution"],
  ])("does NOT auto-approve: %s (%s)", (command) => {
    expect(isTier1AutoApprove("Bash", { command }, CWD)).toBe(false);
  });
});

// ── Bash: compound commands ─────────────────────────────────────────────────

describe("Compound commands", () => {
  it("auto-approves safe && safe", () => {
    expect(isTier1AutoApprove("Bash", { command: "git status && cargo test" }, CWD)).toBe(true);
  });

  it("auto-approves safe || safe", () => {
    expect(isTier1AutoApprove("Bash", { command: "ls || echo 'not found'" }, CWD)).toBe(true);
  });

  it("auto-approves safe ; safe", () => {
    expect(isTier1AutoApprove("Bash", { command: "git add . ; git status" }, CWD)).toBe(true);
  });

  it("auto-approves safe | safe", () => {
    expect(isTier1AutoApprove("Bash", { command: "ls | grep pattern" }, CWD)).toBe(true);
  });

  it("relays safe && dangerous (deny-list match)", () => {
    expect(isTier1AutoApprove("Bash", { command: "git status && rm -rf /tmp" }, CWD)).toBe(false);
  });

  it("relays dangerous ; safe", () => {
    expect(isTier1AutoApprove("Bash", { command: "rm -rf / ; echo done" }, CWD)).toBe(false);
  });

  it("auto-approves multi-part safe chain", () => {
    expect(
      isTier1AutoApprove("Bash", { command: "git status && cargo test && npm run build" }, CWD),
    ).toBe(true);
  });

  it("auto-approves new safe commands in compound chain", () => {
    expect(
      isTier1AutoApprove("Bash", { command: "cargo clean && cargo build && npm test" }, CWD),
    ).toBe(true);
  });

  it("auto-approves gh commands in compound chain", () => {
    expect(
      isTier1AutoApprove("Bash", { command: "gh release list && gh workflow list" }, CWD),
    ).toBe(true);
  });
});

// ── Bash: edge cases ────────────────────────────────────────────────────────

describe("Bash edge cases", () => {
  it("relays empty command", () => {
    expect(isTier1AutoApprove("Bash", { command: "" }, CWD)).toBe(false);
    expect(isTier1AutoApprove("Bash", { command: "   " }, CWD)).toBe(false);
  });

  it("relays Bash with no command field", () => {
    expect(isTier1AutoApprove("Bash", {}, CWD)).toBe(false);
  });

  it("relays Bash with non-string command", () => {
    expect(isTier1AutoApprove("Bash", { command: 42 }, CWD)).toBe(false);
    expect(isTier1AutoApprove("Bash", { command: null }, CWD)).toBe(false);
  });

  it("relays unrecognized commands", () => {
    expect(isTier1AutoApprove("Bash", { command: "some-unknown-command" }, CWD)).toBe(false);
  });

  it("auto-approves sed without -i", () => {
    expect(isTier1AutoApprove("Bash", { command: "sed 's/a/b/' file.txt" }, CWD)).toBe(true);
  });

  it("relays git push without --force but to main", () => {
    expect(isTier1AutoApprove("Bash", { command: "git push origin main" }, CWD)).toBe(false);
  });

  it("auto-approves git push without args", () => {
    expect(isTier1AutoApprove("Bash", { command: "git push" }, CWD)).toBe(true);
  });

  it("auto-approves git push to feature branch", () => {
    expect(isTier1AutoApprove("Bash", { command: "git push origin feat/my-branch" }, CWD)).toBe(true);
  });

  it("relays command substitution embedded in safe command", () => {
    expect(isTier1AutoApprove("Bash", { command: "echo $(cat secret)" }, CWD)).toBe(false);
  });

  it("relays process substitution", () => {
    expect(isTier1AutoApprove("Bash", { command: "diff <(cat a) <(cat b)" }, CWD)).toBe(false);
  });

  it("relays git config (can install hooks/aliases)", () => {
    expect(isTier1AutoApprove("Bash", { command: "git config core.hooksPath /evil" }, CWD)).toBe(false);
  });

  it("relays git branch -D (force-delete)", () => {
    expect(isTier1AutoApprove("Bash", { command: "git branch -D feat/old" }, CWD)).toBe(false);
  });

  it("relays new safe command with redirect (deny-list catches redirect)", () => {
    expect(isTier1AutoApprove("Bash", { command: "cargo tree > output.txt" }, CWD)).toBe(false);
    expect(isTier1AutoApprove("Bash", { command: "cargo metadata >> log.txt" }, CWD)).toBe(false);
    expect(isTier1AutoApprove("Bash", { command: "gh release list > releases.txt" }, CWD)).toBe(false);
  });

  it("relays new safe command with command substitution", () => {
    expect(isTier1AutoApprove("Bash", { command: "echo $(cargo tree)" }, CWD)).toBe(false);
  });
});

// ── Write/Edit: path safety ─────────────────────────────────────────────────

describe("Write/Edit path safety", () => {
  it("auto-approves Write to file within project", () => {
    expect(
      isTier1AutoApprove("Write", { file_path: join(CWD, "src/new-file.ts") }, CWD),
    ).toBe(true);
  });

  it("auto-approves Edit to file within project", () => {
    expect(
      isTier1AutoApprove("Edit", { file_path: join(CWD, "src/tier1.ts") }, CWD),
    ).toBe(true);
  });

  it("relays Write to file outside project", () => {
    expect(
      isTier1AutoApprove("Write", { file_path: "/etc/passwd" }, CWD),
    ).toBe(false);
  });

  it("relays Edit with path traversal", () => {
    expect(
      isTier1AutoApprove("Edit", { file_path: join(CWD, "../../outside/file.ts") }, CWD),
    ).toBe(false);
  });

  it("auto-approves Write to non-existent file in existing project dir", () => {
    expect(
      isTier1AutoApprove("Write", { file_path: join(CWD, "src/brand-new.ts") }, CWD),
    ).toBe(true);
  });

  it("relays Write with no file_path", () => {
    expect(isTier1AutoApprove("Write", {}, CWD)).toBe(false);
  });

  it("relays Write with non-string file_path", () => {
    expect(isTier1AutoApprove("Write", { file_path: 42 }, CWD)).toBe(false);
  });

  it("relays Edit with empty file_path", () => {
    expect(isTier1AutoApprove("Edit", { file_path: "" }, CWD)).toBe(false);
  });

  it("auto-approves relative path within project", () => {
    expect(
      isTier1AutoApprove("Write", { file_path: "src/new.ts" }, CWD),
    ).toBe(true);
  });

  it("relays relative path that escapes project", () => {
    expect(
      isTier1AutoApprove("Write", { file_path: "../../../etc/passwd" }, CWD),
    ).toBe(false);
  });
});

// ── Write/Edit: symlink safety ──────────────────────────────────────────────

describe("Write/Edit symlink safety", () => {
  const tmpBase = join(tmpdir(), "tier1-test-" + Date.now());
  const projectDir = join(tmpBase, "project");
  const outsideDir = join(tmpBase, "outside");
  const symlinkPath = join(projectDir, "escape-link");

  beforeAll(() => {
    mkdirSync(projectDir, { recursive: true });
    mkdirSync(outsideDir, { recursive: true });
    writeFileSync(join(outsideDir, "secret.txt"), "secret");
    try {
      symlinkSync(outsideDir, symlinkPath);
    } catch {
      // symlink creation may fail on some systems
    }
  });

  afterAll(() => {
    try {
      rmSync(tmpBase, { recursive: true, force: true });
    } catch {
      // cleanup best-effort
    }
  });

  it("relays Write through symlink pointing outside project", () => {
    if (!existsSync(symlinkPath)) return; // skip if symlink not created
    expect(
      isWithinProject(join(symlinkPath, "secret.txt"), projectDir),
    ).toBe(false);
  });

  it("auto-approves Write to regular file within project", () => {
    expect(
      isWithinProject(join(projectDir, "file.ts"), projectDir),
    ).toBe(true);
  });
});

// ── isWithinProject unit tests ──────────────────────────────────────────────

describe("isWithinProject", () => {
  it("returns true for file in project root", () => {
    expect(isWithinProject(join(CWD, "package.json"), CWD)).toBe(true);
  });

  it("returns true for file in subdirectory", () => {
    expect(isWithinProject(join(CWD, "src/tier1.ts"), CWD)).toBe(true);
  });

  it("returns false for absolute path outside project", () => {
    expect(isWithinProject("/tmp/outside.txt", CWD)).toBe(false);
  });

  it("returns false for traversal path", () => {
    expect(isWithinProject(join(CWD, "../../../etc/hosts"), CWD)).toBe(false);
  });

  it("returns false for empty path", () => {
    expect(isWithinProject("", CWD)).toBe(false);
  });
});

// ── Skill tool: pipeline slash commands ─────────────────────────────────────

describe("Skill tool — pipeline slash commands", () => {
  // All allowlisted skills (short form)
  it.each([
    "mika",
    "ce:plan",
    "ce:work",
    "ce:review",
    "ce:compound",
    "ce:brainstorm",
    "ralph-loop",
    "ralph-loop:ralph-loop",
    "ralph-loop:cancel-ralph",
    "ralph-loop:help",
    "mika-doc-audit",
  ])("auto-approves short-form skill: %s", (skill) => {
    expect(isTier1AutoApprove("Skill", { skill }, CWD)).toBe(true);
  });

  // All allowlisted skills (fully-qualified form)
  it.each([
    "compound-engineering:ce-plan",
    "compound-engineering:ce-work",
    "compound-engineering:ce-review",
    "compound-engineering:ce-compound",
    "compound-engineering:ce-brainstorm",
    "compound-engineering:resolve_todo_parallel",
  ])("auto-approves fully-qualified skill: %s", (skill) => {
    expect(isTier1AutoApprove("Skill", { skill }, CWD)).toBe(true);
  });

  // Args are ignored for matching — only input.skill matters
  it("auto-approves regardless of args", () => {
    expect(isTier1AutoApprove("Skill", { skill: "ce:plan", args: "--deep" }, CWD)).toBe(true);
    expect(isTier1AutoApprove("Skill", { skill: "mika", args: "#214" }, CWD)).toBe(true);
    expect(isTier1AutoApprove("Skill", { skill: "ralph-loop", args: "finish all slash commands" }, CWD)).toBe(true);
  });

  // Non-allowlisted skills are relayed
  it.each([
    "unknown-skill",
    "some-plugin:dangerous-action",
    "WebSearch",
    "agent-browser",
  ])("relays non-allowlisted skill: %s", (skill) => {
    expect(isTier1AutoApprove("Skill", { skill }, CWD)).toBe(false);
  });

  // Defensive: missing or malformed input.skill
  it("relays when input.skill is missing", () => {
    expect(isTier1AutoApprove("Skill", {}, CWD)).toBe(false);
  });

  it("relays when input.skill is not a string", () => {
    expect(isTier1AutoApprove("Skill", { skill: 42 }, CWD)).toBe(false);
    expect(isTier1AutoApprove("Skill", { skill: null }, CWD)).toBe(false);
    expect(isTier1AutoApprove("Skill", { skill: undefined }, CWD)).toBe(false);
    expect(isTier1AutoApprove("Skill", { skill: true }, CWD)).toBe(false);
  });

  it("relays when input.skill is empty string", () => {
    expect(isTier1AutoApprove("Skill", { skill: "" }, CWD)).toBe(false);
  });

  // Whitespace trimming
  it("trims whitespace from skill name before matching", () => {
    expect(isTier1AutoApprove("Skill", { skill: "  ce:plan  " }, CWD)).toBe(true);
    expect(isTier1AutoApprove("Skill", { skill: "\tralph-loop\n" }, CWD)).toBe(true);
  });

  // Case sensitivity — skill names are case-sensitive
  it("is case-sensitive (does not match wrong case)", () => {
    expect(isTier1AutoApprove("Skill", { skill: "CE:PLAN" }, CWD)).toBe(false);
    expect(isTier1AutoApprove("Skill", { skill: "Mika" }, CWD)).toBe(false);
    expect(isTier1AutoApprove("Skill", { skill: "RALPH-LOOP" }, CWD)).toBe(false);
  });
});

// ── isTier1AutoApprove integration ──────────────────────────────────────────

describe("isTier1AutoApprove integration", () => {
  it("returns boolean, never throws", () => {
    // Valid inputs
    expect(typeof isTier1AutoApprove("Read", {}, CWD)).toBe("boolean");
    expect(typeof isTier1AutoApprove("Bash", { command: "ls" }, CWD)).toBe("boolean");

    // Malformed inputs — should return false, not throw
    expect(typeof isTier1AutoApprove("Bash", { command: undefined }, CWD)).toBe("boolean");
    expect(typeof isTier1AutoApprove("Write", { file_path: {} }, CWD)).toBe("boolean");
    expect(typeof isTier1AutoApprove("", {}, CWD)).toBe("boolean");
  });
});
