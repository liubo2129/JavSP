"""结构化事件总线。

这个模块是终端 UI 与 Web UI 的共同数据源。设计原则：

1. **不依赖项目内其它模块**，避免循环导入，也便于父子进程各自导入。
2. **不依赖 confz/pydantic**，因为 worker 子进程会在 import 主模块之后使用它，
   而主模块的导入带有全局副作用。
3. 事件载荷必须能安全地序列化成 JSON。`Event` 只接受 JSON 原生类型。

传输格式为 NDJSON（每行一个 JSON 对象），因为它可以简单地叠在子进程的
stdout 上：父进程按行读取即可，无需额外的 IPC 机制，也便于落盘做回归对照。
"""
from __future__ import annotations

import json
import logging
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional


class EventKind:
    """事件类型常量。

    命名遵循 ``<对象>.<动作>`` 的约定，前端可直接按前缀分组渲染。

    这里刻意用普通类的字符串常量，而不是 ``str`` 混入的 Enum：
    后者会让成员同时是 ``str`` 实例，导致 ``isinstance(kind, EventKind)`` 判断
    失效、并在再次构造枚举时抛 ValueError。事件名本就是协议字符串，
    用常量更简单，也允许直接传字符串。
    """
    # 运行生命周期
    RUN_STARTED = 'run.started'
    RUN_FINISHED = 'run.finished'
    RUN_FAILED = 'run.failed'
    # 扫描阶段
    SCAN_STARTED = 'scan.started'
    SCAN_PROGRESS = 'scan.progress'
    SCAN_FINISHED = 'scan.finished'
    # 按番号获取模式没有目录扫描阶段，用该事件告知前端
    SCAN_SKIPPED = 'scan.skipped'
    # 单部影片
    MOVIE_STARTED = 'movie.started'
    MOVIE_FINISHED = 'movie.finished'
    MOVIE_FAILED = 'movie.failed'
    MOVIE_STEP = 'movie.step'
    # 单个抓取站点
    CRAWLER_STARTED = 'crawler.started'
    CRAWLER_SUCCEEDED = 'crawler.succeeded'
    CRAWLER_FAILED = 'crawler.failed'
    CRAWLER_RETRY = 'crawler.retry'
    # 资源下载
    DOWNLOAD_PROGRESS = 'download.progress'
    # 自由日志
    LOG = 'log'

    @classmethod
    def values(cls) -> frozenset:
        """所有已知事件类型，供校验与前端枚举使用"""
        return frozenset(
            v for k, v in vars(cls).items()
            if not k.startswith('_') and isinstance(v, str)
        )


def now_iso() -> str:
    """当前时间的 ISO-8601 字符串（UTC，带时区）"""
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


@dataclass
class Event:
    """一条结构化事件

    Attributes:
        kind: 事件类型
        payload: 事件载荷，必须是 JSON 可序列化的原生类型
        ts: 事件产生时间
    """
    kind: EventKind
    payload: Dict[str, Any] = field(default_factory=dict)
    ts: str = field(default_factory=now_iso)

    def __post_init__(self):
        # kind 必须是字符串（EventKind 的常量本身就是 str，无需转换）
        if not isinstance(self.kind, str):
            raise TypeError(f'kind 必须是字符串，收到 {type(self.kind).__name__}')
        if not isinstance(self.payload, dict):
            raise TypeError(f'payload 必须是 dict，收到 {type(self.payload).__name__}')

    @property
    def kind_value(self) -> str:
        """事件类型的字符串值。kind 既可传 EventKind 常量也可传普通字符串。"""
        return self.kind

    def to_dict(self) -> Dict[str, Any]:
        return {'kind': self.kind, 'ts': self.ts, 'payload': self.payload}

    def to_json(self) -> str:
        # ensure_ascii=False 让中文在 NDJSON 里保持可读，也避免无谓的体积膨胀
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(',', ':'))

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> 'Event':
        return cls(kind=d['kind'], payload=d.get('payload') or {}, ts=d.get('ts') or now_iso())

    @classmethod
    def from_json(cls, line: str) -> 'Event':
        return cls.from_dict(json.loads(line))


# 事件接收方。None 表示不关心事件（例如纯 CLI 运行）。
# 约定两种等价用法：sink(kind, **payload)，或 sink(event)。
EventSink = Optional[Callable[..., None]]


