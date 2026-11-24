"""Volume / MPR backend, split by responsibility (FA-3).

| submodule   | owns                                                       |
| ----------- | ---------------------------------------------------------- |
| `slices`    | which instances form a volume, and in what order            |
| `assembly`  | the bundle (geometry + rows) and its tenant-scoped keys     |
| `cache`     | the voxel cache (L1 memory + L2 Redis)                      |
| `mpr`       | oblique/orthogonal slice resampling                         |
| `tiles`     | multi-slice PNG tiles                                       |
| `mip`       | slab MIP / MinIP / average / VRT                            |
| `sessions`  | server-session accounting, viewer-mode decision log         |

**The public surface is `app.services.volume_service`**, not this package. It
re-exports every name below and is what routers, workers and tests import, so
there is exactly one place where the API can drift. New code should import from
`app.services.volume_service` too, unless it is a sibling inside this package.

The dependency direction is one-way — `slices` → `assembly` → `cache` → `mpr` →
`tiles` / `mip`, with `sessions` standing alone — so there is no import cycle.
"""
