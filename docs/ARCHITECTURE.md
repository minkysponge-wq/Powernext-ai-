# Architecture

![VectorLab architecture](architecture.png)

The Django app owns the customer request, job, source documents, readings, rule versions and reports. The station screens save individual readings with source and reviewer state. Engineering rules evaluate reviewed readings only. Quality and HoD are separate approval stages. The issued PDF is signed, hashed and exposed through a verification endpoint. A Django Q worker handles background extraction and delivery; optional Gemini or local extraction is a transcription aid.

```mermaid
flowchart LR
  C[Customer portal] --> D[Django job and workflow]
  S[Station entry] --> D
  D --> E[Reviewed evidence and source pages]
  E --> R[Versioned rules]
  R --> P[Fixed PDF template]
  P --> Q[Quality review]
  Q --> H[HoD approval and signing]
  H --> V[Delivery and QR verification]
  D --> A[Append-only audit chain]
  W[Django Q worker] --> X[Gemini or local extraction]
  X --> E
```

Runtime code remains under `app/` so Django imports, URLs, tables and migration names stay stable. The deployable Compose file remains at `app/compose.yaml`; `deploy/` holds operator guidance.
