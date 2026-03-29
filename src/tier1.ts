import { realpathSync } from "node:fs";
import { resolve, relative, dirname, basename, isAbsolute, join } from "node:path";

/**
 * Tier 1 auto-approval filter for safe tool operations.
 *
 * Returns `true` if the tool request is safe to auto-approve without
 * relaying to the external agent. Returns `false` to fall through
 * to the relay (or interactive fallback).
 *
 * Security principle: deny-list first, conservative default.
 * When in doubt, return false (relay decides).
 *
 * Note: Bash shell commands do NOT get path-containment checks
 * (unlike Write/Edit). Static analysis of shell redirect/copy targets
 * is impractical. Only commands with no write side effects are safe-listed.
 */
export function isTier1AutoApprove(
  toolName: string,
  input: Record<string, unknown>,
  cwd: string,
): boolean {
  switch (toolName) {
    // Always auto-approve: read-only tools
    case "Read":
    case "Glob":
    case "Grep":
      return true;

    // Auto-approve with input inspection
    case "Bash": {
      const command = typeof input.command === "string" ? input.command : "";
      if (!command.trim()) return false; // empty command → relay
      return isSafeBashCommand(command);
    }

    case "Write":
    case "Edit": {
      const filePath = typeof input.file_path === "string" ? input.file_path : "";
      if (!filePath) return false; // missing path → relay
      return isWithinProject(filePath, cwd);
    }

    // Never auto-approve
    default:
      return false;
  }
}

// ── Deny-list ────────────────────────────────────────────────────────────────

/**
 * Tier 3 dangerous patterns. Applied to the FULL raw command string
 * before any splitting. If any pattern matches, the command is NOT
 * auto-approved (falls through to relay).
 */
