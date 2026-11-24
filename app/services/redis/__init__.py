"""Redis implementation submodules for :mod:`app.services.redis_service`.

``app.services.redis_service`` stays the public API (and the test seam) — it
re-exports every name below. The split is by responsibility:

| module       | owns                                                        |
| ------------ | ----------------------------------------------------------- |
| `cache`      | the shared client pool, the Tier-1 RAM cache, cache_get/set  |
| `config`     | URL parsing/masking, secret scrubbing, .env + DB persistence |
| `installer`  | server detection, start/stop/restart, portable install       |
| `panel`      | probes, status metrics, key browsing, flushes                |

Nothing in this package imports ``app.services.redis_service`` at import time;
the facade is looked up on ``sys.modules`` at call time by the seam helper in
each module, so a ``monkeypatch.setattr(redis_service, ...)`` is still observed.
"""

from __future__ import annotations
