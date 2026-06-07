"""
data/_retry.py — exponential backoff helper ใช้ร่วมกันทั้งโมดูล data
"""
from __future__ import annotations
import logging
import time
from typing import Callable, TypeVar

T = TypeVar("T")
log = logging.getLogger(__name__)


def retry(
    fn: Callable[[], T],
    *,
    attempts: int = 3,
    base_delay: float = 1.5,
    label: str = "",
) -> T:
    """
    เรียก fn() ซ้ำสูงสุด attempts ครั้ง พร้อม exponential backoff
    raise exception สุดท้ายถ้าเฟลครบ
    """
    last_err: Exception | None = None
    for i in range(1, attempts + 1):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 — เราอยาก catch กว้าง ๆ จริง ๆ
            last_err = e
            if i == attempts:
                break
            delay = base_delay * (2 ** (i - 1))
            log.warning("[%s] attempt %d/%d failed: %s — retry in %.1fs",
                        label or fn.__name__, i, attempts, e, delay)
            time.sleep(delay)
    assert last_err is not None
    raise last_err