class NullSink:
    """什么都不做的 sink。用它代替大量 ``if sink is not None`` 判断。"""

    def emit(self, kind: EventKind | str, **payload) -> None:
        pass

    def __call__(self, kind, **payload) -> None:
        pass

    def child(self, **prefix) -> 'NullSink':
        return self


class JsonlSink:
    """把事件以 NDJSON 写入一个文本流（通常是 worker 的 stdout）

    写操作加锁：抓取阶段每条事件都来自不同的抓取线程。
    """

    def __init__(self, stream=None):
        self.stream = stream if stream is not None else sys.stdout
        self._lock = threading.Lock()
        self._prefix: Dict[str, Any] = {}
        self.count = 0

    def child(self, **prefix) -> 'JsonlSink':
        """派生出带固定字段的 sink，用于自动附加 movie_id 之类的上下文"""
        sink = JsonlSink.__new__(JsonlSink)
        sink.stream = self.stream
        sink._lock = self._lock
        sink._prefix = {**self._prefix, **prefix}
        sink.count = 0
        return sink

    def emit(self, kind: EventKind | str, **payload) -> None:
        event = Event(kind=kind, payload={**self._prefix, **payload})
        line = event.to_json()
        with self._lock:
            try:
                self.stream.write(line + '\n')
                self.stream.flush()
            except (BrokenPipeError, ValueError, OSError):
                # 父进程已退出（窗口被关闭）时不应让抓取线程崩溃
                pass
        self.count += 1

    def __call__(self, kind, **payload) -> None:
        """支持 sink(event)：便于把 logging.Handler 等既有接口直接接上来"""
        if isinstance(kind, Event):
            self.emit(kind.kind, **kind.payload)
            return
        self.emit(kind, **payload)


class CollectorSink:
    """把事件收集到内存列表，供测试与无头场景使用"""

    def __init__(self):
        self.events: List[Event] = []
        self._lock = threading.Lock()
        self._prefix: Dict[str, Any] = {}

    def child(self, **prefix) -> 'CollectorSink':
        sink = CollectorSink.__new__(CollectorSink)
        sink.events = self.events
        sink._lock = self._lock
        sink._prefix = {**self._prefix, **prefix}
        return sink

    def emit(self, kind: EventKind | str, **payload) -> None:
        event = Event(kind=kind, payload={**self._prefix, **payload})
        with self._lock:
            self.events.append(event)

    def __call__(self, kind, **payload) -> None:
        if isinstance(kind, Event):
            self.emit(kind.kind, **kind.payload)
            return
        self.emit(kind, **payload)

    def kinds(self) -> List[str]:
        return [e.kind for e in self.events]


def read_events(stream: Iterable[str]) -> Iterable[Event]:
    """从逐行的 NDJSON 流中解析事件，跳过无法解析的行

    无法解析的行不会静默丢弃：它们以 logger.warning 记录，因为那通常意味着
    某个模块把普通 print 混进了事件流。
    """
    logger = logging.getLogger(__name__)
    for line in stream:
        line = line.strip()
        if not line:
            continue
        # 只处理 JSON 对象行，容忍第三方库往同一 stdout 打印
        if not line.startswith('{'):
            logger.debug('事件流中出现非事件行，已跳过: %s', line[:200])
            continue
        try:
            yield Event.from_json(line)
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning('无法解析事件行（%s）: %s', e, line[:200])


def log_record_to_event(record: logging.LogRecord) -> Event:
    """把 logging.LogRecord 转成 log 事件

    用于把管线里既有的 logger 输出接到事件流上，而不必改写每一处日志调用。
    """
    payload: Dict[str, Any] = {
        'level': record.levelname,
        'logger': record.name,
        'message': record.getMessage(),
    }
    if record.exc_info:
        payload['exception'] = logging.Formatter().formatException(record.exc_info)
    return Event(kind=EventKind.LOG, payload=payload)


class EventStreamHandler(logging.Handler):
    """把日志转发到事件 sink 的 logging handler

    只应在 worker/GUI 模式下挂到 root logger 上。CLI 模式不挂，以免改变
    既有的终端输出行为，也避免与 print/tqdm 的既有重定向互相干扰。

    注意：参数必须是 **sink 对象**（支持 ``sink(event)`` 调用），不能传
    ``sink.emit`` 这类按 kind 优先的绑定方法，两者参数约定不同。
    """

    def __init__(self, sink: Callable[[Event], None], level=logging.INFO):
        super().__init__(level=level)
        self.sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.sink(log_record_to_event(record))
        except Exception:  # noqa: BLE001 - 日志失败不应影响业务
            self.handleError(record)
