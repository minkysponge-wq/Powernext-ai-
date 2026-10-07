# Security and data boundaries

Role checks, separation of duties, privileged MFA, upload validation, private media storage, issued-PDF hashing and append-only audit chains are implemented in the Django app. Deployment requires independent `DJANGO_SECRET_KEY` and `AUDIT_CHAIN_KEY`, a protected signing identity, HTTPS, and controlled SMTP credentials. Keep those values outside Git and load them from environment variables. The checked-in `.env.example` contains placeholders only.

Customer PDFs, the local database, signing identities and real-job review workbooks are not submission assets. `source/`, `app/private/`, `private/` and `output/` are ignored. Do not package them. Run `python app/manage.py check --deploy` with the intended production configuration and verify the audit chain before a live demonstration.

Uploaded source documents use the `PRIVATE_STORAGE` environment setting (`/data/private` in Compose). Keep any real customer source pack in local ignored `private/` storage.

The calculation worksheet and benchmark examples included here are fictional. For the optional private 37-cell importer, set `VECTORLAB_PRIVATE_REVIEW_WORKBOOK` and `VECTORLAB_PRIVATE_REVIEW_JOB_ID` explicitly; neither its workbook nor a customer job is included.
