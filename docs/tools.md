# MCP tools

All eight tools are annotated `readOnlyHint: true`, `destructiveHint: false`, `idempotentHint: true`,
`openWorldHint: false`, publish a JSON `outputSchema` generated from Pydantic models, and advertise
`additionalProperties: false`. The caller's identity always comes from the bearer token.

| Tool | Inputs | Output | Needs |
| --- | --- | --- | --- |
| `whoami` | none | `Identity`: subject, role, effective scopes, business units, supplier id | `ledger:access` |
| `list_invoices` | `ledger` AP/AR, optional `status`, `counterparty_id`, `business_unit_id`, `due_before`, `due_after`, `cursor`, `limit` 1-50 (default 20) | `InvoicePage` | AP: `ap:read` or `ap:read:own`; AR: `ar:read` |
| `get_invoice_details` | `invoice_id` (`^A[PR]-\d{6}$`) | `InvoiceDetail` incl. payments, counterparty, masked tax id, untrusted `memo` | as above |
| `get_invoice_payment_status` | `invoice_id` | `PaymentStatus`: paid, outstanding, last payment, days overdue | as above |
| `search_suppliers` | `query` 2-60 chars, `cursor`, `limit` | `SupplierPage`, tax ids masked unless `sensitive:read` | `suppliers:read` |
| `summarize_payables` | `group_by` status / counterparty / business_unit | `LedgerSummary` with totals | `ap:read` (portal users excluded) |
| `summarize_receivables` | `group_by` | `LedgerSummary` | `ar:read` |
| `get_overdue_invoices` | `ledger`, `min_days_overdue` 0-365, `cursor`, `limit` | `InvoicePage`, most overdue first | as `list_invoices` |

## Errors

Failures are tool results with `isError: true` and a stable code prefix, never stack traces.

| Code | When |
| --- | --- |
| `INVALID_ARGUMENT` | Schema violation (enforced by the SDK before the tool runs), unknown argument, impossible date, counterparty from the wrong ledger |
| `FORBIDDEN` | Scope missing, business unit outside the caller's access, supplier-portal user asking for another supplier or for aggregates |
| `NOT_FOUND` | Record missing **or** outside the caller's row scope (deliberately indistinguishable) |
| `INVALID_CURSOR` | Malformed or tampered cursor, or a cursor issued for another user, tool, filter set or page size |
| HTTP 401 | Missing, expired, wrongly signed or wrong-audience token (before any tool runs), with a `WWW-Authenticate` header pointing to the protected-resource metadata |

## Pagination

Keyset pagination on `(due_date, invoice_id)` for invoices and `(name, supplier_id)` for suppliers. The cursor is
opaque base64url JSON holding the last key and a fingerprint of `(tool, subject, filters, limit)`. A cursor only
works for the exact query and user that produced it, and the page size is capped at 50.

## Row scope

| Principal | Role | AP | AR | Supplier directory | Tax ids |
| --- | --- | --- | --- | --- | --- |
| `ava.ap` | AP analyst | BU-EAST, BU-WEST | no | BU-EAST, BU-WEST | masked |
| `ben.ar` | AR analyst | no | BU-EAST | no | n/a |
| `cora.controller` | Controller | all | all | all | full |
| `dev.supplier` | Supplier portal (SUP-0007) | own invoices, no aggregates | no | no | masked |
| `fay.supplier` | Supplier portal (SUP-0012) | own invoices, no aggregates | no | no | masked |

Effective scopes are the intersection of the token's `scope` claim and the principal's allowed scopes, so a
narrower token narrows access and a broader token cannot widen it.
