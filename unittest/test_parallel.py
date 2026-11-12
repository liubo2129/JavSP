"""影片级并行整理的单元测试

``crawler.max_workers`` 控制的是**影片之间**的并发（同一部影片的多个站点
一直是并行的）。这条路径有几个容易回归的点，这里逐个固定住：

  1. 默认值必须是 1 —— 老配置没有这个键时行为不能变
  2. 并行时 ``index`` 仍是"输入顺序里的第几部"，不是完成顺序
  3. 返回值按输入顺序排列，且失败的影片不出现在结果里
  4. 事件流必须恰好包含每部影片的一条 started 和一条 finished/failed
"""
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from javsp.config import Cfg
from javsp.datatype import Movie
from javsp.events import CollectorSink, EventKind


@pytest.fixture(scope='module', autouse=True)
def _init_config():
    Cfg()


@pytest.fixture
def set_workers():
    """按需修改并发数（Cfg 是 frozen 的，走 model_copy 替换）

    测试结束后必须还原：``Cfg()`` 是进程内单例，改了就影响后续所有用例。
    """
    cfg = Cfg()
    original = cfg.crawler

    def _apply(value):
        object.__setattr__(
            cfg, 'crawler', original.model_copy(update={'max_workers': value}))

    try:
        yield _apply
    finally:
        object.__setattr__(cfg, 'crawler', original)


def _movies(count: int):
    movies = []
    for i in range(count):
        movie = Movie(f'ABP-{i:03d}')
        movie.files = [f'/tmp/javsp-parallel/ABP-{i:03d}.mp4']
        movies.append(movie)
    return movies


def test_max_workers_defaults_to_serial():
    """出厂默认必须是 1：改造前的行为是逐部串行，升级后不能自动变快/变凶"""
    from javsp.__main__ import _effective_workers

    assert Cfg().crawler.max_workers == 1
    assert _effective_workers(5, event_mode=True) == 1


def test_effective_workers_clamps_to_movie_count(set_workers):
    from javsp.__main__ import _effective_workers

    set_workers(8)
    # 并发数不应超过待整理影片数
    assert _effective_workers(3, event_mode=True) == 3
    assert _effective_workers(1, event_mode=True) == 1


def test_effective_workers_falls_back_to_serial_in_cli_mode(set_workers):
    """终端模式（无 sink）不并行：多个 tqdm 进度条会互相覆盖"""
    from javsp.__main__ import _effective_workers

    set_workers(4)
    assert _effective_workers(10, event_mode=False) == 1


def test_serial_path_runs_one_movie_at_a_time(set_workers, monkeypatch):
    """max_workers=1 时任意时刻只能有一部影片在处理"""
    import javsp.__main__ as main

    active, peak, lock = 0, 0, threading.Lock()

    def fake_process(movie, index, total, steps, sink, event_mode, inner_bar):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.01)
        with lock:
            active -= 1
        sink(EventKind.MOVIE_FINISHED, movie_id=movie.dvdid, index=index, total=total)
        return True

    monkeypatch.setattr(main, '_process_movie', fake_process)
    set_workers(1)

    movies = _movies(4)
    sink = CollectorSink()
    finished = main.RunNormalMode(movies, sink=sink)

    assert peak == 1
    assert len(finished) == 4


def test_parallel_path_actually_overlaps(set_workers, monkeypatch):
    """max_workers>1 时必须真的同时跑多部，而不是排队"""
    import javsp.__main__ as main

    active, peak, lock = 0, 0, threading.Lock()
    barrier = threading.Barrier(2, timeout=5)

    def fake_process(movie, index, total, steps, sink, event_mode, inner_bar):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            # 两部影片都进入函数后才会一起往下走；串行执行会在此超时
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        with lock:
            active -= 1
        sink(EventKind.MOVIE_FINISHED, movie_id=movie.dvdid, index=index, total=total)
        return True

    monkeypatch.setattr(main, '_process_movie', fake_process)
    set_workers(2)

    movies = _movies(4)
    main.RunNormalMode(movies, sink=CollectorSink())
    assert peak >= 2


