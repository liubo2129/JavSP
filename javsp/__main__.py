import os
import re
import sys

# 冻结后的单文件可执行程序需要承担三个角色（cx_Freeze 只产出这一个二进制）：
#   --gui           桌面窗口
#   无参数（Finder 双击）  也进入桌面窗口
#   --javsp-worker  GUI 拉起的抓取子进程
#   带其它参数      命令行刮削
# 派发必须在**其它 import 之前**：worker 与 gui 模块有自己的导入需求，
# 而本模块的导入带有全局副作用（替换 stdout、挂日志 handler 等）。
#
# 实际生效的调用点放在模块末尾（见 _ROLE_DISPATCH），因为 worker 需要
# `from javsp.__main__ import RunNormalMode`，此刻本模块还在初始化，
# 顶层导入会构成循环导入。
if getattr(sys, 'frozen', False):
    _want_gui = '--gui' in sys.argv[1:] or len(sys.argv) == 1
    _want_worker = '--javsp-worker' in sys.argv[1:]
    if _want_worker:
        # multiprocessing spawn helper 的兜底守卫，优先于角色派发
        if (len(sys.argv) >= 3 and sys.argv[-2] == '-c'
                and sys.argv[-1].startswith('from multiprocessing.')):
            exec(sys.argv[-1])
            sys.exit(0)

    def _dispatch_role() -> None:
        if _want_worker:
            # 延迟导入：本模块已执行完毕，worker 里的
            # `from javsp.__main__ import RunNormalMode` 不再冲突
            from javsp.worker import main as _worker_main
            sys.exit(_worker_main())
        if _want_gui:
            from javsp.gui import entry as _gui_entry
            _gui_entry()

    _ROLE_DISPATCH = _dispatch_role
else:
    _ROLE_DISPATCH = None

# cx_Freeze 冻结后，multiprocessing 的 spawn helper（如 resource_tracker）
# 会用当前可执行文件加 `-c` 重新启动自身；此时直接执行 helper 并退出，
# 避免被 argparse 的 `-c/--config` 参数误解析。
if (getattr(sys, 'frozen', False)
        and len(sys.argv) >= 3
        and sys.argv[-2] == '-c'
        and sys.argv[-1].startswith('from multiprocessing.')):
    exec(sys.argv[-1])
    sys.exit(0)

import json
import time
import logging
import subprocess
import tempfile
from pathlib import Path


def _prepare_standard_streams():
    """Finder 启动时可能没有 stdout/stderr，补成 devnull 避免 print/log 崩溃"""
    for name in ('stdout', 'stderr'):
        stream = getattr(sys, name)
        if stream is None:
            setattr(sys, name, open(os.devnull, 'w', encoding='utf-8'))
        else:
            try:
                stream.reconfigure(encoding='utf-8')
            except (AttributeError, ValueError, OSError):
                pass


def _configure_finder_logging():
    """非终端环境下写日志文件，方便从 Finder 启动时排查问题

    worker 模式（由 GUI 拉起的子进程）必须跳过：它的日志已经通过事件流
    上报给父进程，再写一份文件既是重复，也会让 GUI 与 worker 争抢同一个
    日志文件（P0.5 spike 实测：两个进程写同一文件会互相截断）。
    """
    if os.environ.get('JAVSP_WORKER_MODE') == '1':
        return None
    if sys.platform != 'darwin':
        return None
    try:
        has_tty = sys.stderr is not None and sys.stderr.isatty()
    except (AttributeError, ValueError):
        has_tty = False
    if has_tty:
        return None

    root_logger = logging.getLogger()
    for log_dir in (Path.home() / 'Library' / 'Logs' / 'JavSP',
                    Path(tempfile.gettempdir()) / 'JavSP'):
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            log_file = log_dir / 'JavSP.log'
            handler = logging.FileHandler(log_file, encoding='utf-8')
        except OSError:
            continue
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter(
            '%(asctime)s [%(levelname)s] %(name)s: %(message)s'))
        root_logger.addHandler(handler)
        root_logger.setLevel(logging.INFO)
        return str(log_file)
    return None


_prepare_standard_streams()
_FINDER_LOG_FILE = _configure_finder_logging()

from PIL import Image
from pydantic import ValidationError
from pydantic_extra_types.pendulum_dt import Duration
import requests
import threading
from typing import Dict, List

import colorama
import pretty_errors
from colorama import Fore, Style
from tqdm import tqdm


pretty_errors.configure(display_link=True)


from javsp.print import TqdmOut
from javsp.cropper import Cropper, get_cropper
from javsp.events import EventKind, NullSink


# 将StreamHandler的stream修改为TqdmOut，以与Tqdm协同工作
root_logger = logging.getLogger()
for handler in root_logger.handlers:
    if type(handler) == logging.StreamHandler:
        handler.stream = TqdmOut

logger = logging.getLogger('main')
if _FINDER_LOG_FILE:
    logger.info('未检测到终端，日志将写入: %s', _FINDER_LOG_FILE)


