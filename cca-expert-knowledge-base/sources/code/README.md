# Code sources

Implementation snapshots are referenced by file and function, never copied
into the knowledge base. `knowledge/sources/src-005.yaml` points to the Linux
BBR implementation.

Cited locations:

| Source | File | Symbols used |
| --- | --- | --- |
| src-005 | `net/ipv4/tcp_bbr.c` | `bbr_init`, `bbr_main`, `bbr_set_cwnd`, `bbr_set_pacing_rate`, the `bbr_*_gain` tables, `BBR_UNIT`, `BW_SCALE` |

Locator style: `"linux/net/ipv4/tcp_bbr.c: bbr_init()"`. Keep the kernel
version in the source record's metadata; line numbers are not stable.

To refresh a snapshot locally:

```bash
curl -sS https://raw.githubusercontent.com/torvalds/linux/master/net/ipv4/tcp_bbr.c \
  -o sources/code/tcp_bbr.c
```

Do not commit the snapshot; the source record is the citation.
