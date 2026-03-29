import { z } from "zod";

// --- Configuration ---

export const GuardrailConfigSchema = z.object({
  maxTurns: z.number().int().min(1).optional(),
  maxBudgetUsd: z.number().min(0.01).optional(),
  stallThreshold: z.number().int().min(1).optional(),
  emptyResponseThreshold: z.number().int().min(1).optional(),
  idleTimeoutMs: z.number().int().min(1000).optional(),
  minTurnsBeforeDetection: z.number().int().min(0).optional(),
});

export type GuardrailConfig = z.infer<typeof GuardrailConfigSchema>;

export const GUARDRAIL_DEFAULTS: Required<GuardrailConfig> = {
  maxTurns: 200,
  maxBudgetUsd: 0, // 0 = disabled
  stallThreshold: 5,
  emptyResponseThreshold: 5,
  idleTimeoutMs: 300_000, // 5 minutes
  minTurnsBeforeDetection: 10,
};

export const PilotConfigSchema = z.object({
  command: z.string().min(1),
  args: z.array(z.string()).optional(),
  timeout: z.number().int().min(1000).max(600_000).optional(),
  model: z.string().min(1).optional(),
  guardrails: GuardrailConfigSchema.optional(),
});

export type PilotConfig = z.infer<typeof PilotConfigSchema>;

// --- Event payload sent to external agent via stdin ---

export interface PilotEvent {
  type: "permission" | "question";
  tool_name: string;
  tool_input: Record<string, unknown>;
  tool_use_id: string;
  agent_id?: string; // set when the call originates from a sub-agent
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
  status: "success" | "error" | "terminated";
  subtype: string;
  task_id?: string;
  session_id?: string;
  turns: number;
  cost_usd: number;
  duration_ms: number;
  errors?: string[];
  termination_reason?: string;
}

// --- Guardrail abort reason attached to AbortController.abort(reason) ---

export interface GuardrailAbortReason {
  type: "guardrail";
  guardrail: "stall_detected" | "empty_response" | "idle_timeout";
  turns: number;
  detail: string;
}

export function isGuardrailAbortReason(
  value: unknown,
): value is GuardrailAbortReason {
  return (
    typeof value === "object" &&
    value !== null &&
    (value as Record<string, unknown>).type === "guardrail"
  );
}
