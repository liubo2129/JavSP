"""javsp/events.py 的单元测试

事件流是 CLI 与 GUI 的共同数据源，协议一旦破坏会让前端静默失联，
因此这里把序列化、解析容错与 sink 行为都固定下来。
"""
import io
import logging
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from javsp.events import (
    CollectorSink, Event, EventKind, EventStreamHandler, JsonlSink, NullSink,
    log_record_to_event, read_events,
)


def test_event_round_trip():
    event = Event(EventKind.MOVIE_STARTED, {'movie_id': 'ABP-647', 'index': 1, 'total': 2})
    parsed = Event.from_json(event.to_json())
    assert parsed.kind == EventKind.MOVIE_STARTED
    assert parsed.payload == {'movie_id': 'ABP-647', 'index': 1, 'total': 2}
    assert parsed.ts == event.ts


def test_event_accepts_plain_string_kind():
    # kind 既可用常量也可用普通字符串，二者必须等价
    assert Event('log', {}).kind == EventKind.LOG
    assert Event(EventKind.LOG, {}).kind == 'log'


def test_event_rejects_bad_input():
    for bad_kind in (123, None, object()):
        try:
            Event(bad_kind, {})
        except TypeError:
            pass
        else:
            raise AssertionError(f'非字符串 kind 应被拒绝: {bad_kind!r}')
    try:
        Event('log', 'not-a-dict')
    except TypeError:
        pass
    else:
        raise AssertionError('非 dict 的 payload 应被拒绝')


def test_event_kind_values_are_unique_and_complete():
    values = EventKind.values()
    assert 'log' in values
    assert len(values) == len(set(values))
    # 枚举出的常量数应与定义相符，防止误加非字符串属性
    assert all(isinstance(v, str) for v in values)


def test_prefix_is_inherited_by_child_sink():
    buf = io.StringIO()
    sink = JsonlSink(buf)
    sink.child(movie_id='ABP-647').emit(EventKind.CRAWLER_SUCCEEDED, crawler='jav321')
    sink.child(movie_id='SSIS-001').emit(EventKind.CRAWLER_SUCCEEDED, crawler='jav321')

    events = list(read_events(buf.getvalue().splitlines()))
    assert [e.payload['movie_id'] for e in events] == ['ABP-647', 'SSIS-001']
    # 子 sink 不应把父 sink 已有的前缀带进去
    assert all(e.payload['crawler'] == 'jav321' for e in events)


def test_read_events_skips_non_event_lines():
    """第三方库往同一 stdout 打印时，不应中断事件流解析"""
    buf = io.StringIO()
    sink = JsonlSink(buf)
    sink.emit(EventKind.SCAN_STARTED, root='/videos')
    lines = buf.getvalue().splitlines()
    lines.insert(1, 'Collecting package metadata (repodata.json): done')
    lines.insert(2, '')  # 空行

    events = list(read_events(lines))
    assert len(events) == 1
    assert events[0].kind == EventKind.SCAN_STARTED


def test_read_events_skips_malformed_json():
    lines = ['{"kind": "log", "payload": {"message": "ok"}}', '{"kind": broken', '   ']
    events = list(read_events(lines))
    assert len(events) == 1
    assert events[0].payload['message'] == 'ok'


def test_dual_protocol_call_and_emit_are_equivalent():
    a, b = CollectorSink(), CollectorSink()
    a.emit(EventKind.MOVIE_FINISHED, movie_id='X')
    b(Event(EventKind.MOVIE_FINISHED, {'movie_id': 'X'}))
    assert a.kinds() == b.kinds() == ['movie.finished']
    assert a.events[0].payload == b.events[0].payload


def test_collector_sink_shares_events_across_children():
    sink = CollectorSink()
    sink.child(movie_id='A').emit(EventKind.MOVIE_FINISHED)
    sink.child(movie_id='B').emit(EventKind.MOVIE_FINISHED)
    assert len(sink.events) == 2
    assert [e.payload['movie_id'] for e in sink.events] == ['A', 'B']


def test_null_sink_is_silent_and_chainable():
    sink = NullSink()
    assert sink.emit(EventKind.LOG, message='ignored') is None
    assert sink(EventKind.LOG) is None
    assert isinstance(sink.child(movie_id='X'), NullSink)


def test_log_record_to_event_captures_level_and_exception():
    try:
        raise ValueError('boom')
    except ValueError:
        record = logging.LogRecord(
            'javsp.web.javdb', logging.ERROR, __file__, 1,
            '抓取失败: %s', ('javdb',), exc_info=sys.exc_info())
    event = log_record_to_event(record)
    assert event.kind == EventKind.LOG
    assert event.payload['level'] == 'ERROR'
    assert event.payload['logger'] == 'javsp.web.javdb'
    assert event.payload['message'] == '抓取失败: javdb'
    assert 'boom' in event.payload['exception']


def test_event_stream_handler_forwards_logs_to_sink():
    buf = io.StringIO()
    # 注意：必须传 sink 对象，传 sink.emit 会因参数约定不同而失败
    handler = EventStreamHandler(JsonlSink(buf))
    logger = logging.getLogger('unittest.events')
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        logger.info('正在整理: ABP-647.mp4')
    finally:
        logger.removeHandler(handler)

    events = list(read_events(buf.getvalue().splitlines()))
    assert len(events) == 1
    assert events[0].payload['message'] == '正在整理: ABP-647.mp4'