from javsp.lib import resource_path
from javsp.nfo import write_nfo
from javsp.file import *
from javsp.func import *
from javsp.image import *
from javsp.datatype import Movie, MovieInfo
from javsp.web.base import download
from javsp.web.exceptions import *
from javsp.web.translate import translate_movie_info

from javsp.config import Cfg, CrawlerID
from javsp.prompt import prompt
from javsp.ui import (
    show_error_and_exit,
    show_macos_alert,
    show_macos_notification,
    stderr_is_tty,
    stdin_is_tty,
)

actressAliasMap = {}

def resolve_alias(name):
    """将别名解析为固定的名字"""
    for fixedName, aliases in actressAliasMap.items():
        if name in aliases:
            return fixedName
    return name  # 如果找不到别名对应的固定名字，则返回原名


def import_crawlers():
    """按配置文件的抓取器顺序将该字段转换为抓取器的函数列表"""
    unknown_mods = []
    for _, mods in Cfg().crawler.selection.items():
        valid_mods = []
        for name in mods:
            try:
                # 导入fc2fan抓取器的前提: 配置了fc2fan的本地路径
                # if name == 'fc2fan' and (not os.path.isdir(Cfg().Crawler.fc2fan_local_path)):
                #     logger.debug('由于未配置有效的fc2fan路径，已跳过该抓取器')
                #     continue
                import_name = 'javsp.web.' + name
                __import__(import_name)
                valid_mods.append(import_name)  # 抓取器有效: 使用完整模块路径，便于程序实际使用
            except ModuleNotFoundError:
                unknown_mods.append(name)       # 抓取器无效: 仅使用模块名，便于显示
    if unknown_mods:
        logger.warning('配置的抓取器无效: ' + ', '.join(unknown_mods))


def movie_id_of(movie) -> str:
    """影片在事件流里的标识

    不能用 ``str(movie)``：那样得到的是 ``Movie('ABP-647')``，
    前端会把它原样当成番号显示。优先取真正的番号。
    """
    return movie.dvdid or movie.cid or '未知'


# 爬虫是IO密集型任务，可以通过多线程提升效率
def parallel_crawler(movie: Movie, tqdm_bar=None, sink=None):
    """使用多线程抓取不同网站的数据

    Args:
        movie: 待抓取的影片
        tqdm_bar: 终端进度条（CLI 模式），可为 None
        sink: 事件接收方（GUI/worker 模式），可为 None。为 None 时不产生事件，
              既有 CLI 行为完全不变。
    """
    sink = sink if sink is not None else NullSink()

    def wrapper(parser, info: MovieInfo, retry):
        """对抓取器函数进行包装，便于更新提示信息和自动重试"""
        crawler_name = threading.current_thread().name
        task_info = f'Crawler: {crawler_name}: {info.dvdid}'
        # 线程名形如 'javsp.web.javdb'，事件里用短名更便于前端展示
        short_name = crawler_name.split('.')[-1]
        sink(EventKind.CRAWLER_STARTED, crawler=short_name, movie_id=movie_id_of(movie))
        for cnt in range(retry):
            try:
                parser(info)
                movie_id = info.dvdid or info.cid
                logger.debug(f"{crawler_name}: 抓取成功: '{movie_id}': '{info.url}'")
                # 抓取器可能"成功返回但没拿到数据"，站点实际未命中，需区分上报
                found = bool(info.title or info.url)
                if found:
                    setattr(info, 'success', True)
                    sink(EventKind.CRAWLER_SUCCEEDED, crawler=short_name,
                         movie_id=str(movie_id) if movie_id else None,
                         title=info.title)
                else:
                    sink(EventKind.CRAWLER_FAILED, crawler=short_name,
                         error='no_data', message='站点未返回有效数据')
                if isinstance(tqdm_bar, tqdm):
                    tqdm_bar.set_description(f'{crawler_name}: 抓取完成')
                break
            except MovieNotFoundError as e:
                logger.debug(e)
                sink(EventKind.CRAWLER_FAILED, crawler=short_name,
                     error='not_found', message=str(e))
                break
            except MovieDuplicateError as e:
                logger.exception(e)
                sink(EventKind.CRAWLER_FAILED, crawler=short_name,
                     error='duplicate', message=str(e))
                break
            except (SiteBlocked, SitePermissionError, CredentialError) as e:
                logger.error(e)
                sink(EventKind.CRAWLER_FAILED, crawler=short_name,
                     error='blocked', message=str(e))
                break
            except requests.exceptions.RequestException as e:
                logger.debug(f'{crawler_name}: 网络错误，正在重试 ({cnt+1}/{retry}): \n{repr(e)}')
                sink(EventKind.CRAWLER_RETRY, crawler=short_name,
                     attempt=cnt + 1, total=retry, error=type(e).__name__)
                if isinstance(tqdm_bar, tqdm):
                    tqdm_bar.set_description(f'{crawler_name}: 网络错误，正在重试')
                if cnt + 1 >= retry:
                    # 重试次数用尽，此处若不上报失败，前端会一直显示"进行中"
                    sink(EventKind.CRAWLER_FAILED, crawler=short_name,
                         error=type(e).__name__, message=str(e))
            except Exception as e:
                logger.exception(e)
                sink(EventKind.CRAWLER_FAILED, crawler=short_name,
                     error=type(e).__name__, message=str(e))
                break

    # 根据影片的数据源获取对应的抓取器
    crawler_mods: List[CrawlerID] = Cfg().crawler.selection[movie.data_src]

    all_info = {i.value: MovieInfo(movie) for i in crawler_mods}
    # 番号为cid但同时也有有效的dvdid时，也尝试使用普通模式进行抓取
    if movie.data_src == 'cid' and movie.dvdid:
        crawler_mods = crawler_mods + Cfg().crawler.selection.normal
        for i in all_info.values():
            i.dvdid = None
        for i in Cfg().crawler.selection.normal:
            all_info[i.value] = MovieInfo(movie.dvdid)
    thread_pool = []
    for mod_partial, info in all_info.items():
        mod = f"javsp.web.{mod_partial}"
        parser = getattr(sys.modules[mod], 'parse_data')
        # 将all_info中的info实例传递给parser，parser抓取完成后，info实例的值已经完成更新
        # TODO: 抓取器如果带有parse_data_raw，说明它已经自行进行了重试处理，此时将重试次数设置为1
        if hasattr(sys.modules[mod], 'parse_data_raw'):
            th = threading.Thread(target=wrapper, name=mod, args=(parser, info, 1))
        else:
            th = threading.Thread(target=wrapper, name=mod, args=(parser, info, Cfg().network.retry))
        th.start()
        thread_pool.append(th)
    # 等待所有线程结束
    timeout = Cfg().network.retry * Cfg().network.timeout.total_seconds()
    for th in thread_pool:
        th: threading.Thread
        th.join(timeout=timeout)
    # 根据抓取结果更新影片类型判定
    if movie.data_src == 'cid' and movie.dvdid:
        titles = [all_info[i].title for i in Cfg().crawler.selection[movie.data_src]]
        if any(titles):
            movie.dvdid = None
            all_info = {k: v for k, v in all_info.items() if k in Cfg().crawler.selection['cid']}
        else:
            logger.debug(f'自动更正影片数据源类型: {movie.dvdid} ({movie.cid}): normal')
            movie.data_src = 'normal'
            movie.cid = None
            all_info = {k: v for k, v in all_info.items() if k not in Cfg().crawler.selection['cid']}
    # 删除抓取失败的站点对应的数据
    all_info = {k:v for k,v in all_info.items() if hasattr(v, 'success')}
    for info in all_info.values():
        del info.success
    # 删除all_info中键名中的'web.'
    all_info = {k[4:]:v for k,v in all_info.items()}
    return all_info


