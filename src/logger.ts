import { createWriteStream, mkdirSync, type WriteStream } from "node:fs";
import { dirname } from "node:path";

let fileStream: WriteStream | undefined;

export function initFileLog(filePath: string): void {
  mkdirSync(dirname(filePath), { recursive: true });
  fileStream = createWriteStream(filePath, { flags: "a" });
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