const TIER3_PATTERNS: RegExp[] = [
  /rm\s+(-\w*r\w*f|-\w*f\w*r)\b/,        // rm -rf, rm -fr, rm -rfi, etc.
  /git\s+push\s+.*--force\b/,              // git push --force
  /git\s+push\s+.*-\w*f\b/,               // git push -f (short flag)
  /git\s+push\s+\S+\s+(main|master)\b/,   // git push origin main/master
  /git\s+reset\s+--hard\b/,               // git reset --hard
  /git\s+branch\s+.*-\w*D\b/,             // git branch -D (force-delete)
  /\bDROP\s+TABLE\b/i,                    // DROP TABLE (case-insensitive)
  /\bDELETE\s+FROM\b/i,                   // DELETE FROM (case-insensitive)
  /\bcargo\s+publish\b/,                   // cargo publish
  /\bsed\s+(-\w*i|-i\w*)\b/,             // sed -i (in-place)
  /\bgh\s+label\s+(delete|edit)\b/,        // gh label delete/edit
  /\bbash\s+-c\b/,                         // bash -c
  /\bsh\s+-c\b/,                           // sh -c
  /\beval\s/,                              // eval command
  /\bxargs\b/,                             // xargs (command amplifier)
  /\bfind\s.*-(exec|delete)\b/,           // find -exec or -delete
  /\$\(/,                                  // command substitution $(...)
  /`[^`]*`/,                               // backtick command substitution
  /<\(/,                                   // process substitution <(...)
  />\(/,                                   // process substitution >(...)
  /(?:^|[^<])>{1,2}(?!\()/,               // output redirect > or >> (not process substitution)
];

export function isTier3Dangerous(command: string): boolean {
  return TIER3_PATTERNS.some((pattern) => pattern.test(command));
}

// ── Safe Bash command checking ───────────────────────────────────────────────

/**
 * Split a compound command on shell operators (&&, ||, ;, |).
 * This is a naive split — not quote-aware. Garbled sub-commands
 * from splitting inside quotes won't match safe patterns and will
 * fall through to relay. This is safe by design.
 */
function splitCompoundCommand(command: string): string[] {
  return command
    .split(/\s*(?:&&|\|\||[;|])\s*/)
    .map((s) => s.trim())
    .filter(Boolean);
}

/**
 * Check if a full Bash command is safe to auto-approve.
 * Deny-list is checked first on the raw string, then each
 * sub-command must match a safe pattern.
 */
export function isSafeBashCommand(command: string): boolean {
  // Deny-list first — scans full raw command string
  if (isTier3Dangerous(command)) return false;

  // Split and check each sub-command
  const subCommands = splitCompoundCommand(command);
  if (subCommands.length === 0) return false;

  return subCommands.every((sub) => isSafeSubCommand(sub));
}

function isSafeSubCommand(sub: string): boolean {
  return (
    isSafeGitCommand(sub) ||
    isSafeBuildCommand(sub) ||
    isSafeShellCommand(sub) ||
    isSafeGhCommand(sub)
  );
}

// ── Safe git commands ────────────────────────────────────────────────────────

const SAFE_GIT_SUBCOMMANDS = new Set([
  "status", "log", "diff", "branch", "show", "commit",
  "push", "checkout", "worktree", "rev-parse", "remote",
  "fetch", "pull", "add", "stash", "tag", "merge",
  "rebase", "cherry-pick", "symbolic-ref",
  "ls-files", "describe", "shortlog", "blame",
]);

export function isSafeGitCommand(sub: string): boolean {
  const match = sub.match(/^\s*git\s+(\S+)/);
  if (!match) return false;

  const gitSub = match[1];
  if (!SAFE_GIT_SUBCOMMANDS.has(gitSub)) return false;

  // Block --force / -f flags on any git command
  if (/--force\b|-\w*f\b/.test(sub)) return false;

  // Block push to main/master
  if (gitSub === "push" && /\b(main|master)\b/.test(sub)) return false;

  // Block branch -D (force-delete, caught by deny-list too)
  if (gitSub === "branch" && /-\w*D\b/.test(sub)) return false;

  return true;
}

// ── Safe build/test commands ─────────────────────────────────────────────────
//
// Build/test/format commands are safe because the project itself is trusted.
// They execute project-defined code (package.json scripts, Cargo build scripts).
// If the threat model ever changes to include untrusted repos, ALL commands in
// this section must be moved to Tier 2 (relay).

const SAFE_CARGO_SUBCOMMANDS = new Set([
  "check", "test", "clippy", "fmt", "build",
  "clean", "doc", "bench", "tree", "metadata",
]);

const SAFE_NPM_RUN_SCRIPTS = new Set([
  "build", "dev", "test", "lint", "fmt", "start",
  "typecheck", "type-check", "check",
]);

export function isSafeBuildCommand(sub: string): boolean {
  // cargo <subcommand>
  const cargoMatch = sub.match(/^\s*cargo\s+(\S+)/);
  if (cargoMatch && SAFE_CARGO_SUBCOMMANDS.has(cargoMatch[1])) return true;

  // npm run <script>
  const npmRunMatch = sub.match(/^\s*npm\s+run\s+(\S+)/);
  if (npmRunMatch && SAFE_NPM_RUN_SCRIPTS.has(npmRunMatch[1])) return true;

  // npm test / npm start (built-in aliases without "run" prefix)
  if (/^\s*npm\s+(test|start)\b/.test(sub)) return true;

  // npm install / npm ci
  if (/^\s*npm\s+(install|ci)\b/.test(sub)) return true;

  // npx tsc / npx vitest / npx prettier / npx eslint
  // prettier --write and eslint --fix modify project files in-place,
  // intentionally allowed (same trust level as cargo fmt)
  if (/^\s*npx\s+(tsc|vitest|prettier|eslint)\b/.test(sub)) return true;

  return false;
}

// ── Safe shell commands ──────────────────────────────────────────────────────

/**
 * Read-only or benign shell commands. Commands that can write to arbitrary
 * locations (cp, mv, tee, python3) are intentionally excluded — they bypass
 * the isWithinProject() check that protects Write/Edit.
 */
const SAFE_SHELL_COMMANDS = new Set([
  "ls", "cat", "head", "tail", "wc", "find", "grep", "sed",
  "awk", "echo", "printf", "dirname", "basename",
  "realpath", "readlink", "stat", "file", "which", "type",
  "pwd", "date", "sort", "uniq", "tr", "cut", "diff",
  "comm", "test", "[",
]);

export function isSafeShellCommand(sub: string): boolean {
  const match = sub.match(/^\s*(\S+)/);
  if (!match) return false;

  const cmd = match[1];
  if (!SAFE_SHELL_COMMANDS.has(cmd)) return false;

  // sed -i is blocked by deny-list (scans raw string),
  // but double-check here for safety
  if (cmd === "sed" && /\s-\w*i\b/.test(sub)) return false;

  // find with -exec or -delete is blocked by deny-list,
  // but double-check here
  if (cmd === "find" && /-(exec|delete)\b/.test(sub)) return false;

  return true;
}

// ── Safe GitHub CLI commands ─────────────────────────────────────────────────

/**
 * Map of gh subdomains to their safe (auto-approvable) subcommands.
 * Adding a new gh subdomain is a one-liner: add an entry to this map.
 * gh api is handled separately (flag-based gating, not subcommand lookup).
 */
const SAFE_GH_SUBCOMMANDS: ReadonlyMap<string, ReadonlySet<string>> = new Map([
  ["pr",       new Set(["create", "view", "list", "checkout", "diff", "checks"])],
  ["issue",    new Set(["view", "list"])],
  ["run",      new Set(["view", "list"])],
  ["repo",     new Set(["view"])],
  ["release",  new Set(["view", "list"])],
  ["workflow", new Set(["view", "list"])],
]);

export function isSafeGhCommand(sub: string): boolean {
  // gh <domain> <subcommand> — lookup in SAFE_GH_SUBCOMMANDS map
  const match = sub.match(/^\s*gh\s+(\S+)\s+(\S+)/);
  if (match) {
    const allowed = SAFE_GH_SUBCOMMANDS.get(match[1]);
    if (allowed) return allowed.has(match[2]);
  }

  // gh api — only auto-approve read-only (no method override, no field/body input)
  if (/^\s*gh\s+api\b/.test(sub)) {
    if (/-(X|method)\b/.test(sub) || /-(f|F|field|raw-field)\b/.test(sub) || /--input\b/.test(sub)) return false;
    return true;
  }

  return false;
}

// ── Write/Edit path safety ───────────────────────────────────────────────────

/**
 * Check if a file path resolves within the project directory.
 * Uses fs.realpathSync() to resolve symlinks and prevent traversal.
 */
export function isWithinProject(filePath: string, cwd: string): boolean {
  if (!filePath) return false;

  try {
    const resolvedCwd = realpathSync(cwd);
    const absPath = resolve(resolvedCwd, filePath);
    const resolvedPath = tryResolveRealPath(absPath);

    if (!resolvedPath) return false; // cannot resolve → relay

    const rel = relative(resolvedCwd, resolvedPath);
    return !rel.startsWith("..") && !isAbsolute(rel);
  } catch {
    // Any error (bad cwd, etc.) → conservative, relay
    return false;
  }
}

/**
 * Try to resolve the real path. If the file doesn't exist (Write creating new file),
 * resolve the parent directory and append the basename.
 */
function tryResolveRealPath(absPath: string): string {
  try {
    return realpathSync(absPath);
  } catch {
    // File doesn't exist — try resolving the parent directory
    const dir = dirname(absPath);
    const base = basename(absPath);
    try {
      return join(realpathSync(dir), base);
    } catch {
      // Parent doesn't exist either → cannot verify, relay
      return "";
    }
  }
}
