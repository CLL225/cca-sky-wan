# Report sources

Measurement reports, vendor documentation, and other non-peer-reviewed
material. The shipped knowledge base currently cites no reports; this
directory is a placeholder for future sources.

When adding one:

1. Assign `source_type: report` (or the closest allowed enum value) in
   `knowledge/sources/src-NNN.yaml`.
2. Record the publisher and a stable URL. A report that cannot be retrieved
   later is a weak source; say so in `limitations`.
3. Prefer reports for deployment observations (what a production path
   actually does) over mechanism claims (which the RFCs and papers cover).
