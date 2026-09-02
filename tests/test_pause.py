"""
Offline test สำหรับโหมด /pause (runtime_flags + gate ใน main.send_telegram)

รันไม่ต่อเน็ต: inject fake watchlist.store · patch _send_telegram_raw
วิธีรัน:  .venv\\Scripts\\python.exe tests\\test_pause.py
"""
from __future__ import annotations

import os
import sys
import types

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_state: dict = {}


def _install_store() -> None:
    m = types.ModuleType("watchlist.store")
    m.load_json = lambda name, default=None: dict(_state) if _state else (default if default is not None else {})

    def _save(name, data):
        _state.clear()
        _state.update(data)
    m.save_json = _save
    sys.modules["watchlist.store"] = m


def main() -> int:
    fails: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            fails.append(msg)

    # ── runtime_flags บน fake store ──
    _install_store()
    import runtime_flags

    check(runtime_flags.is_paused() is False, "เริ่มต้น (ยังไม่ตั้ง) ต้องไม่ paused")
    runtime_flags.set_paused(True, by="test")
    check(runtime_flags.is_paused() is True, "หลัง set_paused(True) ต้อง paused")
    check(runtime_flags.status().get("alerts_paused") is True, "status ต้องมี alerts_paused=True")
    check(bool(runtime_flags.status().get("updated_at")), "status ต้องมี updated_at")
    runtime_flags.set_paused(False)
    check(runtime_flags.is_paused() is False, "หลัง set_paused(False) ต้องไม่ paused")

    # ── gate ใน main.send_telegram ──
    os.environ["ALERTS_SKIP_WEEKEND"] = "false"   # กันเสาร์-อาทิตย์มารบกวนผลเทส
    import main
    sent: list[str] = []
    main._send_telegram_raw = lambda msg, **kw: (sent.append(msg) or True)  # type: ignore[assignment]

    # case 1: ไม่ paused → ส่งจริง
    runtime_flags.is_paused = lambda: False  # type: ignore[assignment]
    os.environ.pop("IGNORE_PAUSE", None)
    sent.clear()
    main.send_telegram("a")
    check(sent == ["a"], f"ไม่ paused ต้องส่งจริง (sent={sent})")

    # case 2: paused + ไม่มี IGNORE_PAUSE → เงียบ (ไม่ส่ง แต่คืน True)
    runtime_flags.is_paused = lambda: True  # type: ignore[assignment]
    sent.clear()
    r = main.send_telegram("b")
    check(sent == [] and r is True, f"paused ต้องเงียบ+คืน True (sent={sent}, r={r})")

    # case 3: paused + IGNORE_PAUSE=true → ข้าม gate (สแกนที่ผู้ใช้สั่งเอง)
    os.environ["IGNORE_PAUSE"] = "true"
    sent.clear()
    main.send_telegram("c")
    check(sent == ["c"], f"paused แต่ IGNORE_PAUSE ต้องส่ง (sent={sent})")

    # case 4: อ่าน flag พัง → fail-open (ส่งตามปกติ)
    os.environ.pop("IGNORE_PAUSE", None)
    def _boom():
        raise RuntimeError("gcs down")
    runtime_flags.is_paused = _boom  # type: ignore[assignment]
    sent.clear()
    main.send_telegram("d")
    check(sent == ["d"], f"อ่าน flag พังต้อง fail-open ส่งจริง (sent={sent})")

    if fails:
        print("❌ FAIL:")
        for f in fails:
            print("  -", f)
        return 1
    print("✅ PASS — /pause: runtime_flags + gate (ส่ง/เงียบ/bypass/fail-open) ครบ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
