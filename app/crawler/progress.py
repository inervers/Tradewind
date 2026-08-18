from __future__ import annotations

import builtins
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar


ProgressSink = Callable[[str], None]

_progress_sink: ContextVar[ProgressSink | None] = ContextVar(
    "crawler_progress_sink", default=None,
)


@contextmanager
def use_progress_sink(sink: ProgressSink) -> Iterator[None]:
    """把当前线程/上下文的爬虫输出发送到指定任务，不修改全局 stdout。"""
    token = _progress_sink.set(sink)
    try:
        yield
    finally:
        _progress_sink.reset(token)


def report(*values: object, sep: str = " ", end: str = "\n") -> None:
    """兼容 print 的常用调用；无任务接收器时仍输出到启动终端。"""
    sink = _progress_sink.get()
    if sink is None:
        builtins.print(*values, sep=sep, end=end)
        return
    text = sep.join(str(value) for value in values)
    if end and end != "\n":
        text += end
    for line in text.splitlines() or [text]:
        if line.strip():
            sink(line)
