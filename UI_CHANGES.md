# VectorLab UI polish audit

Branch: `ui-polish`. Baseline: `pre-ui-polish` on `main`.

## Before audit

Both 1366×768 and 1920×1080 full-page captures are in `ui/before/`; `audit.json` records response codes and overflow measurements. The issued job is read-only, so separate synthetic fixtures supplied active station and source-crop review captures. Their audit files record 200 responses and zero page overflow. Screenshots remain local and are ignored by Git because the 76 full-page captures exceed 100 MB.

| Screen | Baseline finding / action |
|---|---|
| Customer home | Clear primary request action; spacious, but summary and history need consistent card rhythm. |
| Customer new request | Long field labels and help copy need consistent vertical spacing. |
| Customer status | Download action follows the planned-test table; timeline makes the page long. Lead with report availability and keep updates scannable. |
| Admin dashboard | Clear summary tiles; long identifiers wrap unevenly in job rows. Align spacing and badges. |
| Admin job | Long title dominates the header; file, sample, status and report number are not presented as a compact strip. |
| Admin import | Forms and supporting copy compete; strengthen section spacing and field help styling. |
| Admin mapping | Wide mapping tables need contained scrolling and better text wrapping. |
| Admin archive | Report references and statuses need the same badge/table rhythm. |
| Admin audit | Long hashes and IDs need safe wrapping; history is secondary content. |
| Station job tests | Station table is dense; long usernames and actions need room. |
| Station entry | Dense four-column grid mixed BT, AT and other values; active-job capture confirms its fields and inline messages. |
| Admin source review | Source page, crop and decision were vertically separated in the review card; key controls sat below the initial viewport. |
| Station report | Report action links compete at the top of a very long HTML page. |
| Quality queue | Empty state is clear; spacing differs from other cards. |
| Quality report | Approval context and report actions compete with long evidence. |
| HoD queue | Empty state is clear; spacing differs from other cards. |
| HoD report | Full HTML report is extremely long; primary approval/delivery context is hard to scan. |
| HoD delivery | Form and report context need consistent spacing and alerts. |
| Public verification | Standalone page is unstyled, with a small VALID heading and cramped details/upload form. Result must lead. |

## Changes

- Bundled the existing CSS cascade and the new design-system rules into one served `ui-polish.css`. The old CSS source files remain in the repository for traceability; the app shell loads only the bundled stylesheet. It defines palette, type, spacing, card, table, form, button, badge, alert and verification tokens. No renderer styles were changed.
- Reworked the public verification markup so VALID / NOT VALID leads the page and the existing document details and upload control follow in a clean card. The result wording, form field names and action remain unchanged.
- Moved the existing customer report section before the planned-test table and placed the existing updates timeline in a disclosure.
- Added a compact job identity strip using existing file, sample, customer and report-stage data. The issued report number is rendered from the same existing report fields used by the allocation helper; a focused test compares the displayed number with the helper output.
- Made utility Search and station Lock visually secondary while preserving their buttons, actions, names and selectors.
- Placed the existing station value input in explicit Before test (BT), After test (AT), or Other value columns. IDs, names, status controls, inline errors and save actions remain on the same elements.
- Placed each reviewed value, source crop and confirmation controls in a two-column evidence grid, beside the full source page. The same inputs and confirmation button remain available.

## Before / after evidence

Each pair exists at both `1366x768` and `1920x1080`; replace the size suffix to inspect the larger view.

| Role / screen | Before | After |
|---|---|---|
| Customer home | [before](ui/before/customer-home-1366x768.png) | [after](ui/after/customer-home-1366x768.png) |
| Customer request | [before](ui/before/customer-new-request-1366x768.png) | [after](ui/after/customer-new-request-1366x768.png) |
| Customer status | [before](ui/before/customer-status-1366x768.png) | [after](ui/after/customer-status-1366x768.png) |
| Admin dashboard | [before](ui/before/admin-dashboard-1366x768.png) | [after](ui/after/admin-dashboard-1366x768.png) |
| Admin job | [before](ui/before/admin-job-1366x768.png) | [after](ui/after/admin-job-1366x768.png) |
| Admin import | [before](ui/before/admin-import-1366x768.png) | [after](ui/after/admin-import-1366x768.png) |
| Admin mapping | [before](ui/before/admin-mapping-1366x768.png) | [after](ui/after/admin-mapping-1366x768.png) |
| Admin archive | [before](ui/before/admin-archive-1366x768.png) | [after](ui/after/admin-archive-1366x768.png) |
| Admin audit | [before](ui/before/admin-audit-1366x768.png) | [after](ui/after/admin-audit-1366x768.png) |
| Admin source review | [before](ui/before/admin-source-review-1366x768.png) | [after](ui/after/admin-source-review-1366x768.png) |
| Station job | [before](ui/before/station-job-tests-1366x768.png) | [after](ui/after/station-job-tests-1366x768.png) |
| Active station entry | [before](ui/before/station-entry-1366x768.png) | [after](ui/after/station-entry-1366x768.png) |
| Station report | [before](ui/before/station-report-1366x768.png) | [after](ui/after/station-report-1366x768.png) |
| Quality queue | [before](ui/before/quality-queue-1366x768.png) | [after](ui/after/quality-queue-1366x768.png) |
| Quality report | [before](ui/before/quality-report-1366x768.png) | [after](ui/after/quality-report-1366x768.png) |
| HoD queue | [before](ui/before/hod-queue-1366x768.png) | [after](ui/after/hod-queue-1366x768.png) |
| HoD report | [before](ui/before/hod-report-1366x768.png) | [after](ui/after/hod-report-1366x768.png) |
| HoD delivery | [before](ui/before/hod-delivery-1366x768.png) | [after](ui/after/hod-delivery-1366x768.png) |
| Public verification | [before](ui/before/public-verification-1366x768.png) | [after](ui/after/public-verification-1366x768.png) |

## Verification

| Check | Result |
|---|---|
| Full Django suite | PASS — 252 tests. |
| Unchanged Playwright synthetic flow | PASS — `UI_FLOW_PASS`, 31 seconds on the final UI, signed report, customer download, QR VALID, tampered NOT VALID, audit screen. SMTP absent: clearly marked unsent outbox draft. |
| Horizontal page scroll | PASS — zero document-level overflow at 1366 and 1920 on all 34 issued-job captures, active station form and source-crop review. Wide tables scroll inside their cards. |
| Text fit | PASS in inspected screenshots; long sidebar names use ellipsis with their existing full-value title tooltip, IDs wrap, buttons and badges remain readable. |
| Primary action | PASS for the audited task screens: utility Search and station Lock use secondary styling; required actions remain visible. |
| AA contrast | PASS on captured text at both widths: zero computed low-contrast groups on the 34 issued-job captures, active station form and source-crop review. Token pairs: accent/ink 5.16:1, muted/white 6.04:1, pass 6.90:1, near 5.76:1, fail 5.87:1, blocked 6.16:1. The calculation inherits solid backgrounds and excludes disabled controls; it is not a substitute for a manual accessibility audit. |
| Field names/IDs, URLs, permissions, logic | PASS — template/CSS diff only; Playwright flow unchanged. |
| Issued PDF | PASS — exact same 74,529 bytes, SHA-256 `2e432fa5c9dd9eca1c55c9c9e2ec8d09d6d904e400a909867ef71c015769fa60`; 18 pages and extracted text hash identical. |

**Merge decision:** all specified visual checks and the final regression passed. The presentation-only changes were merged into `main`. No view, review-status or report logic was changed.
