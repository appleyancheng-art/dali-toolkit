"""Request-local, bounded progress delivery; never performs or retries bus I/O."""
from contextvars import ContextVar
import json
import anyio

_sink = ContextVar('dali_progress_sink', default=None)
LIVE_KINDS = frozenset(('live-output', 'live-channel', 'live-group'))


def publish_saved(prefix, ident, report):
    sink = _sink.get()
    if sink is None or prefix not in LIVE_KINDS:
        return
    # Copy only compact evidence AFTER durable publication. No mutable report,
    # parameters, cached profiles or bulk response arrays cross the thread.
    event = {'kind': prefix, 'operation_id': ident, 'state': report.get('state'),
             'progress': dict(report.get('progress') or {}),
             'commands_recorded': len(report.get('commands', [])),
             'verification_records': len(report.get('verification', []))}
    try:
        sink(event)
    except Exception:
        # Notifications are optional. Losing one must not change device execution.
        pass


async def run_with_progress(context, function, *args):
    """Keep synchronous bus ownership; coalesce notifications to the latest event.

    The worker is not abandoned on task cancellation. No second physical request
    is created. Notification transport failures do not fail or replay the worker.
    Progress is a monotonically increasing notification count, not a percentage;
    phase-local completed/total and exact operation ID are in the JSON message.
    """
    send, receive = anyio.create_memory_object_stream(1)

    def offer(event):
        try:
            send.send_nowait(event)
        except anyio.WouldBlock:
            try:
                receive.receive_nowait()
            except anyio.WouldBlock:
                pass
            send.send_nowait(event)

    def worker():
        token = _sink.set(lambda event: anyio.from_thread.run_sync(offer, event))
        try:
            try:
                return True, function(*args)
            except Exception as exc:
                return False, exc
        finally:
            _sink.reset(token)

    async def notify():
        sequence = 0
        async with receive:
            async for event in receive:
                sequence += 1
                try:
                    # Bound a slow/unavailable subscriber without blocking the bus.
                    with anyio.move_on_after(.25):
                        await context.report_progress(sequence, None, json.dumps(event, ensure_ascii=True))
                except Exception:
                    pass

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(notify)
        try:
            ok, value = await anyio.to_thread.run_sync(worker, abandon_on_cancel=False)
        finally:
            await send.aclose()
    if not ok:
        raise value
    return value
