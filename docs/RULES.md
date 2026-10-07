# Engineering rules

Rules are versioned per job. V1 remains in history. The v2 definitions in `app/lab/management/commands/seed_verdict_v2.py` are team reviewed and **pending CPRI adoption**. The report shows that provisional status. Source clauses marked `[CLAUSE TBC]` must be confirmed by CPRI before issue.

Families covered include EEL total loss at the principal tap, impedance against declared tap values, before/after reactance stability, no-load current, temperature rise, ratio/vector group, dielectric observations, pressure/vacuum and oil leakage, and the overall short-circuit observation. Descriptive after-test and non-principal-tap loss rows have no separate EEL pass/fail limit. Every calculated verdict carries its measurement, boundary, margin and provenance when applicable.

The evaluator is in `app/lab/rule_v2.py`; worksheet arithmetic is in `app/lab/reviewed_calculations.py`. The v2.17 worksheet method uses √3 = 1.732 and documented intermediate precision. Do not treat provisional rule results as CPRI certification.
