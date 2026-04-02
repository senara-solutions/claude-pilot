import { createWriteStream, mkdirSync, type WriteStream } from "node:fs";
import { dirname } from "node:path";

let fileStream: WriteStream | undefined;
let logErrorReported = false;

export function initFileLog(filePath: string): void {
  mkdirSync(dirname(filePath), { recursive: true, mode: 0o700 });
  fileStream = createWriteStream(filePath, { flags: "a", mode: 0o600 });
  // Log file is best-effort — swallow errors to prevent them from escalating
  // to uncaughtException and changing the exit code. Emit one diagnostic.
  fileStream.on("error", (err) => {
    if (!logErrorReported) {
      logErrorReported = true;
      process.stderr.write(`Warning: log file write error: ${err.message}\n`);
    }
    fileStream = undefined;
  });
}

export function writeLog(msg: string): void {
  process.stderr.write(msg);
  if (fileStream) {
    fileStream.write(msg.replace(/\x1b\[[0-9;]*m/g, ""));
  }
}

export function writeFileLog(msg: string): void {
  if (fileStream) {
    fileStream.write(msg.replace(/\x1b\[[0-9;]*m/g, ""));
  }
}

export function closeFileLog(): void {
  fileStream?.end();
  fileStream = undefined;
}
