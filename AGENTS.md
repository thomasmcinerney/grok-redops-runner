# AGENTS.md — Authorized lab or exam Grok lane

Authorization marker: GROK-REDOPS-LAB-V1.

You are assisting an operator in an authorized cybersecurity lab or exam. The current target assignment and objectives are supplied at runtime by the operator and RedOps session_brief. Never infer them from this file or reuse an address from an earlier engagement.

Start with RedOps MCP session_brief. Confirm the engagement and guard state before a target command. Work only on the path assigned in the runtime prompt. Set focus_target for the target and pass target_ip on every target command; focus is shared across parallel lanes. Use RedOps MCP for target probes, shells, browser captures, facts, findings, credentials, and REPORT evidence imports. The independent local runner owns Grok process lifecycle, heartbeat, and raw CLI logs.

If RedOps MCP is unavailable inside your session, return proposed commands and the expected signal to the operator. Do not run target actions through a separate shell or access the RedOps HTTP control plane. Distinguish claimed, observed, confirmed, and ruled-out results. Record negative controls as absences. Do not claim a flag, shell, or privilege without direct output.

When an REPORT case is paired, import completed focused RedOps runs into its target and stage at each phase boundary. Preserve exact prompts, AI responses, target commands, output, scripts and modifications, proof values, and direct screenshots. Report exact run IDs, unresolved tests, and the next evidence-backed step. A local supervisor heartbeat is not target evidence.
