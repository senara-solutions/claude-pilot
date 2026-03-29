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
    isSafePrCommand(sub)
  );
}

// ── Safe git commands ────────────────────────────────────────────────────────

const SAFE_GIT_SUBCOMMANDS = new Set([
  "status", "log", "diff", "branch", "show", "commit",
  "push", "checkout", "worktree", "rev-parse", "remote",
  "fetch", "pull", "add", "stash", "tag", "merge",
  "rebase", "cherry-pick", "config", "symbolic-ref",
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

  return true;
}

// ── Safe build/test commands ─────────────────────────────────────────────────

const SAFE_CARGO_SUBCOMMANDS = new Set([
  "check", "test", "clippy", "fmt", "build",
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

  // npm install / npm ci
  if (/^\s*npm\s+(install|ci)\b/.test(sub)) return true;

  // npx tsc / npx vitest
  if (/^\s*npx\s+(tsc|vitest)\b/.test(sub)) return true;

  return false;
}

// ── Safe shell commands ──────────────────────────────────────────────────────

const SAFE_SHELL_COMMANDS = new Set([
  "ls", "cat", "head", "tail", "wc", "find", "grep", "sed",
  "awk", "mkdir", "echo", "printf", "dirname", "basename",
  "realpath", "readlink", "stat", "file", "which", "type",
  "env", "pwd", "date", "sort", "uniq", "tr", "cut", "diff",
  "comm", "test", "[", "touch", "cp", "mv",
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

// ── Safe PR/issue commands ───────────────────────────────────────────────────

const SAFE_GH_PR_SUBCOMMANDS = new Set([
  "create", "view", "list", "checkout", "diff", "checks",
]);

const SAFE_GH_ISSUE_SUBCOMMANDS = new Set([
  "view", "list", "create",
]);

export function isSafePrCommand(sub: string): boolean {
  // gh pr <subcommand>
  const prMatch = sub.match(/^\s*gh\s+pr\s+(\S+)/);
  if (prMatch && SAFE_GH_PR_SUBCOMMANDS.has(prMatch[1])) return true;

  // gh issue <subcommand>
  const issueMatch = sub.match(/^\s*gh\s+issue\s+(\S+)/);
  if (issueMatch && SAFE_GH_ISSUE_SUBCOMMANDS.has(issueMatch[1])) return true;

  // gh run view/list
  if (/^\s*gh\s+run\s+(view|list)\b/.test(sub)) return true;

  // gh api (read-only GET requests)
  if (/^\s*gh\s+api\b/.test(sub)) return true;

  return false;
}

// ── Write/Edit path safety ───────────────────────────────────────────────────

/**
 * Check if a file path resolves within the project directory.
 * Uses fs.realpathSync() to resolve symlinks and prevent traversal.
 */
export function isWithinProject(filePath: string, cwd: string): boolean {
  try {
    const resolvedCwd = realpathSync(cwd);
    let resolvedPath: string;

    if (!filePath) return false; // empty path → relay

    if (isAbsolute(filePath)) {
      // Try to resolve the full path; if it doesn't exist, resolve parent
      resolvedPath = tryResolveRealPath(filePath, resolvedCwd);
    } else {
      // Relative path: resolve against cwd
      const absPath = resolve(resolvedCwd, filePath);
      resolvedPath = tryResolveRealPath(absPath, resolvedCwd);
    }

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
function tryResolveRealPath(absPath: string, _resolvedCwd: string): string {
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
