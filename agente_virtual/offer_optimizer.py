"""Selección controlada de dos invitaciones comerciales verificadas."""
from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Callable
from math import sqrt


def _wilson_interval(successes: int, total: int) -> tuple[float, float]:
    if total <= 0:
        return (0.0, 1.0)
    z = 1.96
    p = successes / total
    divisor = 1 + z * z / total
    center = (p + z * z / (2 * total)) / divisor
    margin = z * sqrt((p * (1 - p) / total) + z * z / (4 * total * total)) / divisor
    return center - margin, center + margin


def select_winner(stats: dict) -> str | None:
    """Solo decide si hay volumen y una ventaja estadística clara."""
    try:
        a, b = stats["A"], stats["B"]
        na, nb = int(a["offers"]), int(b["offers"])
        sa, sb = int(a["notified"]), int(b["notified"])
        if min(na, nb) < 30 or min(sa, sb) < 0 or sa > na or sb > nb:
            return None
        if sa / na - sb / nb >= 0.05 and _wilson_interval(sa, na)[0] > _wilson_interval(sb, nb)[1]:
            return "A"
        if sb / nb - sa / na >= 0.05 and _wilson_interval(sb, nb)[0] > _wilson_interval(sa, na)[1]:
            return "B"
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        pass
    return None


class AdaptiveOfferOptimizer:
    def __init__(self, get_store: Callable, *, refresh_seconds: int = 21600):
        self.get_store = get_store
        self.refresh_seconds = refresh_seconds
        self.checked_at: float | None = None
        self.winner: str | None = None
        self.sample_sizes: dict = {}
        self._task: asyncio.Task | None = None

    async def refresh(self) -> None:
        store = self.get_store()
        if getattr(store, "backend_name", None) != "google_drive":
            return
        try:
            result = await store.request("offer_stats")
            stats = result["stats"]
            winner = select_winner(stats)
            self.winner, self.sample_sizes = winner, stats
        except (KeyError, TypeError, ValueError, RuntimeError):
            # Si el script no está actualizado o Drive falla, el ensayo sigue
            # repartido de forma estable; nunca se inventa un ganador.
            self.winner = None
        finally:
            self.checked_at = time.monotonic()

    def select(self, key: str) -> str:
        if ((self.checked_at is None
             or time.monotonic() - self.checked_at >= self.refresh_seconds)
                and (self._task is None or self._task.done())):
            self.checked_at = time.monotonic()
            self._task = asyncio.create_task(self.refresh())
        if self.winner:
            return self.winner
        digest = hashlib.sha256(key.encode()).digest()[0]
        return "A" if digest % 2 == 0 else "B"
