# Frozen experiment sources

Byte-exact copies of shared sources that a published evidence bundle pins by
hash and that later changed in `scripts/` or `src/`. They are data for
`scripts/check_yokohama_pad_reentry_sources.py`, which restores a copy only when
its hash equals the bundle's pin. Do not import them from runtime code, and do
not edit them: an edited copy no longer matches its pin and is refused.

| Path | sha256 | Pinned by |
|---|---|---|
| `src/runtime/yokohama_pad_queue.py` | `528971136a196d127ab4a1cbd3960c7c07d39838dec9204518bcda2e4c56042b` | `yokohama-pad-reentry`, `yokohama-pad-reentry-learning` |