def info_summary(movie: Movie, all_info: Dict[str, MovieInfo]):
    """汇总多个来源的在线数据生成最终数据"""
    final_info = MovieInfo(movie)
    ########## 部分字段配置了专门的选取逻辑，先处理这些字段 ##########
    # genre
    if 'javdb' in all_info and all_info['javdb'].genre:
        final_info.genre = all_info['javdb'].genre

    ########## 移除所有抓取器数据中，标题尾部的女优名 ##########
    if Cfg().summarizer.title.remove_trailing_actor_name:
        for name, data in all_info.items():
            data.title = remove_trail_actor_in_title(data.title, data.actress)
    ########## 然后检查所有字段，如果某个字段还是默认值，则按照优先级选取数据 ##########
    # parser直接更新了all_info中的项目，而初始all_info是按照优先级生成的，已经符合配置的优先级顺序了
    # 按照优先级取出各个爬虫获取到的信息
    attrs = [i for i in dir(final_info) if not i.startswith('_')]
    covers, big_covers = [], []
    for name, data in all_info.items():
        absorbed = []
        # 遍历所有属性，如果某一属性当前值为空而爬取的数据中含有该属性，则采用爬虫的属性
        for attr in attrs:
            incoming = getattr(data, attr)
            current = getattr(final_info, attr)
            if attr == 'cover':
                if incoming and (incoming not in covers):
                    covers.append(incoming)
                    absorbed.append(attr)
            elif attr == 'big_cover':
                if incoming and (incoming not in big_covers):
                    big_covers.append(incoming)
                    absorbed.append(attr)
            elif attr == 'uncensored':
                if (current is None) and (incoming is not None):
                    setattr(final_info, attr, incoming)
                    absorbed.append(attr)
            else:
                if (not current) and (incoming):
                    setattr(final_info, attr, incoming)
                    absorbed.append(attr)
        if absorbed:
            logger.debug(f"从'{name}'中获取了字段: " + ' '.join(absorbed))
    # 使用网站的番号作为番号
    if Cfg().crawler.respect_site_avid:
        id_weight = {}
        for name, data in all_info.items():
            if data.title:
                if movie.dvdid:
                    id_weight.setdefault(data.dvdid, []).append(name)
                else:
                    id_weight.setdefault(data.cid, []).append(name)
        # 根据权重选择最终番号
        if id_weight:
            id_weight = {k:v for k, v in sorted(id_weight.items(), key=lambda x:len(x[1]), reverse=True)}
            final_id = list(id_weight.keys())[0]
            if movie.dvdid:
                final_info.dvdid = final_id
            else:
                final_info.cid = final_id
    # javdb封面有水印，优先采用其他站点的封面
    javdb_cover = getattr(all_info.get('javdb'), 'cover', None)
    if javdb_cover is not None:
        match Cfg().crawler.use_javdb_cover:
            case UseJavDBCover.fallback:
                covers.remove(javdb_cover)
                covers.append(javdb_cover)
            case UseJavDBCover.no:
                covers.remove(javdb_cover)

    setattr(final_info, 'covers', covers)
    setattr(final_info, 'big_covers', big_covers)
    # 对cover和big_cover赋值，避免后续检查必须字段时出错
    if covers:
        final_info.cover = covers[0]
    if big_covers:
        final_info.big_cover = big_covers[0]
    ########## 部分字段放在最后进行检查 ##########
    # 特殊的 genre
    if final_info.genre is None:
        final_info.genre = []
    if movie.hard_sub:
        final_info.genre.append('内嵌字幕')
    if movie.uncensored:
        final_info.genre.append('无码流出/破解')

    # 女优别名固定
    if Cfg().crawler.normalize_actress_name and bool(final_info.actress_pics):
        final_info.actress = [resolve_alias(i) for i in final_info.actress]
        if final_info.actress_pics:
            final_info.actress_pics = {
                resolve_alias(key): value for key, value in final_info.actress_pics.items()
            }

    # 检查是否所有必需的字段都已经获得了值
    for attr in Cfg().crawler.required_keys:
        if not getattr(final_info, attr, None):
            logger.error(f"所有抓取器均未获取到字段: '{attr}'，抓取失败")
            return False
    # 必需字段均已获得了值：将最终的数据附加到movie
    movie.info = final_info
    return True

