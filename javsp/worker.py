"""抓取 worker 的子进程入口。

架构说明（P0.5 spike 验证后的结论）：

    GUI 进程（主线程 = webview 事件循环）
      └── 每次运行 spawn 一个 worker 子进程，通过 stdout 的 NDJSON 事件流上报进度

为什么不把管线放在 GUI 进程内的后台线程里：

1. ``Cfg()`` 是 confz 单例，进程启动时从 argv 快照，运行期无法改扫描目录
   （``javsp/config.py:256``）。换目录只能换进程——这也是项目既有
   ``--oscanner.input_directory`` 注入方式的原因。
2. ``parallel_crawler`` 用 ``th.join(timeout)`` 等待抓取线程，超时后线程会
   泄漏并继续修改模块级单例（``web/javdb.py`` 的 request/cookies_pool、
   ``file.py`` 的 failed_items 等）。进程隔离可以随杀随清。
3. 取消操作退化为杀进程组，语义干净（见 P0.5 spike 的 SPIKEKILL 验证）。

进程内约束（必须遵守，否则事件流会被污染）：

- **stdout 只用于事件流**。NDJSON 逐行解析，混入任何别的输出都会产生告警。
- tqdm 与 ``javsp.print`` 重定向后的 ``print`` 都写入 stderr，因此不会污染
  stdout；不要在这里直接 print。
- 父进程须设置 ``JAVSP_BACKGROUND_WORKER=1``，否则 ``__main__`` 的
  ``_should_run_in_background()`` 会再派生一层子进程后立即退出。
"""
from __future__ import annotations

import logging
import os
import re
import sys
from typing import Any, Dict, List

# 该环境变量同时承担两个作用：
#   1. 阻止 __main__ 的 _should_run_in_background() 再派生一层子进程
#   2. 让本入口能识别自己是被父进程拉起的 worker
WORKER_ENV_FLAG = 'JAVSP_BACKGROUND_WORKER'

logger = logging.getLogger('worker')

# 供 __main__ 判断"是否应跳过写日志文件"的标记。必须在 import javsp.__main__
# 之前设置，因为它是在模块导入期读取该变量的。
WORKER_MODE_FLAG = 'JAVSP_WORKER_MODE'

# 置为 '1' 时只扫描不上报抓取（GUI 的扫描预览步骤）
SCAN_ONLY_ENV_FLAG = 'JAVSP_SCAN_ONLY'

# 置为 '1' 时进入"按番号获取"模式：不扫描目录，直接按给定番号抓取，
# 结果按 <目标文件夹>/<番号>/ 组织
BY_ID_ENV_FLAG = 'JAVSP_BY_ID'
# 按番号获取的目标文件夹
BY_ID_DIR_ENV = 'JAVSP_BY_ID_DIR'
# 待获取的番号，多个用空白、逗号或分号分隔
BY_ID_IDS_ENV = 'JAVSP_BY_ID_IDS'

# 输入番号时可能用到的分隔符（含中文标点）
_ID_SEPARATORS = re.compile(r'[\s,，;；、|/]+')


def parse_movie_ids(text: str) -> List[str]:
    """把用户输入的一串番号解析成列表（去重、保序）"""
    seen = set()
    ids = []
    for raw in _ID_SEPARATORS.split(text or ''):
        item = raw.strip()
        if not item:
            continue
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        ids.append(item)
    return ids


def _build_by_id_movies(ids: List[str]) -> List[Any]:
    """按番号构造 Movie 列表

    数据源类型沿用项目既有的推断逻辑；推断不出时按普通番号处理
    （``parallel_crawler`` 在 cid 且带 dvdid 时会自动再试普通模式）。
    """
    from javsp.avid import guess_av_type
    from javsp.datatype import Movie

    movies = []
    for avid in ids:
        try:
            data_src = guess_av_type(avid)
        except Exception:  # noqa: BLE001 - 推断失败按普通番号处理
            data_src = 'normal'
        if data_src == 'cid':
            movie = Movie(cid=avid)
        else:
            movie = Movie(avid)
        movie.data_src = data_src
        movie.files = []
        movie.by_id_mode = True
        movies.append(movie)
    return movies


def _run_by_id(sink, ids: List[str], folder: str) -> int:
    """按番号获取元数据，输出到 <folder>/<番号>/"""
    from javsp.events import EventKind
    from javsp.__main__ import RunNormalMode, import_crawlers, movie_id_of

    _apply_user_settings(sink)
    import_crawlers()

    movies = _build_by_id_movies(ids)
    for movie in movies:
        movie.by_id_folder = folder
    # movie_ids 用干净的番号（不是 str(movie)），前端会直接拿它当列表项显示
    sink(EventKind.SCAN_SKIPPED, reason='by_id', root=folder, folder=folder,
         movie_count=len(movies), movie_ids=[movie_id_of(m) for m in movies])
    sink(EventKind.RUN_STARTED, mode='by_id', movie_count=len(movies), root=folder)
    finished = RunNormalMode(movies, sink=sink)
    sink(EventKind.RUN_FINISHED, mode='by_id',
         movie_count=len(movies), finished_count=len(finished),
         finished=[movie_id_of(m) for m in finished])
    return 0 if len(finished) == len(movies) else 1


