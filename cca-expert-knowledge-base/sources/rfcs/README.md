# RFC sources

RFC full text is not duplicated here; retrieve it from the RFC Editor. The
`knowledge/sources/src-NNN.yaml` records carry the canonical URL and section
locators.

| Source | RFC | Section used |
| --- | --- | --- |
| src-001 | RFC 5681 (TCP Congestion Control) | 2, 3.1, 4.2 |
| src-002 | RFC 9002 (QUIC Loss Detection and Congestion Control) | 6, 7, 7.6 |
| src-003 | RFC 3168 (ECN) | 6, 7 |
| src-007 | RFC 9438 (CUBIC for Fast and Long-Distance Networks) | 4, 4.2, 5 |

Download example (inside the container or on the host):

```bash
curl -sS https://www.rfc-editor.org/rfc/rfc9002.txt -o sources/rfcs/rfc9002.txt
```

If you keep local copies, add `sources/rfcs/*.txt` to `.gitignore`.