def generate_names(movie: Movie):
    """按照模板生成相关文件的文件名"""

    def legalize_path(path: str):
        """
            Windows下文件名中不能包含换行 #467
            所以这里对文件路径进行合法化
        """
        return ''.join(c for c in path if c not in {'\n'})

    info = movie.info
    # 准备用来填充命名模板的字典
    d = info.get_info_dic()

    if info.actress and len(info.actress) > Cfg().summarizer.path.max_actress_count:
        logging.debug('女优人数过多，按配置保留了其中的前n个: ' + ','.join(info.actress))
        actress = info.actress[:Cfg().summarizer.path.max_actress_count] + ['…']
    else:
        actress = info.actress
    d['actress'] = ','.join(actress) if actress else Cfg().summarizer.default.actress

    # 保存label供后面判断裁剪图片的方式使用
    setattr(info, 'label', d['label'].upper())
    # 处理字段：替换不能作为文件名的字符，移除首尾的空字符
    for k, v in d.items():
        d[k] = replace_illegal_chars(v.strip())

    # 生成nfo文件中的影片标题
    nfo_title = Cfg().summarizer.nfo.title_pattern.format(**d)
    setattr(info, 'nfo_title', nfo_title)
    
    # 使用字典填充模板，生成相关文件的路径（多分片影片要考虑CD-x部分）
    cdx = '' if len(movie.files) <= 1 else '-CD1'
    if hasattr(info, 'title_break'):
        title_break = info.title_break
    else:
        title_break = split_by_punc(d['title'])
    if hasattr(info, 'ori_title_break'):
        ori_title_break = info.ori_title_break
    else:
        ori_title_break = split_by_punc(d['rawtitle'])
    copyd = d.copy()

    def legalize_info():
        if movie.save_dir != None:
            movie.save_dir = legalize_path(movie.save_dir)
        if movie.nfo_file != None:
            movie.nfo_file = legalize_path(movie.nfo_file)
        if movie.fanart_file != None:
            movie.fanart_file = legalize_path(movie.fanart_file)
        if movie.poster_file != None:
            movie.poster_file = legalize_path(movie.poster_file)
        if d['title'] != copyd['title']:
            logger.info(f"自动截短标题为:\n{copyd['title']}")
        if d['rawtitle'] != copyd['rawtitle']:
            logger.info(f"自动截短原始标题为:\n{copyd['rawtitle']}")
        return

    copyd['num'] = copyd['num'] + movie.attr_str
    # 按番号获取模式下没有输入文件，movie.files 为空；这里必须容错，
    # 否则 max() 会对空序列抛 ValueError。
    exts = [os.path.splitext(i)[1] for i in movie.files]
    longest_ext = max(exts, key=len) if exts else ''
    remaining = None
    save_dir = None
    basename = None
    for end in range(len(ori_title_break), 0, -1):
        copyd['rawtitle'] = replace_illegal_chars(''.join(ori_title_break[:end]).strip())
        for sub_end in range(len(title_break), 0, -1):
            copyd['title'] = replace_illegal_chars(''.join(title_break[:sub_end]).strip())
            save_dir = _pattern_save_dir(movie, copyd)
            basename = _pattern_basename(movie, copyd)
            if save_dir is None:
                # 本模式无法生成目录（例如按番号获取但未给目标文件夹）：
                # 直接跳到兜底分支，避免继续按标题截短循环
                break
            long_path = os.path.join(save_dir, basename + longest_ext)
            remaining = get_remaining_path_len(os.path.abspath(long_path))
            if remaining > 0:
                movie.save_dir = save_dir
                movie.basename = basename
                movie.nfo_file = os.path.join(save_dir, Cfg().summarizer.nfo.basename_pattern.format(**copyd) + '.nfo')
                movie.fanart_file = os.path.join(save_dir, Cfg().summarizer.fanart.basename_pattern.format(**copyd) + '.jpg')
                movie.poster_file = os.path.join(save_dir, Cfg().summarizer.cover.basename_pattern.format(**copyd) + '.jpg')
                return legalize_info()
        if save_dir is None:
            break
    # 走到这里有两种情况：路径过长需要硬性截短；或本模式不生成目录（save_dir 为 None）
    if save_dir is None:
        save_dir = os.path.dirname(movie.files[0]) if movie.files else os.getcwd()
        basename = movie_id_of(movie)
    else:
        # 硬性截短：remaining 此时必然已被赋值
        shorten = max(1, remaining or 1)
        copyd['title'] = copyd['title'][:shorten]
        copyd['rawtitle'] = copyd['rawtitle'][:shorten]
        basename = _pattern_basename(movie, copyd)
    movie.save_dir = save_dir
    movie.basename = basename

    movie.nfo_file = os.path.join(save_dir, Cfg().summarizer.nfo.basename_pattern.format(**copyd) + '.nfo')
    movie.fanart_file = os.path.join(save_dir, Cfg().summarizer.fanart.basename_pattern.format(**copyd) + '.jpg')
    movie.poster_file = os.path.join(save_dir, Cfg().summarizer.cover.basename_pattern.format(**copyd) + '.jpg')

    return legalize_info()