def _mark_worker_mode() -> None:
    """本进程是父进程拉起的 worker：跳过 Finder 文件日志，事件流即日志"""
    os.environ[WORKER_MODE_FLAG] = '1'
    # 同时阻止 __main__ 再派生一层后台进程
    os.environ.setdefault(WORKER_ENV_FLAG, '1')


def _force_utf8_streams() -> None:
    """保证事件流的编码不受宿主环境影响（Windows 上尤其重要）"""
    for name in ('stdout', 'stderr'):
        stream = getattr(sys, name)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding='utf-8')
        except (AttributeError, ValueError, OSError):
            pass


def _install_event_logging(sink) -> None:
    """让既有 logger 调用变成事件，而不是终端输出

    ``javsp.print`` 会把内置 print 重定向到 tqdm（即 stderr），
    ``__main__`` 又把 root 的 StreamHandler 换成了 TqdmOut。
    这里把 root logger 重置为"只发事件"，避免 worker 往 stderr 刷进度条，
    也顺带保证 stdout 只剩事件流。
    """
    from javsp.events import EventStreamHandler

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = EventStreamHandler(sink, level=logging.INFO)
    handler.setFormatter(logging.Formatter('%(name)s: %(message)s'))
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def _current_paths(movie) -> List[str]:
    """影片文件**当前**所在位置（绝对路径）

    整理时会调用 ``movie.rename_files()`` 把文件移到
    ``<输出目录>/<女优>/[番号] 标题/``（默认在 ``#整理完成`` 下）。
    该方法把新路径记在 ``movie.new_paths``，**不会**更新 ``movie.files``，
    所以移动后 ``movie.files`` 指向的是已经不存在的旧路径——
    界面拿它去查详情只能得到"文件不存在"。
    """
    paths = getattr(movie, 'new_paths', None) or movie.files
    return [_abs(p) for p in paths]


def _nfo_for_movie(movie) -> str | None:
    """影片整理后的 nfo 路径（绝对路径）

    移动后 nfo 与影片文件同目录，所以优先按新路径找，找不到再退回旧路径。
    """
    for candidate in _current_paths(movie) + [_abs(f) for f in movie.files]:
        found = _find_nfo_for([candidate])
        if found:
            return found
    nfo = getattr(movie, 'nfo_file', None)
    if nfo:
        absolute = _abs(nfo)
        if os.path.isfile(absolute):
            return absolute
    return None


def _movie_summary(movie) -> Dict[str, Any]:
    """把 Movie 转成前端可直接渲染的结构

    事件流是 JSON，不能传对象；同时前端需要番号/数据源/文件明细来做预览，
    只给 ``str(movie)`` 那种 ``Movie('ABP-647')`` 字符串是不够的。
    """
    from javsp.__main__ import movie_id_of
    paths = _current_paths(movie)
    return {
        'id': movie_id_of(movie),
        'data_src': movie.data_src,
        'files': [os.path.basename(p) for p in paths],
        'paths': paths,
        'file_count': len(paths),
        'scraped': _find_nfo_for(paths) is not None,
    }


def _find_nfo_for(files) -> str | None:
    """为影片文件寻找对应的 nfo

    遵循 Kodi/Emby 的两种惯例：同目录下 ``<basename>.nfo`` 或 ``movie.nfo``。
    有 nfo 即认为这部影片已被整理过，界面据此区分显示。
    """
    if not files:
        return None
    first = _abs(files[0])
    base = os.path.splitext(first)[0]
    for candidate in (base + '.nfo', os.path.join(os.path.dirname(first), 'movie.nfo')):
        if os.path.isfile(candidate):
            return candidate
    return None


def _abs(path: str) -> str:
    """把可能相对的路径转成绝对路径

    GUI 的 worker 以扫描目录为 cwd 启动，返回绝对路径方便前端与 API 直接使用。
    """
    return os.path.abspath(path)


def _apply_user_settings(sink) -> None:
    """把设置界面里的抓取器开关应用到 Cfg()

    必须在 ``import_crawlers()`` 之前调用：那个函数按 ``crawler.selection``
    导入模块，设置晚了只会影响后续的数据汇总，被禁用的站点仍会被抓取。

    Cfg() 是 confz 单例（进程启动即从 argv 快照），运行期无法重新加载配置，
    因此这里直接就地改写 ``cfg.crawler.selection`` 的各组属性。
    """
    from javsp.config import Cfg
    from javsp.events import EventKind
    from javsp.settings import load_crawler_selection, merge_into_config

    saved = load_crawler_selection()
    if not saved:
        # 用户没动过设置，完整走 config.yml 的出厂配置
        return
    merge_into_config(Cfg().crawler.selection, saved)
    logger.info('已应用用户设置的抓取器开关: %s',
                ', '.join(f'{g}={len(v)}' for g, v in saved.items()))
    sink(EventKind.SETTINGS_APPLIED, crawler_selection=saved)


