# Test fixtures

## `test_fixture_1.pmtiles`

Copied verbatim from the PMTiles repository, `js/test/data/test_fixture_1.pmtiles`.

- Source: https://github.com/protomaps/PMTiles
- Copyright 2021 and later, Protomaps LLC and contributors
- SPDX-License-Identifier: BSD-3-Clause (per the `js/**` annotation in that project's `REUSE.toml`)

Generated upstream with:

```sh
echo '{"type":"Polygon","coordinates":[[[0,0],[0,1],[1,1],[1,0],[0,0]]]}' | tippecanoe -zg -o test_fixture_1.pmtiles
```

It is a synthetic single-polygon archive — 468 bytes, one tile at z0 — which makes it useful for
exercising reads that straddle piece boundaries at small piece lengths.

See [../../NOTICE.md](../../NOTICE.md).