# 按番号获取模式：输出到 <nfo_folder>/<番号>/，文件名用番号
BY_ID_BASENAME_PATTERN = '{num}'


def _pattern_save_dir(movie, copyd):
    """按当前模式求出保存目录；返回 None 表示本模式不生成目录

    * 按目录整理：沿用配置里的 output_folder_pattern / basename_pattern
    * 按番号获取：目标文件夹 / 番号
    """
    if getattr(movie, 'by_id_mode', False):
        folder = getattr(movie, 'by_id_folder', '')
        if not folder:
            return None
        return os.path.join(folder, replace_illegal_chars(movie_id_of(movie)))
    if Cfg().summarizer.move_files:
        return os.path.normpath(
            Cfg().summarizer.path.output_folder_pattern.format(**copyd)).strip()
    return os.path.dirname(movie.files[0]) if movie.files else os.getcwd()


def _pattern_basename(movie, copyd):
    """按当前模式求出文件主名"""
    if getattr(movie, 'by_id_mode', False):
        return os.path.normpath(BY_ID_BASENAME_PATTERN.format(**copyd)).strip()
    if Cfg().summarizer.move_files:
        return os.path.normpath(
            Cfg().summarizer.path.basename_pattern.format(**copyd)).strip()
    if not movie.files:
        return movie_id_of(movie)
    filebasename = os.path.basename(movie.files[0])
    return filebasename.replace(os.path.splitext(filebasename)[1], '')

def reviewMovieID(all_movies, root):
    """人工检查每一部影片的番号"""
    count = len(all_movies)
    logger.info('进入手动模式检查番号: ')
    for i, movie in enumerate(all_movies, start=1):
        id = repr(movie)[7:-2]
        print(f'[{i}/{count}]\t{Fore.LIGHTMAGENTA_EX}{id}{Style.RESET_ALL}, 对应文件:')
        relpaths = [os.path.relpath(i, root) for i in movie.files]
        print('\n'.join(['  '+i for i in relpaths]))
        s = prompt("回车确认当前番号，或直接输入更正后的番号（如'ABC-123'或'cid:sqte00300'）", "更正后的番号")
        if not s:
            logger.info(f"已确认影片番号: {','.join(relpaths)}: {id}")
        else:
            s = s.strip()
            s_lc = s.lower()
            if s_lc.startswith(('cid:', 'cid=')):
                new_movie = Movie(cid=s_lc[4:])
                new_movie.data_src = 'cid'
                new_movie.files = movie.files
            elif s_lc.startswith('fc2'):
                new_movie = Movie(s)
                new_movie.data_src = 'fc2'
                new_movie.files = movie.files
            else:
                new_movie = Movie(s)
                new_movie.data_src = 'normal'
                new_movie.files = movie.files
            all_movies[i-1] = new_movie
            new_id = repr(new_movie)[7:-2]
            logger.info(f"已更正影片番号: {','.join(relpaths)}: {id} -> {new_id}")
        print()


