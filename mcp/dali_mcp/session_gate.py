"""Prevent target switching while a tool call or background operation is alive."""
from contextlib import contextmanager
import threading


class SessionGate:
    def __init__(self):
        self.lock=threading.Lock()
        self.calls=0
        self.switching=False

    @contextmanager
    def operation(self):
        with self.lock:
            if self.switching:raise RuntimeError('gateway selection in progress')
            self.calls+=1
        try:yield
        finally:
            with self.lock:self.calls-=1

    @contextmanager
    def selection(self,controllers):
        # All tool entrypoints must use operation(), including starting/cancelling
        # background jobs. Worker completion can race here only toward quiescence.
        with self.lock:
            if self.switching or self.calls:raise RuntimeError('tool call active; cannot switch gateway')
            for controller in controllers:
                worker=getattr(controller,'worker',None)
                if getattr(controller,'active',None) or (worker is not None and worker.is_alive()):
                    raise RuntimeError('background task active; cannot switch gateway')
            self.switching=True
        try:yield
        finally:
            with self.lock:self.switching=False
