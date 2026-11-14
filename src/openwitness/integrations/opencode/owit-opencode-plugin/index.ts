// Plugin de Open Witness para OpenCode 2.3.0 (SPEC §7.6, §17.3). Dueña: Juliana.
//
// No firma ni decide nada: reenvía cada evento a `owit hook …` con el mismo JSON que usa Claude Code, para
// reutilizar integrations/hooks.py sin cambios. Las variables OWIT_* las exporta `owit exec`; si faltan,
// `owit hook` no hace nada y el plugin tampoco.
//
// Eventos (verificados con OpenCode 2.3.0, ver bench/opencode_probe.py):
// - session.hook("prompt")       → `owit hook prompt`: firma el prompt y guarda el recibo.
// - tool.hook("execute.before")  → `owit hook pre-tool`: trae `tool`, `input` e `id` (el tool_use_id).
// - permission.hook("evaluate")  → pone `effect = "deny"` para la llamada que `pre-tool` rechazó.
import { Plugin } from "@opencode/plugin"
import { spawnSync } from "node:child_process"

type HookResult = { status: number; stdout: string; stderr: string }

function hookCommand(): string[] {
  // OWIT_HOOK_CMD es un arreglo JSON, p. ej. ["/ruta/a/owit"] o ["python", "-m", "openwitness.entry"].
  try {
    const cmd = JSON.parse(process.env.OWIT_HOOK_CMD ?? "")
    if (Array.isArray(cmd) && cmd.length > 0 && cmd.every((c) => typeof c === "string")) return cmd
  } catch {}
  return ["owit"]
}

function owitHook(kind: string, payload: unknown): HookResult {
  const [bin, ...args] = hookCommand()
  const r = spawnSync(bin, [...args, "hook", kind], {
    input: JSON.stringify(payload),
    encoding: "utf8",
    timeout: 20_000,
  })
  return { status: r.status ?? 1, stdout: r.stdout ?? "", stderr: (r.stderr ?? "") + (r.error ? String(r.error) : "") }
}

function denyReason(stdout: string): string | undefined {
  try {
    const out = JSON.parse(stdout)?.hookSpecificOutput
    if (out?.permissionDecision === "deny") return String(out.permissionDecisionReason ?? "Open Witness bloqueó la herramienta")
  } catch {}
  return undefined
}

export default Plugin.define({
  id: "openwitness",
  async setup(ctx) {
    if (!process.env.OWIT_SESSION) return // el agente no se lanzó con `owit exec`
    const denied = new Map<string, string>() // tool_use_id → motivo

    await ctx.session.hook("prompt", (event) => {
      const r = owitHook("prompt", {
        hook_event_name: "UserPromptSubmit",
        prompt: event.prompt.text,
        session_id: event.sessionID,
      })
      // `owit hook prompt` sale con 2 solo en modo estricto, cuando no pudo firmar: el prompt no debe enviarse.
      if (r.status === 2) throw new Error(r.stderr.trim() || "Open Witness: no se pudo firmar el prompt")
    })

    await ctx.tool.hook("execute.before", (event) => {
      const r = owitHook("pre-tool", {
        hook_event_name: "PreToolUse",
        tool_name: event.tool,
        tool_input: event.input ?? {},
        tool_use_id: event.id,
        session_id: event.sessionID,
      })
      const reason = denyReason(r.stdout)
      if (reason && event.id) denied.set(event.id, reason)
    })

    await ctx.permission.hook("evaluate", (event) => {
      const id = event.source?.type === "tool" ? event.source.id : undefined
      const reason = id ? denied.get(id) : undefined
      if (reason) {
        event.effect = "deny"
        event.message = reason
      }
    })

    await ctx.tool.hook("execute.after", (event) => {
      if (event.id) denied.delete(event.id)
    })
  },
})
