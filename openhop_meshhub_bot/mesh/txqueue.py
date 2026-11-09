"""Priority outbound LoRa transmission queue with rate pacing and 135-byte clamping."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

from .text import MAX_LORA_BYTES, clamp_lora_message

logger = logging.getLogger("openhop.meshhub-bot.txqueue")

PRIORITY_REPLY = 1
PRIORITY_ALERT = 2
PRIORITY_BROADCAST = 3


@dataclass(order=True)
class TxJob:
    priority: int
    seq: int
    target_type: str = field(compare=False)  # "channel" | "dm"
    target: Any = field(compare=False)       # channel index/name or contact name/pubkey
    text: str = field(compare=False)
    module: str = field(default="bot", compare=False)
    min_spacing: float = field(default=0.0, compare=False)
    future: asyncio.Future | None = field(default=None, compare=False)


class OutboundTxQueue:
    """Centralized priority queue for all outbound LoRa bot transmissions."""

    def __init__(
        self,
        send_fn: Callable[[str, Any, str], Coroutine[Any, Any, dict[str, Any]]],
        min_interval_seconds: float = 1.5,
        on_tx_callback: Callable[[TxJob, dict[str, Any]], None] | None = None,
    ):
        self._send_fn = send_fn
        self.min_interval_seconds = max(0.2, float(min_interval_seconds))
        self._on_tx_callback = on_tx_callback
        self._queue: asyncio.PriorityQueue[TxJob] = asyncio.PriorityQueue()
        self._seq = 0
        self._worker_task: asyncio.Task | None = None
        self._running = False
        self._last_tx_mono = 0.0
        self.total_sent = 0
        self.total_errors = 0

    def set_min_interval(self, seconds: float) -> None:
        self.min_interval_seconds = max(0.2, float(seconds))

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._worker_task = asyncio.create_task(self._worker_loop())

    async def stop(self) -> None:
        self._running = False
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await asyncio.wait_for(self._worker_task, timeout=1.5)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        self._worker_task = None
        # Cancel any pending futures so callers don't hang during shutdown
        while not self._queue.empty():
            try:
                job = self._queue.get_nowait()
                if job.future and not job.future.done():
                    job.future.set_result({"error": "TX queue stopped"})
                self._queue.task_done()
            except asyncio.QueueEmpty:
                break

    async def send_channel(
        self,
        channel: int | str,
        text: str,
        *,
        priority: int = PRIORITY_BROADCAST,
        module: str = "bot",
        min_spacing: float = 0.0,
        wait: bool = True,
    ) -> dict[str, Any]:
        return await self._enqueue(
            target_type="channel",
            target=channel,
            text=text,
            priority=priority,
            module=module,
            min_spacing=min_spacing,
            wait=wait,
        )

    async def send_dm(
        self,
        recipient: str,
        text: str,
        *,
        priority: int = PRIORITY_REPLY,
        module: str = "bot",
        min_spacing: float = 0.0,
        wait: bool = True,
    ) -> dict[str, Any]:
        return await self._enqueue(
            target_type="dm",
            target=recipient,
            text=text,
            priority=priority,
            module=module,
            min_spacing=min_spacing,
            wait=wait,
        )

    async def _enqueue(
        self,
        *,
        target_type: str,
        target: Any,
        text: str,
        priority: int,
        module: str,
        min_spacing: float,
        wait: bool,
    ) -> dict[str, Any]:
        clamped = clamp_lora_message(text, MAX_LORA_BYTES)
        if not clamped:
            return {"error": "Empty message"}

        # If worker isn't started (e.g. unit test calling directly), dispatch inline
        if not self._running:
            return await self._dispatch_job(
                TxJob(
                    priority=priority,
                    seq=0,
                    target_type=target_type,
                    target=target,
                    text=clamped,
                    module=module,
                    min_spacing=min_spacing,
                )
            )

        self._seq += 1
        loop = asyncio.get_running_loop()
        fut = loop.create_future() if wait else None
        job = TxJob(
            priority=priority,
            seq=self._seq,
            target_type=target_type,
            target=target,
            text=clamped,
            module=module,
            min_spacing=min_spacing,
            future=fut,
        )
        await self._queue.put(job)
        if fut is not None:
            return await fut
        return {"queued": True}

    async def _worker_loop(self) -> None:
        while self._running:
            try:
                job = await self._queue.get()
            except asyncio.CancelledError:
                break

            try:
                required_gap = max(self.min_interval_seconds, float(job.min_spacing or 0.0))
                elapsed = time.monotonic() - self._last_tx_mono
                if self._last_tx_mono > 0 and elapsed < required_gap:
                    await asyncio.sleep(required_gap - elapsed)

                result = await self._dispatch_job(job)
                self._last_tx_mono = time.monotonic()
                if job.future and not job.future.done():
                    job.future.set_result(result)
            except asyncio.CancelledError:
                if job.future and not job.future.done():
                    job.future.set_result({"error": "Cancelled"})
                break
            except Exception as exc:
                logger.error("Error in OutboundTxQueue worker: %s", exc, exc_info=True)
                if job.future and not job.future.done():
                    job.future.set_result({"error": str(exc)})
            finally:
                self._queue.task_done()

    async def _dispatch_job(self, job: TxJob) -> dict[str, Any]:
        try:
            res = await self._send_fn(job.target_type, job.target, job.text)
            if isinstance(res, dict) and "error" in res:
                self.total_errors += 1
            else:
                self.total_sent += 1
            if self._on_tx_callback:
                try:
                    self._on_tx_callback(job, res)
                except Exception:
                    pass
            return res
        except Exception as exc:
            self.total_errors += 1
            return {"error": str(exc)}