def test_parallel_results_keep_input_order(set_workers, monkeypatch):
    """返回值要按输入顺序，不能是完成顺序（否则上层比对 finished 会错）"""
    import javsp.__main__ as main

    def fake_process(movie, index, total, steps, sink, event_mode, inner_bar):
        # 故意让靠前的影片更慢，制造"完成顺序与输入顺序相反"
        time.sleep(0.05 * (total - index + 1))
        sink(EventKind.MOVIE_FINISHED, movie_id=movie.dvdid, index=index, total=total)
        return True

    monkeypatch.setattr(main, '_process_movie', fake_process)
    set_workers(3)

    movies = _movies(4)
    finished = main.RunNormalMode(movies, sink=CollectorSink())
    assert [m.dvdid for m in finished] == [m.dvdid for m in movies]


def test_parallel_events_cover_each_movie_once(set_workers, monkeypatch):
    """并行不能丢事件或重复上报：每部影片恰好一条 started + 一条 finished/failed"""
    import javsp.__main__ as main

    def fake_process(movie, index, total, steps, sink, event_mode, inner_bar):
        sink(EventKind.MOVIE_STARTED, movie_id=movie.dvdid, index=index, total=total)
        # 第 3 部失败，验证失败路径也具备同样的完整性
        if movie.dvdid.endswith('002'):
            sink(EventKind.MOVIE_FAILED, movie_id=movie.dvdid, index=index, total=total,
                 step='crawl', error='TestError', message='boom')
            return False
        sink(EventKind.MOVIE_FINISHED, movie_id=movie.dvdid, index=index, total=total)
        return True

    monkeypatch.setattr(main, '_process_movie', fake_process)
    set_workers(3)

    movies = _movies(5)
    sink = CollectorSink()
    finished = main.RunNormalMode(movies, sink=sink)

    started = [e.payload['movie_id'] for e in sink.events
               if e.kind == EventKind.MOVIE_STARTED]
    ended = [e.payload['movie_id'] for e in sink.events
             if e.kind in (EventKind.MOVIE_FINISHED, EventKind.MOVIE_FAILED)]

    assert sorted(started) == sorted(m.dvdid for m in movies)
    assert sorted(ended) == sorted(m.dvdid for m in movies)
    assert len(finished) == 4          # 失败的那部不计入
    assert 'ABP-002' not in [m.dvdid for m in finished]


def test_empty_movie_list_returns_empty():
    from javsp.__main__ import RunNormalMode

    assert RunNormalMode([], sink=CollectorSink()) == []


def test_rate_gate_spaces_out_movie_starts():
    """并行时的限流：相邻两部影片的"开始整理"必须至少间隔 interval 秒

    否则 max_workers 一调大就会同时向所有站点打请求，比串行更容易被限流。
    """
    from javsp.__main__ import _RateGate

    gate = _RateGate(0.05)
    starts = []
    lock = threading.Lock()

    def worker():
        gate.wait()
        with lock:
            starts.append(time.monotonic())

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    starts.sort()
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    # 允许少量调度抖动，但不能出现两部影片同时开始
    assert all(g >= 0.04 for g in gaps), gaps


def test_rate_gate_with_zero_interval_does_not_block():
    from javsp.__main__ import _RateGate

    start = time.monotonic()
    gate = _RateGate(0)
    for _ in range(5):
        gate.wait()
    assert time.monotonic() - start < 0.1


def test_rename_files_tolerates_concurrent_dir_removal(tmp_path, monkeypatch):
    """rmdir 竞态：另一部影片同时清空并删掉了源目录时不该抛异常

    并行整理时同一源目录可能被多部影片共享，``os.listdir`` 与 ``os.rmdir``
    之间目录已消失，改造前会抛 OSError 并把已成功的影片误报为失败。
    """
    src_dir = tmp_path / 'src'
    src_dir.mkdir()
    video = src_dir / 'ABP-001.mp4'
    video.write_bytes(b'not-a-real-video')

    movie = Movie('ABP-001')
    movie.files = [str(video)]
    movie.save_dir = str(tmp_path / 'dst')
    movie.basename = 'ABP-001'
    os.makedirs(movie.save_dir, exist_ok=True)

    # 模拟"另一个线程已经把目录删掉"：listdir 看到空目录后目录立刻消失，
    # 随后的 rmdir 必然失败
    import javsp.datatype as datatype
    real_listdir = os.listdir

    def listdir_then_removed(path):
        result = real_listdir(path)
        try:
            os.rmdir(path)
        except OSError:
            pass
        return result

    monkeypatch.setattr(datatype.os, 'listdir', listdir_then_removed)

    movie.rename_files()      # 不抛异常即通过
    assert movie.new_paths
    assert os.path.isfile(movie.new_paths[0])
