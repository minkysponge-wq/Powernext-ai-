# VectorLab for CPRI Track 3

VectorLab carries a transformer test job from customer request through station readings, engineering review, Quality verification, HoD approval, and a controlled PDF certificate. Each reading retains its source and review state. The fixed `CPRI-SCL-TR-v1` report computes deterministic results from reviewed inputs and keeps drafts separate from issued certificates.

![VectorLab architecture](docs/architecture.png)

## Stack and prerequisites

Python 3.12, Django 5.2, Django Q2, PostgreSQL 17 for deployment (SQLite locally), ReportLab/pypdf/pyHanko for reports and signatures, Caddy/WhiteNoise for serving, Playwright with local Chrome for browser acceptance, and optional Gemini or local extraction. Install Node.js and Docker Compose for the browser-test and deployment paths. The pinned Python dependencies are in `requirements.txt` and `app/requirements.txt`; Playwright is pinned in `package.json`.

## Local setup

From the repository root on Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
npm ci
Copy-Item .env.example app/.env
# For local-only development, set DJANGO_DEBUG=1 and
# DJANGO_ALLOWED_HOSTS=127.0.0.1,localhost,testserver in app/.env.
.\.venv\Scripts\python.exe app/manage.py migrate
.\.venv\Scripts\python.exe app/manage.py seed_cpri_fixed_template
.\.venv\Scripts\python.exe app/manage.py seed_verdict_candidates
.\.venv\Scripts\python.exe app/manage.py seed_walkthrough_demo
.\.venv\Scripts\python.exe app/manage.py runserver
```

Run the task worker in another terminal:

```powershell
.\.venv\Scripts\python.exe app/manage.py qcluster
```

The walkthrough seed creates local demonstration users for the roles. Inspect the command output and set fresh passwords through Django's `changepassword`; no real passwords are supplied in this repository. Real customer records are not part of this repository.

`python demo/seed_station_entry.py` performs a read-only preflight; add `--apply` to replay the synthetic customer and station workflow through the app's HTTP screens.

## Docker Compose

Fill `app/.env` from the example with independent production secrets and deployment URLs, then run `docker compose -f app/compose.yaml --project-directory app up --build`. The Compose stack starts PostgreSQL, migrations, web, worker and Caddy. `deploy/` documents deployment files and the private-storage boundary.

## Test and workflow

Run `.\.venv\Scripts\python.exe app/manage.py test lab --noinput` (baseline: 241 passing) and `.\.venv\Scripts\python.exe app/manage.py check --deploy` with production environment values. Verify local audit chains with `.\.venv\Scripts\python.exe app/manage.py verify_audit_chain`.

1. **Customer:** submit a request and watch timestamped status changes.
2. **Admin:** register the sample, confirm scope and assign stations.
3. **Station Engineer:** enter readings in digital logsheets and attach source evidence.
4. **Engineer:** reconcile source readings, apply versioned rules and lock the draft.
5. **Quality:** verify evidence and return or approve the technical review.
6. **HoD:** authenticate with MFA, approve and sign the issued PDF; delivery and public verification follow.

See [demo script](docs/DEMO_SCRIPT.md) for a judge walkthrough and [architecture](docs/ARCHITECTURE.md) for the data flow.

## Problem statement coverage

| Need | VectorLab feature |
|---|---|
| Reduce manual report assembly | Fixed template, mapped readings, calculated tables, curves and PDF generation |
| Preserve measurement traceability | Source-page binding, human review state, versioned rules and append-only audit chain |
| Coordinate multiple roles | Customer, Admin, station, Engineer, Quality and HoD workflow with separation of duties |
| Control the legal certificate | Draft watermark, approval gate, report number, signature, PDF hash and verification page |
| Use AI safely | Gemini/local extraction assists transcription; critical readings still require human confirmation |

## Known limits

The demo signer is self-signed and does not establish institutional trust. Several IS clause numbers remain `[CLAUSE TBC]`, and rule set v2 awaits CPRI adoption. Real source scans, customer databases, reports and screenshots are excluded from this repository. The local application is not a production validation claim. See [rules](docs/RULES.md), [report template](docs/REPORT_TEMPLATE.md), and [security](docs/SECURITY.md).
