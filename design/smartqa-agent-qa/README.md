## Request

> “The UI is too ugly.”
> “Agents have no self-serve way to borrow a device and see an app.”

Source: [CHE-2082](mention://issue/01a1765a-477b-7030-b320-992e95573dbd), Plan rev-6 comment `01a17697-fe0f-71ae-95bd-6c3e1be3c637`.

- Issue: [CHE-2096](mention://issue/01a1769a-ae5a-70b8-a296-321215806a40)
- Pages in scope: `phones-computers-leases`, `agent-qa-report`, `sessions-shell`
- Status: selected rev-3 direction A — awaiting fresh UX review; approved when PR #231 merges

The approved `sessions-v2.1` and `smartqa-verdicts` records remain unchanged. Rev-1 posted three directions for device/lease visibility and the agent QA report, plus a shell polish pass. Cheese selected A on 27 Oct 2026 (`Tôi chọn A`, comment `01a17b36-ffc2-75f4-9593-90cfe9824a4c`). Rev-2 remains as the reviewed candidate in PR history. Rev-3 corrects A only; B and C remain unchanged in rev-1. It addresses the gate finding and the remaining shared findings in UX verdict `01a17b84-61f7-7add-ba69-02dbe770c2e0` and leader reconciliation `01a17b88-6563-749f-b281-98268968d1ce`. Rev-3 retains synthetic examples for the S4.2 flow artifact, hard gate, first-run rubric, and T1–T5 data safeguards. The new head needs a fresh independent UX review and leader reconciliation.

The prior-loop search is recorded in Plan rev-6 S1.0: no matching loop was found after 21 Oct 2026. The current Hindsight direction page could not be read in this run: no Hindsight MCP resources were exposed, and `hindsight --help` returned exit 127 (`command not found`). The live X99 `/phones` route returned a blank capture after the page-load timeout; the capture was not used as a design reference. Existing X99 baseline files remain at `design/sessions-v2.1/live/`.
