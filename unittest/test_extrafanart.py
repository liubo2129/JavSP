"""剧照下载失败不应否决整部影片

真实场景（用户反馈）：站点对失效的剧照地址返回 HTML 错误页且状态码是 200，
``download()`` 正常落盘，``valid_pic()`` 才解码失败。改造前这里会
``check_step(False, ...)`` 抛异常，导致这部影片连 nfo 都写不出来——
元数据其实早已抓齐、封面海报也已下载完成。

剧照不属于 ``crawler.required_keys``，属于"锦上添花"的资源，
因此单张失败必须降级为警告。
"""
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from javsp.config import Cfg
from javsp.datatype import Movie, MovieInfo
from javsp.events import CollectorSink, EventKind


@pytest.fixture(scope='module', autouse=True)
def _init_config():
    Cfg()


def _scraped_movie(tmp_path, preview_pics):
    """构造一部"元数据已抓齐、正要进入 extrafanart 步骤"的影片"""
    folder = tmp_path / '瀬名きらり' / '[ABP-647] 标题'
    folder.mkdir(parents=True)
    video = folder / 'ABP-647.mp4'
    video.write_bytes(b'v')

    info = MovieInfo(from_file=os.path.join(
        os.path.dirname(__file__), 'data', 'ABP-647 (airav).json'))
    info.dvdid = 'ABP-647'
    info.actress = ['瀬名きらり']
    info.preview_pics = list(preview_pics)

    movie = Movie('ABP-647')
    movie.files = [str(video)]
    movie.info = info
    movie.update_only = True      # 原地更新：不移动文件，测试更干净
    return movie, folder


def _run(movie, monkeypatch):
    """跑完整管线，返回 (是否成功, 收集到的事件)

    抓取与封面/海报步骤都被替换成"立刻就成功"：
    这些用例只关心剧照步骤，真去联网会让结果依赖站点可用性，既慢又不稳定。
    """
    import javsp.__main__ as main
    from PIL import Image
    from javsp.datatype import MovieInfo as _MI

    def fake_crawler(movie_, bar=None, sink_=None):
        info = _MI(from_file=os.path.join(
            os.path.dirname(__file__), 'data', 'ABP-647 (airav).json'))
        # preview_pics 必须来自抓取结果：info_summary 会用这里的数据重建
        # movie.info，预设到别处的值会被冲掉。
        info.preview_pics = list(movie_.info.preview_pics or [])
        return {'airav': info}

    def fake_download_cover(covers, fanart_path, big_covers=None):
        # 造一张真实的 jpg，让后面的 valid_pic / 裁剪都能过
        Image.new('RGB', (40, 60), (80, 90, 120)).save(fanart_path, 'JPEG')
        return (covers[0] if covers else 'local://cover', fanart_path)

    def fake_process_poster(movie_):
        Image.new('RGB', (40, 60), (80, 90, 120)).save(movie_.poster_file, 'JPEG')

    monkeypatch.setattr(main, 'parallel_crawler', fake_crawler)
    monkeypatch.setattr(main, 'download_cover', fake_download_cover)
    monkeypatch.setattr(main, 'process_poster', fake_process_poster)

    sink = CollectorSink()
    steps = main._planned_steps([movie])
    ok = main._process_movie(movie, 1, 1, steps, sink, True, main._NoopBar())
    return ok, sink


def test_one_bad_still_does_not_fail_the_movie(tmp_path, monkeypatch):
    """核心回归：剧照全失败时，影片仍然要成功（nfo 必须写出来）"""
    movie, folder = _scraped_movie(tmp_path, ['https://example.invalid/a.jpg',
                                              'https://example.invalid/b.jpg'])
    # 让下载一律抛异常，模拟站点彻底不可达
    monkeypatch.setattr('javsp.__main__.download',
                        lambda *a, **k: (_ for _ in ()).throw(OSError('boom')))

    ok, sink = _run(movie, monkeypatch)

    assert ok is True, '剧照失败不应该让整部影片失败'
    # 关键产物必须存在
    assert os.path.isfile(movie.nfo_file), 'nfo 必须写出来'
    assert not any(e.kind == EventKind.MOVIE_FAILED for e in sink.events)
    assert any(e.kind == EventKind.MOVIE_FINISHED for e in sink.events)


def test_partial_status_is_reported_when_some_stills_fail(tmp_path, monkeypatch):
    """失败要可见，但状态是 partial 而不是 failed"""
    movie, folder = _scraped_movie(tmp_path, ['https://example.invalid/a.jpg'])

    monkeypatch.setattr('javsp.__main__.download',
                        lambda *a, **k: (_ for _ in ()).throw(OSError('boom')))

    ok, sink = _run(movie, monkeypatch)
    assert ok is True

    partial = [e for e in sink.events
               if e.kind == EventKind.MOVIE_STEP and e.payload.get('status') == 'partial']
    assert partial, '应当上报 partial 状态'
    assert partial[0].payload['step'] == 'extrafanart'
    assert partial[0].payload['failed'] == 1
    assert partial[0].payload['total'] == 1


def test_invalid_image_content_is_deleted(tmp_path, monkeypatch):
    """站点返回 HTML 错误页时，坏文件不能留在 extrafanart 里冒充剧照"""
    movie, folder = _scraped_movie(tmp_path, ['https://example.invalid/a.jpg'])

    def fake_download(url, dest, *a, **k):
        # 模拟真实行为：写入了 HTML 错误页，但不抛异常
        with open(dest, 'wb') as f:
            f.write(b'<!DOCTYPE html><html><body>Not Found</body></html>')
        return {'total': 100, 'elapsed': 0.1, 'rate': 1000}

    monkeypatch.setattr('javsp.__main__.download', fake_download)

    ok, sink = _run(movie, monkeypatch)

    assert ok is True
    extrafanart = folder / 'extrafanart'
    leftovers = list(extrafanart.iterdir()) if extrafanart.is_dir() else []
    assert leftovers == [], f'坏文件应被删除，实际残留: {leftovers}'


def test_no_preview_pics_still_marks_step_ok(tmp_path, monkeypatch):
    """没有剧照时该步骤照常记为 ok"""
    movie, folder = _scraped_movie(tmp_path, [])
    ok, sink = _run(movie, monkeypatch)

    assert ok is True
    ok_steps = [e for e in sink.events
                if e.kind == EventKind.MOVIE_STEP
                and e.payload.get('step') == 'extrafanart'
                and e.payload.get('status') == 'ok']
    assert ok_steps
