"""작업 진행 상황: 처리한 바이트/항목 수, 일시 정지, 취소.

작업 스레드(여러 개)가 advance()/item_done()으로 진행을 알리고, 화면은 값을 읽기만 한다.
취소하면 다음 advance()/item_done()에서 Cancelled가 발생해 작업이 멈춘다.
"""
import threading
import time


class Cancelled(Exception):
    pass


class Progress:
    def __init__(self):
        self.lock = threading.Lock()
        self.total_bytes = 0
        self.done_bytes = 0
        self.total_items = 0
        self.done_items = 0
        self.name = ""
        self.cancelled = False
        self.started = time.monotonic()
        self.paused_for = 0.0  # 일시 정지했던 시간 합계 (경과 시간에서 뺀다)
        self._paused_at = None
        self._running = threading.Event()
        self._running.set()

    # ---- 작업 스레드에서 호출 ----
    def begin(self, total_bytes: int, total_items: int):
        with self.lock:
            self.total_bytes = total_bytes
            self.total_items = total_items

    def advance(self, nbytes: int):
        self._check()
        with self.lock:
            self.done_bytes += nbytes

    def item_done(self, name: str):
        self._check()
        with self.lock:
            self.done_items += 1
            self.name = name

    def _check(self):
        if not self._running.is_set():
            self._running.wait()  # 일시 정지 중이면 여기서 기다린다
        if self.cancelled:
            raise Cancelled("작업을 취소했습니다.")

    # ---- 화면에서 호출 ----
    def cancel(self):
        self.cancelled = True
        self._running.set()  # 일시 정지 중이어도 깨워서 바로 멈추게

    @property
    def paused(self) -> bool:
        return not self._running.is_set()

    def pause(self):
        if not self.paused:
            self._paused_at = time.monotonic()
            self._running.clear()

    def resume(self):
        if self.paused:
            self.paused_for += time.monotonic() - self._paused_at
            self._paused_at = None
            self._running.set()

    def elapsed(self) -> float:
        now = self._paused_at if self.paused else time.monotonic()
        return now - self.started - self.paused_for