SUBTITLE_MARK_FILE = Image.open(os.path.abspath(resource_path('image/sub_mark.png')))
UNCENSORED_MARK_FILE = Image.open(os.path.abspath(resource_path('image/unc_mark.png')))

def process_poster(movie: Movie):
    def should_use_ai_crop_match(label):
        for r in Cfg().summarizer.cover.crop.on_id_pattern:
            if re.match(r, label):
                return True
        return False
    crop_engine = None
    if (movie.info.uncensored or
       movie.data_src == 'fc2' or
       should_use_ai_crop_match(movie.info.label.upper())):
        crop_engine = Cfg().summarizer.cover.crop.engine
    cropper = get_cropper(crop_engine)
    fanart_image = Image.open(movie.fanart_file)
    fanart_cropped = cropper.crop(fanart_image)

    if Cfg().summarizer.cover.add_label:
        if movie.hard_sub:
            fanart_cropped = add_label_to_poster(fanart_cropped, SUBTITLE_MARK_FILE, LabelPostion.BOTTOM_RIGHT)
        if movie.uncensored:
            fanart_cropped = add_label_to_poster(fanart_cropped, UNCENSORED_MARK_FILE, LabelPostion.BOTTOM_LEFT)
    fanart_cropped.save(movie.poster_file)

class _NoopBar:
    """tqdm 的替身。GUI/worker 模式下不需要终端进度条，但代码路径要保持一致。"""

    def update(self, n=1):
        pass

    def set_description(self, *args, **kwargs):
        pass

    def close(self):
        pass


# 整理一部影片的步骤名。顺序即执行顺序，用于事件里的 step_index/step_total，
# 使前端可以在不解析描述文案的情况下正确渲染进度。
_NORMAL_MODE_STEPS = [    'crawl',       # 并发抓取各站点
    'summarize',   # 汇总多站点数据
    'translate',   # 翻译（仅当配置了翻译引擎）
    'generate_names',  # 按模板生成文件名
    'download_cover',  # 下载封面
    'process_poster',  # 裁剪/加标记生成海报
    'extrafanart',     # 下载剧照（仅当启用）
    'write_nfo',   # 写入 NFO
    'move_files',  # 移动影片文件（仅当启用）
]


def _planned_steps(movies=None):
    """本次运行实际会执行的步骤名列表（受配置与运行模式影响）"""
    steps = ['crawl', 'summarize']
    if Cfg().translator.engine:
        steps.append('translate')
    steps += ['generate_names', 'download_cover', 'process_poster']
    if Cfg().summarizer.extra_fanarts.enabled:
        steps.append('extrafanart')
    steps.append('write_nfo')
    # 按番号获取模式只产出元数据，没有源文件可移动，因此不包含 move_files。
    # 只要有一部影片处于该模式就按该模式计算（两种模式不会混在一次运行里）。
    by_id = any(getattr(m, 'by_id_mode', False) for m in (movies or []))
    if Cfg().summarizer.move_files and not by_id:
        steps.append('move_files')
    return steps


