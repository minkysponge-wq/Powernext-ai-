# Judge walkthrough

Run `python demo/seed_station_entry.py --apply` from the repository root to create the synthetic walkthrough, then use the locally saved access file. Keep the six roles separate. Record the job ID and demonstrate:

1. Customer creates a request and sees its status.
2. Admin confirms scope and assigns a station.
3. Station Engineer records a reading and source reference.
4. Engineer reviews fields, inspects the audit trail and locks a draft.
5. Quality verifies or returns the report.
6. HoD completes MFA, approves and signs the synthetic certificate.
7. Customer sees delivery; the QR endpoint shows report number and document hash. Altered bytes fail verification.

Use `demo/screens/` only as a backup visual sequence. The source-backed HVD replay requires the private job data and is intentionally excluded from a public clone. The demo signer is self-signed and must be labelled as such.
