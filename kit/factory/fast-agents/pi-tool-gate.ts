// pi extension: the factory tool gate for the pi harness (fast_coder.harness = "pi").
// Loaded by fast-coder with `pi -e <root>/factory/fast-agents/pi-tool-gate.ts`.
// For bash, edit and write calls it runs <root>/factory/bin/tool-gate with a
// Claude-Code-shaped PreToolUse event on stdin and blocks the call when the gate
// says deny or ask (headless: nobody can answer an ask). Exit 0 with no decision
// defers, as in Claude Code, so the call runs. Unlike Claude Code it fails closed
// on a gate that can't run, exits non-zero or prints unparsable output.
// Adapted from spikes/s2_pi_gate.ts.
import { spawnSync } from "node:child_process";
import { dirname, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const TOOL_NAMES: Record<string, string> = { bash: "Bash", edit: "Edit", write: "Write" };

function gatePath(): string {
	if (process.env.FACTORY_TOOL_GATE) return process.env.FACTORY_TOOL_GATE;
	// <root>/factory/fast-agents/pi-tool-gate.ts -> <root>/factory/bin/tool-gate
	return resolve(dirname(fileURLToPath(import.meta.url)), "..", "bin", "tool-gate");
}

function toolInput(tool: string, input: any, cwd: string): Record<string, unknown> {
	const abs = (p: unknown) => {
		const s = String(p ?? "");
		return isAbsolute(s) ? s : resolve(cwd, s);
	};
	if (tool === "bash") return { command: String(input?.command ?? "") };
	if (tool === "write") return { file_path: abs(input?.path), content: String(input?.content ?? "") };
	const edits = Array.isArray(input?.edits) ? input.edits : [];
	return {
		file_path: abs(input?.path),
		old_string: edits.map((e: any) => String(e?.oldText ?? "")).join("\n"),
		new_string: edits.map((e: any) => String(e?.newText ?? "")).join("\n"),
	};
}

export default function (pi: ExtensionAPI) {
	pi.on("tool_call", async (event: any, ctx: any) => {
		const name = TOOL_NAMES[event.toolName];
		if (!name) return undefined;
		const cwd = ctx?.cwd ?? process.cwd();
		const payload = {
			session_id: ctx?.sessionManager?.getSessionId?.() ?? "",
			cwd,
			permission_mode: "dontAsk",
			hook_event_name: "PreToolUse",
			tool_name: name,
			tool_input: toolInput(event.toolName, event.input, cwd),
			tool_use_id: event.toolCallId ?? "",
		};
		const r = spawnSync(gatePath(), [], { input: JSON.stringify(payload), cwd, encoding: "utf8", timeout: 10_000 });
		if (r.error) return { block: true, reason: `factory tool-gate: could not run (${r.error.message})` };
		if (r.status === 2) return { block: true, reason: (r.stderr || "factory tool-gate: denied").trim() };
		if (r.status !== 0) return { block: true, reason: `factory tool-gate: exited ${r.status}` };
		if (!(r.stdout || "").trim()) return undefined;
		let decision = "";
		let reason = "";
		try {
			const out = JSON.parse(r.stdout);
			decision = out?.hookSpecificOutput?.permissionDecision ?? "";
			reason = out?.hookSpecificOutput?.permissionDecisionReason ?? "";
		} catch {
			return { block: true, reason: "factory tool-gate: unparsable output" };
		}
		if (decision === "deny" || decision === "ask") {
			return { block: true, reason: reason || `factory tool-gate: ${decision}` };
		}
		return undefined;
	});
}
