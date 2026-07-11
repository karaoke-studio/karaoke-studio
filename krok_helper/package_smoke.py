"""Non-interactive smoke checks executed by packaged build scripts."""

from __future__ import annotations

import multiprocessing as mp


def _square(value: int) -> int:
    return value * value


def run_spawn_smoke() -> int:
    """Exercise a real spawn Pool from the current (possibly frozen) executable."""
    context = mp.get_context("spawn")
    with context.Pool(2) as pool:
        result = pool.map_async(_square, [1, 2, 3, 4]).get(timeout=30)
    return 0 if result == [1, 4, 9, 16] else 1
