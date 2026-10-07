# Fixed report template

`CPRI-SCL-TR-v1` defines the report section order and explicit field mapping. The current local reviewed draft uses mapping v8. Its cover, one-row-per-test summary, engineering tables, heating curve, cross-test comparisons, deterministic observations and annexures are rendered by `app/lab/fixed_report.py` from reviewed fields. Unreviewed extracted values print as `[pending review]`; a provisional preview cannot be issued.

The annexes list source records and dates, instrument information, review provenance, rule provenance and abbreviations. The final certificate path requires Engineer lock, Quality verification and HoD approval with signing. A report number is allocated at approval. The real reviewed HVD draft and its source files remain in ignored private storage; the repository does not ship customer readings.
