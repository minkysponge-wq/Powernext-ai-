# Second synthetic form and report stress check

This check uses a **fictional second transformer form**, `SYN-UNSEEN-002`, with the same schema as the first demo and different customer, sample, serial, insulation resistance, principal-tap ratio, no-load current and winding-rise readings. It contains no customer data. The [one-page routine logsheet](../demo/forms/SYN-UNSEEN-002-routine.pdf) and [999-row CSV export](../demo/forms/SYN-UNSEEN-002-readings.csv) are generated together by `demo/create_unseen_form.py`.

## Method

On a fresh local SQLite database, `stress_unseen_report` creates a second synthetic job on `CPRI-SCL-TR-v1` v8, imports the CSV through the application's import endpoint, confirms all 999 rows start unreviewed, and saves every decision through the station review endpoint in 34 pages. The review decisions are automated **only for this fictional benchmark**; they are not evidence of human checking. The command then creates a draft through the report endpoint and renders the PDF 30 times serially. It does not approve, sign or deliver the report.

```powershell
$env:DJANGO_DEBUG='1'
$env:VECTORLAB_FORCE_MFA='0'
$env:VECTORLAB_SQLITE_PATH='C:\path\to\an-empty-local-stress.sqlite3'
.\.venv\Scripts\python.exe app\manage.py migrate --noinput
.\.venv\Scripts\python.exe demo\create_unseen_form.py
.\.venv\Scripts\python.exe app\manage.py stress_unseen_report --iterations 30
```

The command refuses a non-debug or non-empty job database. Its draft and machine-readable result go to ignored `output/stress-unseen/`.

## Result on 9 October 2026

| Measure | Result |
|---|---:|
| CSV rows imported as unreviewed | 999 |
| UI review pages saved | 34 |
| Template-bound fields still pending | 0 |
| Fixed draft pages | 18 |
| Serial PDF renders | 30 |
| Import time | 0.770 s |
| Automated synthetic review time | 4.774 s |
| PDF render median / p95 / max | 1.167 / 1.356 / 1.368 s |
| Python allocation peak during renders | 3.10 MiB |
| Rendered draft size | 66,708 bytes |

The candidate v2 checks evaluated to 15 PASS, 1 MARGINAL, 11 DESCRIPTIVE and 1 NOT APPLICABLE. The snapshot retains **28 rule-adoption blockers**, so this second job remains an unissued draft. Its pages contain the draft watermark and no `[pending review]` values. The short-circuit page, cover and annexure were visually inspected after rendering.

## Issue found and fixed

The first synthetic export used an empty unit for a deflection reading; station review correctly rejected it. A later export chose `VA` where the worksheet calculation requires the still-allowed `kVA`; that prevented loss calculation. The generator now preserves valid units and fills only missing or invalid ones.

A long source label initially made the short-circuit section spill onto page 19. The short-circuit tables now use slightly tighter vertical cell padding; the same long label fits on page 13 of the 18-page draft. A regression test repeats the full import, synthetic UI review and page-count check.

## Limits of this evidence

This is a **same-schema synthetic CSV import and report-render stress test**, not proof that OCR maps an unseen scanned PDF. The companion PDF is a readable visual form; its CSV twin is the input imported here. Thirty serial renders do not establish multi-user throughput or thousands-of-jobs capacity. The memory figure is Python allocation tracking, not total process memory. A genuinely unseen organiser form still needs a source-by-source extraction and human review trial before claiming production accuracy.