def RunNormalMode(all_movies, sink=None):
    """普通整理模式

    Args:
        all_movies: 待整理的影片列表
        sink: 事件接收方。为 None 时走既有的 tqdm 终端路径，行为不变。
    """
    event_mode = sink is not None
    sink = sink if sink is not None else NullSink()
    steps = _planned_steps(all_movies)
    total_step = len(steps)

    def check_step(result, step, msg='步骤错误'):
        """检查一个整理步骤的结果，并负责更新进度与上报事件"""
        if result:
            if not event_mode:
                inner_bar.update()
            sink(EventKind.MOVIE_STEP, step=step,
                 step_index=steps.index(step) + 1, step_total=total_step, status='ok')
        else:
            raise Exception(msg + '\n')

    outer_bar = tqdm(all_movies, desc='整理影片', ascii=True, leave=False) if not event_mode else all_movies
    total_movies = len(all_movies)

    return_movies = []
    for index, movie in enumerate(outer_bar, start=1):
        movie_key = movie_id_of(movie)
        sink(EventKind.MOVIE_STARTED, movie_id=movie_key, index=index, total=total_movies,
             data_src=movie.data_src,
             files=[os.path.split(i)[1] for i in movie.files])
        # 当前步骤：异常发生时用它把失败归因到具体步骤
        current_step = 'crawl'
        inner_bar = tqdm(total=total_step, desc='步骤', ascii=True, leave=False) if not event_mode else _NoopBar()
        try:
            # 初始化本次循环要整理影片任务
            filenames = [os.path.split(i)[1] for i in movie.files]
            logger.info('正在整理: ' + ', '.join(filenames))
            # 依次执行各个步骤
            inner_bar.set_description(f'启动并发任务')
            all_info = parallel_crawler(movie, inner_bar, sink)
            msg = f'为其配置的{len(Cfg().crawler.selection[movie.data_src])}个抓取器均未获取到影片信息'
            check_step(all_info, 'crawl', msg)

            current_step = 'summarize'
            inner_bar.set_description('汇总数据')
            has_required_keys = info_summary(movie, all_info)
            check_step(has_required_keys, 'summarize')

            if Cfg().translator.engine:
                current_step = 'translate'
                inner_bar.set_description('翻译影片信息')
                success = translate_movie_info(movie.info)
                check_step(success, 'translate')

            current_step = 'generate_names'
            generate_names(movie)
            check_step(movie.save_dir, 'generate_names', '无法按命名规则生成目标文件夹')
            if not os.path.exists(movie.save_dir):
                os.makedirs(movie.save_dir)

            current_step = 'download_cover'
            inner_bar.set_description('下载封面图片')
            if Cfg().summarizer.cover.highres:
                cover_dl = download_cover(movie.info.covers, movie.fanart_file, movie.info.big_covers)
            else:
                cover_dl = download_cover(movie.info.covers, movie.fanart_file)
            check_step(cover_dl, 'download_cover', '下载封面图片失败')
            cover, pic_path = cover_dl
            # 确保实际下载的封面的url与即将写入到movie.info中的一致
            if cover != movie.info.cover:
                movie.info.cover = cover
            # 根据实际下载的封面的格式更新fanart/poster等图片的文件名
            if pic_path != movie.fanart_file:
                movie.fanart_file = pic_path
                actual_ext = os.path.splitext(pic_path)[1]
                movie.poster_file = os.path.splitext(movie.poster_file)[0] + actual_ext

            current_step = 'process_poster'
            process_poster(movie)

            check_step(True, 'process_poster')

            if Cfg().summarizer.extra_fanarts.enabled:
                current_step = 'extrafanart'
                scrape_interval = Cfg().summarizer.extra_fanarts.scrap_interval.total_seconds()
                inner_bar.set_description('下载剧照')
                if movie.info.preview_pics:
                    extrafanartdir = os.path.join(movie.save_dir, 'extrafanart')
                    # 用 exist_ok 而非裸 mkdir：不移动文件时同一目录会被多部影片共用，
                    # 裸 mkdir 会让第二部影片抛 FileExistsError 并中断整个运行
                    os.makedirs(extrafanartdir, exist_ok=True)
                    for (id, pic_url) in enumerate(movie.info.preview_pics):
                        inner_bar.set_description(f"Downloading extrafanart {id} from url: {pic_url}")
                                                                                                                                
                        fanart_destination = os.path.join(extrafanartdir, f'{id}.png')
                        try:
                            info = download(pic_url, fanart_destination)
                            if valid_pic(fanart_destination):
                                filesize = get_fmt_size(pic_path)
                                width, height = get_pic_size(pic_path)
                                elapsed = time.strftime("%M:%S", time.gmtime(info['elapsed']))
                                speed = get_fmt_size(info['rate']) + '/s'
                                logger.info(f"已下载剧照{pic_url} {id}.png: {width}x{height}, {filesize} [{elapsed}, {speed}]")
                            else:
                                check_step(False, 'extrafanart', f"下载剧照{id}: {pic_url}失败")
                        except Exception:
                            check_step(False, 'extrafanart', f"下载剧照{id}: {pic_url}失败")
                        time.sleep(scrape_interval)
                check_step(True, 'extrafanart')

            current_step = 'write_nfo'
            inner_bar.set_description('写入NFO')
            write_nfo(movie.info, movie.nfo_file)
            check_step(True, 'write_nfo')
            if Cfg().summarizer.move_files and not getattr(movie, 'by_id_mode', False):
                current_step = 'move_files'
                inner_bar.set_description('移动影片文件')
                movie.rename_files(Cfg().summarizer.path.hard_link)
                check_step(True, 'move_files')
                logger.info(f'整理完成，相关文件已保存到: {movie.save_dir}\n')
            else:
                logger.info(f'刮削完成，相关文件已保存到: {movie.nfo_file}\n')

            if movie != all_movies[-1] and Cfg().crawler.sleep_after_scraping > Duration(0):
                time.sleep(Cfg().crawler.sleep_after_scraping.total_seconds())
            return_movies.append(movie)
            sink(EventKind.MOVIE_FINISHED, movie_id=movie_key, index=index, total=total_movies,
                 save_dir=movie.save_dir, nfo_file=movie.nfo_file,
                 title=getattr(movie.info, 'nfo_title', None))
        except Exception as e:
            # 既有的 except 被注释掉了，导致失败原因只出现在 stderr；这里上报事件
            # 以便前端能显示"哪部影片、哪一步、为什么失败"
            logger.debug(e, exc_info=True)
            sink(EventKind.MOVIE_FAILED, movie_id=movie_key, index=index, total=total_movies,
                 step=current_step, error=type(e).__name__, message=str(e))
        finally:
            inner_bar.close()
    return return_movies


