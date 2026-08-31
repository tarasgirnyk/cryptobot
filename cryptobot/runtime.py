"""Спільний змінний стан процесу.

Виділено в окремий модуль без важких імпортів, щоб уникнути циклічних
залежностей між ``storage``, ``paper``, ``automation`` і ``telegram``.
"""

from __future__ import annotations

import threading
import time
from collections import deque


automation_state = {
    "paused": False,
    "killSwitch": False,
    "lastRunAt": None,
    "lastError": "",
    "telegramError": "",
    "marketStatus": "starting",
    "marketFailures": 0,
    "marketAlerted": False,
    "lastMarketSuccessAt": None,
    "lastReportAt": int(time.time() * 1000),
    "readinessNotified": False,
    "startupReconciled": True,
}

# symbol -> unix-час, до якого символ пропускається
symbol_cooldowns: dict[str, float] = {}

# Paper-позиції та журнал закритих угод.
paper_lock = threading.Lock()
paper_positions: dict[str, dict] = {}
paper_closed: deque = deque(maxlen=500)

# Live-позиції (demo/live) та журнал закритих live-угод.
live_lock = threading.Lock()
live_positions: dict[str, dict] = {}
live_closed: deque = deque(maxlen=500)
