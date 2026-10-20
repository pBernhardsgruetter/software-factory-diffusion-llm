// Minimal pi extension: PreToolUse-equivalent. Logs every tool call (event JSON only) to $GATE_LOG
// and blocks bash commands containing "BLOCKME". Load with: pi -e spikes/s2_pi_gate.ts
import { appendFileSync } from "node:fs";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export default function (pi: ExtensionAPI) {
	pi.on("tool_call", async (event) => {
		const log = process.env.GATE_LOG;
		if (log) appendFileSync(log, JSON.stringify({ tool: event.toolName, input: event.input }) + "\n");
		if (event.toolName === "bash" && String((event.input as any).command ?? "").includes("BLOCKME")) {
			return { block: true, reason: "GATE-DENIED: command contains BLOCKME" };
		}
		return undefined;
	});
}
