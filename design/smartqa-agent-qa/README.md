## Request

> “The UI is too ugly.”
> “Agents have no self-serve way to borrow a device and see an app.”

Source: [CHE-2082](mention://issue/01a1765a-477b-7030-b320-992e95573dbd), Plan rev-6 comment `01a17697-fe0f-71ae-95bd-6c3e1be3c637`.

- Issue: [CHE-2096](mention://issue/01a1769a-ae5a-70b8-a296-321215806a40)
- Pages in scope: `phones-computers-leases`, `agent-qa-report`, `sessions-shell`
- Status: exploring

The approved `sessions-v2.1` and `smartqa-verdicts` records remain unchanged. This revision adds three directions for device/lease visibility and the agent QA report, plus a shell polish pass. Report content uses synthetic examples and represents the S4.2 flow artifact, hard gate, first-run rubric, and T1–T5 data safeguards.

The prior-loop search is recorded in Plan rev-6 S1.0: no matching loop was found after 21 Oct 2026. The current Hindsight direction page could not be read in this run: no Hindsight MCP resources were exposed, and `hindsight --help` returned exit 127 (`command not found`). The live X99 `/phones` route returned a blank capture after the page-load timeout; the capture was not used as a design reference. Existing X99 baseline files remain at `design/sessions-v2.1/live/`.