def download_cover(covers, fanart_path, big_covers=[]):
    """下载封面图片"""
    # 优先下载高清封面
    for url in big_covers:
        pic_path = get_pic_path(fanart_path, url)
        for _ in range(Cfg().network.retry):
            try:
                info = download(url, pic_path)
                if valid_pic(pic_path):
                    filesize = get_fmt_size(pic_path)
                    width, height = get_pic_size(pic_path)
                    elapsed = time.strftime("%M:%S", time.gmtime(info['elapsed']))
                    speed = get_fmt_size(info['rate']) + '/s'
                    logger.info(f"已下载高清封面: {width}x{height}, {filesize} [{elapsed}, {speed}]")
                    return (url, pic_path)
            except requests.exceptions.HTTPError:
                # HTTPError通常说明猜测的高清封面地址实际不可用，因此不再重试
                break
    # 如果没有高清封面或高清封面下载失败
    for url in covers:
        pic_path = get_pic_path(fanart_path, url)
        for _ in range(Cfg().network.retry):
            try:
                download(url, pic_path)
                if valid_pic(pic_path):
                    logger.debug(f"已下载封面: '{url}'")
                    return (url, pic_path)
                else:
                    logger.debug(f"图片无效或已损坏: '{url}'，尝试更换下载地址")
                    break
            except Exception as e:
                logger.debug(e, exc_info=True)
    logger.error(f"下载封面图片失败")
    logger.debug('big_covers:'+str(big_covers) + ', covers'+str(covers))
    return None

def get_pic_path(fanart_path, url):
    fanart_base = os.path.splitext(fanart_path)[0]
    pic_extend = url.split('.')[-1]
    # 判断 url 是否带？后面的参数
    if '?' in pic_extend:
        pic_extend = pic_extend.split('?')[0]
        
    pic_path = fanart_base + "." + pic_extend
    return pic_path

def error_exit(success, err_info):
    """检查业务逻辑是否成功完成，如果失败则报错退出程序"""
    if not success:
        show_error_and_exit(err_info)


def _should_run_in_background():
    """macOS 上从 Finder 直接启动 .app 时，主流程放到后台执行"""
    return (
        sys.platform == 'darwin'
        and os.environ.get('JAVSP_BACKGROUND_WORKER') != '1'
        and not stdin_is_tty()
    )


def _build_worker_command(root: str):
    """构造后台 worker 的启动命令"""
    cmd = [sys.executable]
    if not getattr(sys, 'frozen', False):
        cmd += ['-m', 'javsp']
    # 继承父进程原有的 -c/--o... 参数；后面的同名参数会覆盖前面的值
    cmd += sys.argv[1:]
    cmd += [
        '--oscanner.input_directory', root,
        '--oother.check_update', 'false',
    ]
    return cmd


def _spawn_background_worker(root: str):
    """启动后台整理进程，父进程随后立即退出，避免前台转圈"""
    env = os.environ.copy()
    env['JAVSP_BACKGROUND_WORKER'] = '1'
    cmd = _build_worker_command(root)
    logger.info('启动后台整理进程: %s', root)
    try:
        subprocess.Popen(
            cmd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as e:
        show_error_and_exit(f'无法启动后台整理进程: {e}')
    show_macos_notification('JavSP 已在后台开始整理，完成后会弹窗通知。')


def entry():
    try:
        Cfg()
    except ValidationError as e:
        show_error_and_exit(f'配置校验失败: {e.errors()}')

    global actressAliasMap
    if Cfg().crawler.normalize_actress_name:
        actressAliasFilePath = resource_path("data/actress_alias.json")
        with open(actressAliasFilePath, "r", encoding="utf-8") as file:
            actressAliasMap = json.load(file)

    colorama.init(autoreset=True)

    # Finder 直接启动时，父进程只负责选目录和拉起后台 worker，
    # 检查更新等耗时操作留给后台 worker 或直接跳过。
    run_in_background = _should_run_in_background()
    if not run_in_background:
        # 检查更新
        version_info = 'JavSP ' + getattr(sys, 'javsp_version', '未知版本/从代码运行')
        logger.debug(version_info.center(60, '='))
        check_update(Cfg().other.check_update, Cfg().other.auto_update)

    root = get_scan_dir(Cfg().scanner.input_directory)
    error_exit(root, '未选择要扫描的文件夹')

    if run_in_background:
        _spawn_background_worker(root)
        sys.exit(0)

    # 导入抓取器，必须在chdir之前
    import_crawlers()
    os.chdir(root)

    print(f'扫描影片文件...')
    recognized = scan_movies(root)
    movie_count = len(recognized)
    recognize_fail = []
    error_exit(movie_count, '未找到影片文件')
    logger.info(f'扫描影片文件：共找到 {movie_count} 部影片')
    if Cfg().scanner.manual:
        reviewMovieID(recognized, root)
    finished_movies = RunNormalMode(recognized + recognize_fail)

    if sys.platform == 'darwin' and not stderr_is_tty():
        show_macos_alert(f'整理完成，共处理 {len(finished_movies)} 部影片。')
    sys.exit(0)


# 角色派发的实际执行点：放在模块末尾，确保 RunNormalMode 等符号都已定义，
# 延迟导入 worker 才不会触发循环导入。
if _ROLE_DISPATCH is not None:
    _ROLE_DISPATCH()


if __name__ == "__main__":
    try:
        entry()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        logger.exception('运行出错')
        show_error_and_exit(f'运行出错: {e}')
