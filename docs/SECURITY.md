# Security and data boundaries

Role checks, separation of duties, privileged MFA, upload validation, private media storage, issued-PDF hashing and append-only audit chains are implemented in the Django app. Deployment requires independent `DJANGO_SECRET_KEY` and `AUDIT_CHAIN_KEY`, a protected signing identity, HTTPS, and controlled SMTP credentials. Keep those values outside Git and load them from environment variables. The checked-in `.env.example` contains placeholders only.

Customer PDFs, the local database, signing identities and real-job review workbooks are not submission assets. `source/`, `app/private/`, `private/` and `output/` are ignored. Do not package them. Run `python app/manage.py check --deploy` with the intended production configuration and verify the audit chain before a live demonstration.

The original organiser PDF set is held locally at ignored `private/sources/`. A local ignored `source/` junction preserves legacy diagnostic paths; it is absent from fresh clones. Uploaded source documents use the `PRIVATE_STORAGE` environment setting (`/data/private` in Compose).

The public calculation worksheet and benchmark examples are fictional. The earlier source-based worksheet, manifest and source-specific scripts remain in ignored `private/`. The optional `load_reference_demo` command reads customer metadata from `private/reference/metadata.json`; its source transcription is intentionally absent from a fresh clone. For the private 37-cell importer, set `VECTORLAB_PRIVATE_REVIEW_WORKBOOK` and `VECTORLAB_PRIVATE_REVIEW_JOB_ID` explicitly. Historical Git commits still contain real-job material, so publish the cleaned tree only after the repository owner decides how to handle that history.
