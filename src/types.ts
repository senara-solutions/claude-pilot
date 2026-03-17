import { z } from "zod";

// --- Configuration ---

export const PilotConfigSchema = z.object({
  command: z.string().min(1),
  args: z.array(z.string()).optional(),
  timeout: z.number().int().min(1000).max(600_000).optional(),
});

export type PilotConfig = z.infer<typeof PilotConfigSchema>;

// --- Event payload sent to external agent via stdin ---

export interface PilotEvent {
  type: "permission" | "question";
  tool_name: string;
  tool_input: Record<string, unknown>;
  tool_use_id: string;
  decision_reason?: string;
  blocked_path?: string;
  error?: string; // present on retry after malformed response
}

// --- Response from external agent via stdout ---

export const PilotResponseSchema = z.discriminatedUnion("action", [
  z.object({ action: z.literal("allow") }),
  z.object({ action: z.literal("deny"), message: z.string().optional() }),
  z.object({
    action: z.literal("answer"),
    answers: z.record(z.string(), z.string()),
  }),
]);

export type PilotResponse = z.infer<typeof PilotResponseSchema>;

// --- Result JSON written to stdout on completion ---

export interface ResultJson {
  status: "success" | "error";
  subtype: string;
  task_id?: string;
  session_id?: string;
  turns: number;
  cost_usd: number;
  duration_ms: number;
  errors?: string[];
}
