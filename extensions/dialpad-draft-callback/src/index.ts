import { Type } from "typebox";
import { defineToolPlugin } from "openclaw/plugin-sdk/tool-plugin";

export default defineToolPlugin({
  id: "dialpad-draft-callback",
  name: "Dialpad Draft Callback",
  description: "Submit a draft reply to the Dialpad webhook server.",
  configSchema: Type.Object({
    callbackUrl: Type.Optional(
      Type.String({ description: "Dialpad webhook draft-callback URL (default: http://127.0.0.1:8081/internal/draft-callback)" }),
    ),
  }),
  tools: (tool) => [
    tool({
      name: "submit_draft",
      label: "Submit Draft",
      description:
        "Submit a draft SMS reply to the Dialpad webhook server. The webhook renders the draft in a Telegram approval card for the operator.",
      parameters: Type.Object({
        jobId: Type.String({ description: "Draft callback job ID from the hook message" }),
        draft: Type.String({ description: "The draft reply text (plain text, no markdown)" }),
        token: Type.Optional(
          Type.String({ description: "Callback token from the hook message (X-Callback-Token header)" }),
        ),
      }),
      async execute({ jobId, draft, token }, config) {
        const url = config.callbackUrl ?? "http://127.0.0.1:8081/internal/draft-callback";
        const headers: Record<string, string> = {
          "Content-Type": "application/json",
        };
        if (token) {
          headers["X-Callback-Token"] = token;
        }
        const response = await fetch(url, {
          method: "POST",
          headers,
          body: JSON.stringify({ jobId, draft }),
          signal: AbortSignal.timeout(10000),
        });
        if (!response.ok) {
          return `Draft submission failed: ${response.status} ${response.statusText}`;
        }
        const result = await response.json().catch(() => ({}));
        const status = (result as { status?: string }).status ?? "unknown";
        if (status === "lost") {
          return `Draft was not delivered (timer already fired). Job ID: ${jobId}`;
        }
        return `Draft delivered successfully. Job ID: ${jobId}`;
      },
    }),
  ],
});