def _run(root: str, sink) -> int:
    """扫描并整理，全程向 sink 上报事件"""
    from javsp.events import EventKind
    from javsp.config import Cfg
    from javsp.file import scan_movies
    from javsp.__main__ import RunNormalMode, import_crawlers

    _apply_user_settings(sink)

    # 必须导入抓取器：parallel_crawler 是通过 sys.modules['javsp.web.<name>']
    # 取 parser 的，这些模块在配置里只是字符串，不 import 就不在 sys.modules 中。
    # CLI 路径在 entry() 里调用它，worker 路径必须自己调用。
    import_crawlers()

    sink(EventKind.SCAN_STARTED, root=root)
    recognized = scan_movies(root, sink=sink)
    summaries = [_movie_summary(m) for m in recognized]
    sink(EventKind.SCAN_FINISHED, movie_count=len(recognized),
         movies=summaries,
         # 兼容旧的字符串形式（其它消费方可能仍按字符串解析）
         movie_ids=[s['id'] for s in summaries])

    # 仅扫描模式：GUI 的"预览"步骤用，只统计识别到哪些影片，不抓取也不改文件。
    # 用环境变量而非命令行参数，是为了避免与 confz 的 CLArgSource(prefix='o')
    # 以及 __main__ 的 parse_known_args 互相干扰。
    if os.environ.get(SCAN_ONLY_ENV_FLAG) == '1':
        sink(EventKind.RUN_FINISHED, mode='scan_only',
             movie_count=len(recognized), finished_count=0,
             finished=[])
        return 0

    if not recognized:
        sink(EventKind.RUN_FAILED, error='no_movie',
             message='未找到影片文件')
        return 1

    sink(EventKind.RUN_STARTED, movie_count=len(recognized), root=root)
    finished = RunNormalMode(recognized, sink=sink)
    sink(EventKind.RUN_FINISHED,
         movie_count=len(recognized),
         finished_count=len(finished),
         finished=[str(m) for m in finished])
    # 有影片失败时返回非零，父进程据此判断整体是否成功
    return 0 if len(finished) == len(recognized) else 1


def main(argv=None) -> int:
    # 必须最先执行：javsp.__main__ 在导入期就会读取这些标记来决定
    # 是否写日志文件、是否再派生后台进程。
    _mark_worker_mode()
    _force_utf8_streams()

    from javsp.events import JsonlSink
    from javsp.config import Cfg

    # 事件流固定写到真正的 stdout。项目里 colorama/pretty_errors 等会把
    # sys.stdout 替换成包装对象，而 __main__ 在无终端时还可能把它指向 devnull，
    # 因此这里保存的原始流才可靠。
    sink = JsonlSink(sys.__stdout__)
    _install_event_logging(sink)

    try:
        Cfg()
    except Exception as e:  # noqa: BLE001 - 配置失败必须上报给父进程
        from javsp.events import EventKind
        sink(EventKind.RUN_FAILED, error='config_invalid', message=str(e))
        return 2

    # 按番号获取模式：不需要扫描目录，直接按给定番号抓取。
    # 目标文件夹允许不存在（会自动创建），因此这里优先于扫描目录校验。
    if os.environ.get(BY_ID_ENV_FLAG) == '1':
        from javsp.events import EventKind
        ids = parse_movie_ids(os.environ.get(BY_ID_IDS_ENV, ''))
        folder = (os.environ.get(BY_ID_DIR_ENV) or '').strip()
        if not ids:
            sink(EventKind.RUN_FAILED, error='no_ids',
                 message='未提供任何番号')
            return 2
        if not folder:
            sink(EventKind.RUN_FAILED, error='no_target_dir',
                 message='未指定目标文件夹')
            return 2
        folder = os.path.abspath(os.path.expanduser(folder))
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as e:
            sink(EventKind.RUN_FAILED, error='target_dir_unusable',
                 message=f'无法创建目标文件夹 {folder}: {e}')
            return 2
        try:
            return _run_by_id(sink, ids, folder)
        except KeyboardInterrupt:
            return 130
        except Exception as e:  # noqa: BLE001
            logging.getLogger('worker').exception('按番号获取失败')
            sink(EventKind.RUN_FAILED, error=type(e).__name__, message=str(e))
            return 1

    # get_scan_dir 会在配置为空时弹出目录选择框；worker 模式只接受父进程
    # 通过 --oscanner.input_directory 注入的路径，因此这里直接读取配置值。
    configured = Cfg().scanner.input_directory
    if not configured or not configured.exists():
        from javsp.events import EventKind
        sink(EventKind.RUN_FAILED, error='no_scan_dir',
             message=f'扫描目录无效: {configured}')
        return 2
    root = str(configured)

    try:
        code = _run(root, sink)
    except KeyboardInterrupt:
        code = 130
    except Exception as e:  # noqa: BLE001 - 任何未捕获异常都要变成事件
        from javsp.events import EventKind
        logging.getLogger('worker').exception('worker 运行失败')
        sink(EventKind.RUN_FAILED, error=type(e).__name__, message=str(e))
        code = 1
    return code


def entry() -> None:
    """console_scripts 入口（对应 pyproject.toml 的 worker 脚本）"""
    _mark_worker_mode()
    sys.exit(main())


if __name__ == '__main__':
    entry()
