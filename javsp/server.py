"""GUI 的本地服务层：HTTP API + SSE 事件流 + worker 子进程生命周期管理。

线程模型（与 P0.5 spike 的结论一致）：

    主线程          webview.start()  —— GUI 事件循环，必须独占主线程
    后台线程 A      ThreadingHTTPServer —— 提供静态资源、API 与 SSE
    后台线程 B      worker 输出读取 —— 逐行解析 NDJSON 并广播给所有 SSE 客户端
    子进程          javsp.worker —— 真正跑抓取管线，stdout 是事件流

只用标准库：项目依赖里没有任何 web 框架，cx_Freeze 冻结产物里也没有
wsgiref（P0.5 核查结论）。窗口是本机单用户使用，不需要引入额外依赖。

安全边界：仅绑定 127.0.0.1，并要求每个请求带上随机会话 token，
避免本机其它程序驱动抓取或读到本机文件路径。
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import OrderedDict, deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from javsp.config import Cfg
from javsp.events import Event, read_events
from javsp.lib import resource_path

logger = logging.getLogger(__name__)

# 事件历史保留条数：用于页面刷新/后连接时补齐上下文
EVENT_HISTORY_SIZE = 500

# 会话 token 的 Cookie 名。用于让 webview 的相对路径子资源请求（CSS/JS）
# 也能通过鉴权——它们不会携带主页面的 query string。
TOKEN_COOKIE = 'javsp_token'

# 远程图片代理缓存的最大条目数
IMAGE_CACHE_SIZE = 64


class _Handler(BaseHTTPRequestHandler):
    """请求处理。通过 server 上的属性访问共享状态。"""

    server_version = 'JavSP'
    protocol_version = 'HTTP/1.1'

    # 默认的 log_message 会往 stderr 刷访问日志，GUI 模式下没有意义
    def log_message(self, fmt, *args):  # noqa: A003
        logger.debug('%s - %s', self.address_string(), fmt % args)

    def _log_request_debug(self) -> None:
        """诊断用：打印每个请求的路径与是否带 token

        webview 的子资源请求（CSS/JS）若丢了 token 会被鉴权拦掉，
        表现为"界面无样式、只剩裸 HTML"。设 JAVSP_GUI_DEBUG=1 打开。
        """
        if os.environ.get('JAVSP_GUI_DEBUG') != '1':
            return
        query = parse_qs(urlparse(self.path).query)
        has_token = bool((query.get('token') or [None])[0] or self.headers.get('X-Auth'))
        print(f'[http] {self.command} {self.path[:130]} token={has_token}',
              file=sys.stderr, flush=True)

    # ---------- 工具方法 ----------

    @property
    def state(self) -> 'GuiServer':
        return self.server.gui  # type: ignore[attr-defined]

    def _check_token(self) -> bool:
        """校验会话 token。

        三种载体都要支持：

        * **Cookie**（主要）：webview 加载 ``style.css``/``app.js`` 这类相对
          路径子资源时**不会携带主页面的 query string**，实测会以
          ``GET /style.css``（无 token）请求，若只认 query 就会被 403 拦掉，
          页面退化成无样式的裸 HTML。
        * 查询参数：SSE（EventSource）与手工调试用。
        * ``X-Auth`` 头：前端 fetch 用。
        """
        supplied = (parse_qs(urlparse(self.path).query).get('token') or [None])[0]
        if not supplied:
            supplied = self.headers.get('X-Auth')
        if not supplied:
            supplied = self._cookie_token()
        if supplied != self.state.token:
            self._send_json({'error': 'bad_token'}, HTTPStatus.FORBIDDEN)
            return False
        return True

    def _cookie_token(self) -> Optional[str]:
        """从 Cookie 头里取出会话 token"""
        raw = self.headers.get('Cookie')
        if not raw:
            return None
        for part in raw.split(';'):
            name, _, value = part.strip().partition('=')
            if name == TOKEN_COOKIE:
                return value
        return None

    def _send_json(self, payload: Any, status=HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_file(self, path: Path, set_token_cookie: bool = False) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self._send_json({'error': 'not_found'}, HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header('Content-Type', _content_type(path))
        self.send_header('Content-Length', str(len(body)))
        if set_token_cookie:
            # 下发会话 Cookie，使后续相对路径的子资源请求（CSS/JS）也能通过鉴权
            self.send_header(
                'Set-Cookie',
                f'{TOKEN_COOKIE}={self.state.token}; Path=/; HttpOnly; SameSite=Strict')
        # GUI 资源随程序发布，不需要缓存，避免开发时改了看不到效果
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_json_body(self) -> Dict[str, Any]:
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            return {}
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            return {}

    # ---------- 路由 ----------

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        self._log_request_debug()
        route = urlparse(self.path).path
        if route == '/api/events':
            if not self._check_token():
                return
            self._serve_events()
            return
        if route == '/api/state':
            if not self._check_token():
                return
            self._send_json(self.state.snapshot())
            return
        if route == '/api/settings':
            if not self._check_token():
                return
            self._send_json(self.state.get_settings())
            return
        if route == '/api/image':
            if not self._check_token():
                return
            self._serve_image(parse_qs(urlparse(self.path).query))
            return
        # 静态资源仍要求 token，避免本机其它程序读到前端代码
        if not self._check_token():
            return
        rel = 'index.html' if route in ('/', '/index.html') else route.lstrip('/')
        candidate = (self.state.webui_dir / rel).resolve()
        # 防目录穿越：解析后必须仍在 webui 目录内
        if not str(candidate).startswith(str(self.state.webui_dir.resolve())):
            self._send_json({'error': 'forbidden'}, HTTPStatus.FORBIDDEN)
            return
        if not candidate.is_file():
            self._send_json({'error': 'not_found'}, HTTPStatus.NOT_FOUND)
            return
        # 只有主页面下发 Cookie；子资源请求带上它即可通过鉴权
        self._send_file(candidate, set_token_cookie=(rel == 'index.html'))

    def do_POST(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        if not self._check_token():
            return
        body = self._read_json_body()

        if route == '/api/select_directory':
            self._send_json(self.state.select_directory(body.get('initial') or ''))
        elif route == '/api/scan':
            directory = (body.get('directory') or '').strip()
            self._send_json(self.state.start_scan(directory))
        elif route == '/api/start':
            directory = (body.get('directory') or '').strip()
            self._send_json(self.state.start_scrape(directory))
        elif route == '/api/start_by_id':
            self._send_json(self.state.start_by_id(
                body.get('folder') or '', body.get('ids') or ''))
        elif route == '/api/settings/crawler_selection':
            self._send_json(self.state.update_crawler_selection(
                body.get('selection') or {}))
        elif route == '/api/stop':
            self._send_json(self.state.stop())
        elif route == '/api/log_tail':
            self._send_json({'events': self.state.recent_events(limit=body.get('limit') or 200)})
        elif route == '/api/movie_info':
            self._send_json(self.state.movie_info(
                body.get('path') or '', body.get('root') or ''))
        elif route == '/api/reveal':
            self._send_json(self.state.reveal(body.get('path') or ''))
        else:
            self._send_json({'error': 'not_found'}, HTTPStatus.NOT_FOUND)

    def _serve_image(self, query: Dict[str, Any]) -> None:
        """提供图片：本地文件或远程代理

        * ``path`` + ``root``：读取扫描目录内的本地图片（海报/剧照），
          用 ``_is_within`` 限制范围，避免把本机任意文件暴露给前端。
        * ``remote``：代理远程图片（女优头像等）。nfo 里存的是外链，
          由本地服务代理可以绕开 webview 的跨域/CSP 限制。仅限 http(s)，
          且仍需 token，避免变成开放的代理。
        """
        remote = (query.get('remote') or [None])[0]
        if remote:
            self._serve_remote_image(remote)
            return

        raw = (query.get('path') or [None])[0]
        root = (query.get('root') or [None])[0]
        if not raw:
            self._send_json({'error': 'no_path'}, HTTPStatus.BAD_REQUEST)
            return
        if not _is_within(raw, root):
            self._send_json({'error': 'forbidden'}, HTTPStatus.FORBIDDEN)
            return
        candidate = Path(raw).resolve()
        if not candidate.is_file():
            self._send_json({'error': 'not_found'}, HTTPStatus.NOT_FOUND)
            return
        self._send_file(candidate)

    def _serve_remote_image(self, url: str) -> None:
        """代理一张远程图片"""
        if not url.lower().startswith(('http://', 'https://')):
            self._send_json({'error': 'bad_url'}, HTTPStatus.BAD_REQUEST)
            return
        data, ctype = self.state.fetch_image(url)
        if data is None:
            self._send_json({'error': 'fetch_failed'}, HTTPStatus.BAD_GATEWAY)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header('Content-Type', ctype or 'image/jpeg')
        self.send_header('Content-Length', str(len(data)))
        # 同一次会话内会反复渲染，允许短期缓存
        self.send_header('Cache-Control', 'private, max-age=600')
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ---------- SSE ----------

    def _serve_events(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'keep-alive')
        self.end_headers()

        queue = self.state.subscribe()
        try:
            # 先补发历史，使页面刷新后仍能看到已发生的进度
            self._write_sse('snapshot', self.state.snapshot())
            for prev in self.state.recent_events():
                self._write_sse('event', prev)
            while True:
                try:
                    event = queue.get(timeout=15)
                except Exception:
                    # 队列空转：发注释行保活，同时探测连接是否还在
                    try:
                        self.wfile.write(b': keepalive\n\n')
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        break
                    continue
                if event is None:
                    break
                self._write_sse('event', event)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.state.unsubscribe(queue)

    def _write_sse(self, name: str, payload: Dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False)
        # SSE 的 data 不能包含裸换行；json.dumps 默认不产生换行
        frame = f'event: {name}\ndata: {data}\n\n'.encode('utf-8')
        self.wfile.write(frame)
        self.wfile.flush()


def _content_type(path: Path) -> str:
    return {
        '.html': 'text/html; charset=utf-8',
        '.css': 'text/css; charset=utf-8',
        '.js': 'application/javascript; charset=utf-8',
        '.json': 'application/json; charset=utf-8',
        '.svg': 'image/svg+xml',
        '.png': 'image/png',
        '.ico': 'image/x-icon',
        # 图片类型必须完整：WebKit 不会把 application/octet-stream 当图片渲染，
        # 缺了 jpg 会让气泡里的海报显示成破图占位符
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.webp': 'image/webp',
        '.gif': 'image/gif',
        '.bmp': 'image/bmp',
    }.get(path.suffix.lower(), 'application/octet-stream')


def resolve_webui_dir() -> Path:
    """定位前端资源目录

    两种布局都要支持：

    * 源码运行：``javsp/webui``（相对包目录）
    * 冻结打包：``<exe 目录>/webui`` 或 ``Contents/Resources/webui``
      （setup.py 的 include_files 目标），因为 ``javsp`` 在冻结产物里
      是被打进 lib/javsp.zip 的，不能作为资源目录。
    """
    candidates = [Path(__file__).resolve().parent / 'webui']
    try:
        candidates.append(Path(resource_path('webui')))
    except Exception:  # noqa: BLE001 - resource_path 不应阻断启动
        logger.debug('解析 webui 资源目录失败', exc_info=True)
    for candidate in candidates:
        if (candidate / 'index.html').is_file():
            return candidate
    # 都找不到时返回首选路径，由调用方给出明确报错
    return candidates[0]


def _find_nfo_for_path(video_path: str) -> Optional[str]:
    """为影片文件寻找 nfo（同目录 <basename>.nfo 或 movie.nfo）"""
    base = os.path.splitext(video_path)[0]
    for candidate in (base + '.nfo', os.path.join(os.path.dirname(video_path), 'movie.nfo')):
        if os.path.isfile(candidate):
            return candidate
    return None


def _locate_after_move(path: str, root: Optional[str]) -> Optional[Path]:
    """影片文件被"整理"移动后，尝试在输出目录里把它找回来

    启用 ``summarizer.move_files`` 时，文件会被移到
    ``output_folder_pattern`` 指定的目录（默认 ``#整理完成/<女优>/[番号] 标题/``），
    文件名也会按 ``basename_pattern`` 重写，因此**不能**按原文件名去找。

    这里按"同名字幕/番号"的线索做尽力而为的搜索：
    优先看 ``#整理完成`` 下是否存在与原文件同名、或按番号命名的文件。
    """
    if not root:
        return None
    source = Path(path)
    root_path = Path(root)
    if not root_path.is_dir():
        return None

    # 输出目录名取自配置；取不到时退回默认的 "#整理完成"
    try:
        pattern = Cfg().summarizer.path.output_folder_pattern
        prefix = str(pattern).split('{', 1)[0].strip('/').strip(os.sep) or '#整理完成'
    except Exception:  # noqa: BLE001 - 配置缺失不影响兜底搜索
        prefix = '#整理完成'

    candidates = [root_path / prefix]
    # 目录层级可能有多段（如 "a/b"），也可能根本不存在
    for candidate in candidates:
        if not candidate.is_dir():
            continue
        target_name = source.name
        stem = source.stem
        try:
            for found in candidate.rglob(target_name):
                if found.is_file():
                    return found
            # 文件名已被重写：按原文件名的番号部分模糊匹配
            for found in candidate.rglob(f'*{stem}*'):
                if found.is_file():
                    return found
        except OSError:
            logger.debug('搜索移动后的文件失败: %s', candidate, exc_info=True)
    return None


def _first_existing(folder: Path, names) -> Optional[str]:
    """返回目录中第一个存在的文件名（绝对路径）"""
    for name in names:
        candidate = folder / name
        if candidate.is_file():
            return str(candidate)
    return None


# 支持的图片扩展名（按番号/按目录两种模式下载的图片都可能是这些格式）
IMAGE_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp')


def _image_kind(path: Path, stem: str = '') -> Optional[str]:
    """把图片文件归类，用于界面分组

    ``stem`` 是影片文件名（不含扩展名）。本程序默认生成 ``poster.jpg`` /
    ``fanart.jpg``，但配置允许自定义命名（如 ``ABP-647-poster.jpg``），
    因此先剥掉 ``<stem>-`` 前缀再判断。
    """
    name = path.name.lower()
    if stem:
        prefix = stem.lower() + '-'
        if name.startswith(prefix):
            name = name[len(prefix):]
    if name.startswith('poster'):
        return 'poster'
    if name.startswith('fanart'):
        return 'fanart'
    if name.startswith('thumb'):
        return 'thumb'
    if name.startswith('landscape'):
        return 'landscape'
    if name.startswith('banner'):
        return 'banner'
    return None


def _collect_gallery(folder: Path, stem: str) -> Dict[str, Any]:
    """收集影片目录下的全部图片

    产出结构（按界面展示顺序）：

    * ``poster``   竖版海报
    * ``fanart``   横版封面
    * ``stills``   剧照（extrafanart 目录，Emby/Jellyfin 惯例）
    * ``other``    同目录下其它图片（thumb/landscape/banner 等）

    只收集文件名符合条件的图片，避免把无关文件塞进界面。
    """
    result: Dict[str, Any] = {'poster': None, 'fanart': None, 'stills': [], 'other': []}
    if not folder.is_dir():
        return result

    still_dir = folder / 'extrafanart'
    if still_dir.is_dir():
        try:
            entries = sorted(
                (p for p in still_dir.iterdir()
                 if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS),
                # 0.png 1.png ... 10.png 需要按数值而非字典序排列
                key=lambda p: (_numeric_prefix(p.stem), p.name.lower()))
            result['stills'] = [str(p) for p in entries]
        except OSError:
            logger.debug('读取 extrafanart 目录失败: %s', still_dir, exc_info=True)

    try:
        for path in sorted(folder.iterdir()):
            if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
                continue
            kind = _image_kind(path, stem)
            # 只接受与本影片相关或由本程序生成的图片名
            if kind is None and not path.stem.startswith(stem):
                continue
            if kind == 'poster' and result['poster'] is None:
                result['poster'] = str(path)
            elif kind == 'fanart' and result['fanart'] is None:
                result['fanart'] = str(path)
            elif kind == 'poster':
                result['other'].append(str(path))
            elif kind == 'fanart':
                result['other'].append(str(path))
            else:
                result['other'].append(str(path))
    except OSError:
        logger.debug('读取影片目录失败: %s', folder, exc_info=True)

    # 没有 poster 时退回 fanart，保证界面至少有一张主图
    if result['poster'] is None and result['fanart'] is not None:
        result['poster'] = result['fanart']
    return result


def _numeric_prefix(stem: str) -> int:
    """取文件名开头的数字用于排序（'10' > '9'）"""
    digits = ''
    for ch in stem:
        if ch.isdigit():
            digits += ch
        else:
            break
    return int(digits) if digits else 1 << 30


def _is_within(path: str, root: Optional[str]) -> bool:
    """判断 path 是否位于 root 目录内（防目录穿越）

    root 为空时一律拒绝，避免前端传错参数时把整个磁盘暴露出去。
    """
    if not root:
        return False
    try:
        target = Path(path).resolve()
        base = Path(root).resolve()
    except (OSError, ValueError):
        return False
    return target == base or base in target.parents


class GuiServer:
    """GUI 的后端状态与生命周期

    事件只由本类产生，格式统一为 ``{'kind': ..., 'ts': ..., 'payload': {...}}``，
    与 worker 的 NDJSON 事件同构，前端可以用同一套渲染逻辑处理。
    """

    def __init__(self, host: str = '127.0.0.1', port: int = 0,
                 webui_dir: Optional[Path] = None):
        self.host = host
        # 端口/token 可用环境变量固定，仅供自动化验证使用（正常使用随机分配，
        # 避免与本机其它程序冲突，也避免被其它进程猜到 token）
        self.port = int(os.environ.get('JAVSP_GUI_PORT') or port or 0)
        self.token = os.environ.get('JAVSP_GUI_TOKEN') or secrets.token_urlsafe(24)
        self.webui_dir = Path(webui_dir) if webui_dir else resolve_webui_dir()
        # pywebview 的窗口句柄，由 gui.py 在创建后注入。
        # 原生目录对话框需要它（内部会分发到 GUI 主线程）。
        self.window = None
        # 远程图片代理缓存（女优头像等）
        self._image_cache: 'OrderedDict[str, tuple]' = OrderedDict()
        self._image_lock = threading.Lock()

        self._lock = threading.Lock()
        self._subscribers: List['_QueueLike'] = []
        self._history: deque = deque(maxlen=EVENT_HISTORY_SIZE)
        # 单 worker：GUI 同一时刻只跑一个任务，避免多个 worker 争抢同一目录
        self._worker: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._monitor: Optional[threading.Thread] = None
        self._mode: Optional[str] = None      # 'scan' | 'scrape'
        self._directory: Optional[str] = None
        self._last_exit: Optional[int] = None
        self._dialog_thread: Optional[threading.Thread] = None
        # 追加到 worker 命令行的额外参数（自检等场景覆盖配置用）
        self.extra_args: List[str] = []

        self._httpd: Optional[ThreadingHTTPServer] = None
        self._http_thread: Optional[threading.Thread] = None

    # ---------- 事件分发 ----------

    def _emit(self, kind: str, **payload) -> None:
        """产生一条 GUI 事件并广播给所有订阅者"""
        event = {'kind': kind, 'ts': _now_iso(), 'payload': payload}
        with self._lock:
            self._history.append(event)
            targets = list(self._subscribers)
        for q in targets:
            try:
                q.put(event)
            except Exception:  # noqa: BLE001 - 单个订阅者异常不应影响其它订阅者
                logger.debug('投递事件到订阅者失败', exc_info=True)

    def recent_events(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        with self._lock:
            items = list(self._history)
        if limit:
            return items[-int(limit):]
        return items

    def subscribe(self):
        q = _Queue()
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    # ---------- 状态 ----------

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                'token': self.token,
                'mode': self._mode,
                'directory': self._directory,
                'running': self._is_running_locked(),
                'last_exit': self._last_exit,
                'recent': list(self._history)[-120:],
            }

    def _is_running_locked(self) -> bool:
        return self._worker is not None and self._worker.poll() is None

    def is_running(self) -> bool:
        with self._lock:
            return self._is_running_locked()

    # ---------- 目录选择 ----------

    def _native_dialog(self, initial: str) -> Optional[str]:
        """用 pywebview 的原生目录对话框选目录

        这是首选实现：pywebview 的 cocoa 后端内部用
        ``AppHelper.callAfter`` + 信号量把对话框分发到 GUI 主线程，
        因此**可以从 HTTP 处理线程安全调用**，而且面板由本进程的 GUI
        主线程驱动，会正常显示在最前面。

        相比 osascript 的好处：不依赖自动化权限（TCC），也不需要
        Info.plist 里的 NSAppleEventsUsageDescription。
        """
        window = self.window
        if window is None:
            try:
                import webview
                window = webview.windows[0] if webview.windows else None
            except Exception:  # noqa: BLE001
                window = None
        if window is None or not hasattr(window, 'create_file_dialog'):
            return None
        import webview
        dialog_type = getattr(
            getattr(webview, 'FileDialog', None), 'FOLDER',
            getattr(webview, 'FOLDER_DIALOG', 20))
        result = window.create_file_dialog(dialog_type, directory=initial or '')
        if not result:
            return None
        # 单目录选择时返回的是 tuple/list，取第一项
        if isinstance(result, (list, tuple)):
            return str(result[0]) if result else None
        return str(result)

    # ---------- 影片信息（已整理后的元数据） ----------

    def fetch_image(self, url: str, timeout: float = 8.0):
        """抓取远程图片，返回 (bytes, content_type)；失败返回 (None, None)

        带一个小容量 LRU 缓存：同一部影片的气泡会被反复打开，
        女优头像通常来自同一批地址。
        """
        with self._image_lock:
            hit = self._image_cache.get(url)
            if hit is not None:
                self._image_cache.move_to_end(url)
                return hit
        try:
            import requests
            resp = requests.get(url, timeout=timeout, headers={'User-Agent': 'Mozilla/5.0'})
            resp.raise_for_status()
            if len(resp.content) > 8 * 1024 * 1024:
                return None, None
            value = (resp.content, resp.headers.get('Content-Type'))
        except Exception:  # noqa: BLE001 - 远程图片失败不应让接口报错
            logger.debug('代理远程图片失败: %s', url, exc_info=True)
            return None, None
        with self._image_lock:
            self._image_cache[url] = value
            if len(self._image_cache) > IMAGE_CACHE_SIZE:
                self._image_cache.popitem(last=False)
        return value

    def movie_info(self, path: str, root: str = '') -> Dict[str, Any]:
        """读取某部影片的元数据（来自同目录的 nfo）

        界面点击影片文件时调用，用于在气泡里展示"整理后"的信息。
        没有 nfo 时返回 ``scraped: False``，由前端提示尚未整理。
        """
        if not path:
            return {'ok': False, 'error': 'no_path', 'message': '缺少文件路径'}
        if not _is_within(path, root or self._directory):
            return {'ok': False, 'error': 'forbidden',
                    'message': '只能查看已扫描目录内的影片'}
        target = Path(path)
        if not target.is_file():
            # 文件可能已被"整理"移动到 #整理完成/ 下。前端正常情况下会跟着
            # movie.finished 更新路径，这里兜底处理历史截图/旧状态，
            # 避免只抛一句"文件不存在"让人以为是 bug。
            moved = _locate_after_move(path, root or self._directory)
            if moved:
                return {'ok': False, 'error': 'moved',
                        'moved_to': str(moved),
                        'message': f'文件已整理到: {moved}'}
            return {'ok': False, 'error': 'not_found',
                    'message': f'文件不存在: {path}'}

        nfo = _find_nfo_for_path(str(target))
        result: Dict[str, Any] = {
            'ok': True,
            'path': str(target),
            'filename': target.name,
            'dir': str(target.parent),
            'size': target.stat().st_size,
            'scraped': nfo is not None,
            'nfo': nfo,
        }
        if nfo is None:
            result['message'] = '这部影片尚未整理（没有找到 nfo 文件）'
            return result

        from javsp.nfo import read_nfo
        try:
            result['info'] = read_nfo(nfo)
        except Exception as e:  # noqa: BLE001 - nfo 损坏不应让整个接口失败
            logger.debug('解析 nfo 失败: %s', nfo, exc_info=True)
            result['scraped'] = False
            result['error'] = 'nfo_parse_failed'
            result['message'] = f'nfo 解析失败: {e}'
            return result

        # 附带同目录的本地图片（供气泡展示）
        gallery = _collect_gallery(target.parent, target.stem)
        result['poster'] = gallery['poster']
        result['fanart'] = gallery['fanart']
        result['gallery'] = gallery
        result['image_count'] = (
            (1 if gallery['poster'] else 0)
            + (1 if gallery['fanart'] else 0)
            + len(gallery['stills'])
            + len(gallery['other'])
        )
        return result

    def reveal(self, path: str) -> Dict[str, Any]:
        """在系统文件管理器中定位该文件（macOS 为访达）"""
        if not path:
            return {'ok': False, 'error': 'no_path'}
        target = Path(path)
        if not target.exists():
            return {'ok': False, 'error': 'not_found',
                    'message': f'路径不存在: {path}'}
        try:
            if sys.platform == 'darwin':
                subprocess.Popen(['open', '-R', str(target)])
            elif sys.platform == 'win32':
                subprocess.Popen(['explorer', f'/select,{target}'])
            else:
                subprocess.Popen(['xdg-open', str(target.parent)])
        except OSError as e:
            return {'ok': False, 'error': 'spawn_failed', 'message': str(e)}
        return {'ok': True}

    # ---------- 目录选择 ----------

    def select_directory(self, initial: str = '') -> Dict[str, Any]:
        """弹出原生目录选择框（异步，不阻塞 HTTP 请求）

        两个关键约束：

        1. **不能在请求线程里同步等待**。原生对话框要等用户操作，可能几十秒；
           实测在无 GUI 会话的环境下会一直阻塞。因此放到独立线程执行，
           结果通过 ``gui.directory_selected`` 事件推给前端。
        2. **优先用 pywebview 原生对话框**，osascript 仅作兜底。
           从终端启动 app 时 osascript 面板可能不在最前（用户会以为"没反应"），
           而 pywebview 的面板由 GUI 主线程驱动。
        """
        with self._lock:
            if self._dialog_thread is not None and self._dialog_thread.is_alive():
                return {'ok': False, 'error': 'busy',
                        'message': '目录选择框已经打开'}
            self._dialog_thread = threading.Thread(
                target=self._run_directory_dialog, args=(initial,),
                name='gui-dir-dialog', daemon=True)
            self._dialog_thread.start()
        self._emit('gui.directory_dialog_opened')
        # 立刻返回：前端用事件拿结果，避免请求长时间挂起
        return {'ok': True, 'async': True}

    def _run_directory_dialog(self, initial: str) -> None:
        """在独立线程里取目录，结果以事件上报"""
        path = None
        error = None
        try:
            path = self._native_dialog(initial)
        except Exception as e:  # noqa: BLE001
            logger.debug('pywebview 原生对话框失败，回退 osascript', exc_info=True)
            error = type(e).__name__

        if not path and error is not None:
            # 兜底：osascript choose folder（需要自动化权限）
            try:
                from javsp.func import select_folder
                path = select_folder(initial, use_macos_dialog=True)
            except SystemExit:
                # 无 GUI 可用时 select_folder 会 exit(1)，必须拦住：
                # 在服务进程里 exit 会杀掉整个界面
                self._emit('gui.directory_selected', ok=False, error='unavailable',
                           message='当前环境无法打开目录选择框')
                return
            except Exception as e:  # noqa: BLE001
                logger.debug('osascript 对话框也失败', exc_info=True)
                self._emit('gui.directory_selected', ok=False,
                           error=type(e).__name__, message=str(e))
                return

        if path:
            self._emit('gui.directory_selected', ok=True, path=path)
        else:
            self._emit('gui.directory_selected', ok=False, error='cancelled')

    # ---------- worker 生命周期 ----------

    def _build_command(self, directory: str, mode: str = 'scrape') -> List[str]:
        """构造 worker 命令

        复用 confz 的 ``--o<key>`` 注入方式，与既有 CLI/后台进程一致。

        冻结后不能再用 ``-m javsp.worker``：打包产物里没有模块搜索路径，
        而是让同一个可执行文件以 ``--javsp-worker`` 重新启动自己
        （角色派发见 javsp/__main__.py 顶部）。
        """
        if getattr(sys, 'frozen', False):
            cmd = [sys.executable, '--javsp-worker']
        else:
            cmd = [sys.executable, '-m', 'javsp.worker']
        cmd += [
            # by_id 模式不需要扫描目录，但 confz 仍要求该参数可解析；
            # 传目标文件夹即可（worker 在 by_id 模式下不会去扫描它）
            '--oscanner.input_directory', directory,
            # GUI 模式下人工核对番号应在界面里完成，不能阻塞 worker
            '--oscanner.manual', 'false',
            '--oother.check_update', 'false',
        ]
        # 额外参数：自检等场景用它覆盖配置（例如禁止移动文件）
        cmd += list(self.extra_args)
        return cmd

    def _spawn(self, directory: str, mode: str, ids: str = '') -> Dict[str, Any]:
        with self._lock:
            if self._is_running_locked():
                return {'ok': False, 'error': 'busy',
                        'message': '已有任务正在运行，请先停止'}
            env = os.environ.copy()
            # 阻止 __main__ 再派生一层后台进程，并让 worker 跳过文件日志
            env['JAVSP_BACKGROUND_WORKER'] = '1'
            env['JAVSP_WORKER_MODE'] = '1'
            env['PYTHONIOENCODING'] = 'utf-8'
            # 显式指定设置文件，保证父子进程读写同一份（否则 worker 会按
            # 默认路径找，若 GUI 用 JAVSP_SETTINGS_FILE 覆盖过就会不一致）
            from javsp.settings import settings_path
            env['JAVSP_SETTINGS_FILE'] = str(settings_path())
            if mode == 'scan':
                env['JAVSP_SCAN_ONLY'] = '1'
            else:
                env.pop('JAVSP_SCAN_ONLY', None)
            # 按番号获取模式：不走目录扫描，直接用目标文件夹 + 番号列表
            if mode == 'by_id':
                env['JAVSP_BY_ID'] = '1'
                env['JAVSP_BY_ID_DIR'] = directory
                env['JAVSP_BY_ID_IDS'] = ids or ''
            else:
                env.pop('JAVSP_BY_ID', None)
                env.pop('JAVSP_BY_ID_DIR', None)
                env.pop('JAVSP_BY_ID_IDS', None)
            # 从源码运行时需要让子进程能找到 javsp 包
            if not getattr(sys, 'frozen', False):
                root = str(Path(__file__).resolve().parent.parent)
                env['PYTHONPATH'] = root + os.pathsep + env.get('PYTHONPATH', '')

            try:
                proc = subprocess.Popen(
                    self._build_command(directory, mode),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding='utf-8', errors='replace',
                    env=env, cwd=str(Path(directory)),
                    # 独立进程组：停止时可以整组杀掉
                    start_new_session=True,
                )
            except OSError as e:
                return {'ok': False, 'error': 'spawn_failed', 'message': str(e)}

            self._worker = proc
            self._mode = mode
            self._directory = directory
            self._last_exit = None

        self._emit('gui.job_started', mode=mode, directory=directory, pid=proc.pid)
        self._reader = threading.Thread(target=self._read_events, args=(proc,),
                                        name='worker-events', daemon=True)
        self._reader.start()
        self._monitor = threading.Thread(target=self._read_stderr, args=(proc,),
                                        name='worker-stderr', daemon=True)
        self._monitor.start()
        threading.Thread(target=self._await_exit, args=(proc, mode),
                         name='worker-exit', daemon=True).start()
        return {'ok': True, 'pid': proc.pid, 'mode': mode}

    def start_scan(self, directory: str) -> Dict[str, Any]:
        check = _validate_directory(directory)
        if check is not None:
            return check
        # 记录目录：前端拿它当图片接口的 root，也是"当前任务目录"的唯一来源
        self._remember_directory(directory)
        return self._spawn(os.path.abspath(directory), 'scan')

    # ---------- 设置 ----------

    def get_settings(self) -> Dict[str, Any]:
        """返回设置界面需要的全部信息

        站点清单由服务端生成（枚举 + 出厂默认表），前端不硬编码，
        避免两处清单再次漂移——历史上 config.yml 就漏配过 5 个抓取器。
        """
        from javsp.settings import (
            GROUP_LABELS, all_crawlers_by_group, default_enabled_by_group,
            load_crawler_selection, settings_path,
        )

        saved = load_crawler_selection()
        defaults = default_enabled_by_group()
        by_group = all_crawlers_by_group()

        def is_available(name: str) -> tuple:
            """站点是否可用；fc2fan 依赖本地镜像路径"""
            if name == 'fc2fan':
                path = Cfg().crawler.fc2fan_local_path
                if not (path and os.path.isdir(str(path))):
                    return False, '需要先在 config.yml 配置 fc2fan 的本地镜像路径'
            return True, ''

        groups = []
        for group in by_group:
            # 用户没保存过该组时，回落到出厂默认
            enabled = set(saved.get(group) or defaults.get(group) or [])
            items = []
            for name in by_group[group]:
                available, note = is_available(name)
                items.append({
                    'id': name,
                    'enabled': name in enabled,
                    'available': available,
                    'note': note,
                })
            groups.append({
                'key': group,
                'label': GROUP_LABELS.get(group, group),
                'crawlers': items,
            })

        return {
            'ok': True,
            'groups': groups,
            'defaults': defaults,
            'settings_file': str(settings_path()),
        }

    def update_crawler_selection(self, selection: Dict[str, Any]) -> Dict[str, Any]:
        """保存抓取器开关

        校验要点：

        * 每组至少保留 1 个——否则该类型的影片必然找不到任何站点，
          这类"保存后必然失败"的状态不该允许写入
        * 站点 id 必须是已知的，未知项直接拒绝（而不是静默丢弃，
          否则用户会以为已经保存成功）
        """
        from javsp.settings import (
            GROUP_KEYS, all_crawlers_by_group, normalize_selection,
            save_crawler_selection,
        )
        if not isinstance(selection, dict):
            return {'ok': False, 'error': 'bad_payload',
                    'message': 'selection 必须是对象'}

        normalized = normalize_selection(selection)
        by_group = all_crawlers_by_group()

        # 未知站点：明确报错，避免"看起来保存了其实没有"
        known = {name for names in by_group.values() for name in names}
        unknown = [n for group in GROUP_KEYS for n in (selection.get(group) or [])
                   if isinstance(n, str) and n not in known]
        if unknown:
            return {'ok': False, 'error': 'unknown_crawler',
                    'message': f'未知的抓取器: {", ".join(sorted(set(unknown)))}'}

        # 提交里出现的组必须至少留一个
        empty = [g for g in GROUP_KEYS
                 if g in selection and not normalized.get(g)]
        if empty:
            labels = ', '.join(empty)
            return {'ok': False, 'error': 'empty_group',
                    'message': f'分组不能一个都不选: {labels}'}

        try:
            path = save_crawler_selection(normalized)
        except OSError as e:
            logger.warning('保存设置失败: %s', e, exc_info=True)
            return {'ok': False, 'error': 'save_failed',
                    'message': f'无法写入设置文件: {e}'}

        self._emit('gui.settings_changed', section='crawler_selection')
        return {'ok': True, 'selection': normalized, 'settings_file': str(path),
                'message': '设置已保存，将在下一次任务生效'}

    def start_by_id(self, folder: str, ids_text: str) -> Dict[str, Any]:
        """按番号获取元数据

        与"按目录"不同：不扫描任何目录，直接按给定番号抓取，
        结果写入 ``<folder>/<番号>/``。目标文件夹允许不存在（会自动创建）。
        """
        from javsp.worker import parse_movie_ids

        ids = parse_movie_ids(ids_text or '')
        if not ids:
            return {'ok': False, 'error': 'no_ids', 'message': '请至少输入一个番号'}
        if not folder or not folder.strip():
            # 目标文件夹留空时给一个合理的默认值（桌面），避免用户必须先选目录
            folder = str(Path.home() / 'Desktop')
        folder = os.path.abspath(os.path.expanduser(folder.strip()))

        # 目标文件夹允许不存在，但必须是可创建/可写的目录
        if os.path.exists(folder) and not os.path.isdir(folder):
            return {'ok': False, 'error': 'not_a_directory',
                    'message': f'目标不是文件夹: {folder}'}
        if not os.path.exists(folder):
            try:
                os.makedirs(folder, exist_ok=True)
            except OSError as e:
                return {'ok': False, 'error': 'mkdir_failed',
                        'message': f'无法创建目标文件夹: {e}'}
        if not os.access(folder, os.W_OK):
            return {'ok': False, 'error': 'not_writable',
                    'message': f'目标文件夹不可写: {folder}'}

        self._remember_directory(folder)
        return self._spawn(folder, 'by_id', ids='\n'.join(ids))

    def _remember_directory(self, directory: str) -> None:
        """记住本次任务使用的目录

        前端用它作为图片接口的 root 参数（限制可读取范围），
        同时页面刷新后能从快照恢复目录框内容。
        """
        with self._lock:
            self._directory = os.path.abspath(directory)
        self._emit('gui.directory_changed', directory=self._directory)

    def start_scrape(self, directory: str) -> Dict[str, Any]:
        check = _validate_directory(directory)
        if check is not None:
            return check
        self._remember_directory(directory)
        return self._spawn(os.path.abspath(directory), 'scrape')

    def _read_events(self, proc: subprocess.Popen) -> None:
        """逐行解析 worker 的 NDJSON 事件并广播"""
        assert proc.stdout is not None
        try:
            for event in read_events(proc.stdout):
                self._emit(event.kind, **event.payload)
        except Exception:  # noqa: BLE001 - 读取线程不能把异常抛给主线程
            logger.debug('读取 worker 事件流失败', exc_info=True)

    def _read_stderr(self, proc: subprocess.Popen) -> None:
        """把 worker 的 stderr 收进事件流，避免管道写满阻塞 worker

        始终要读（否则管道缓冲区满会卡住 worker），但默认不把每行都广播出去：
        worker 的 tqdm 进度条会产生大量 stderr 行，转发它们只会淹没事件流。
        需要排查时用 JAVSP_GUI_DEBUG=1 打开。
        """
        assert proc.stderr is not None
        debug = os.environ.get('JAVSP_GUI_DEBUG') == '1'
        tail: deque = deque(maxlen=20)
        try:
            for line in proc.stderr:
                line = line.strip()
                if not line:
                    continue
                tail.append(line)
                if debug:
                    self._emit('worker.stderr', message=line)
        except Exception:  # noqa: BLE001
            logger.debug('读取 worker stderr 失败', exc_info=True)
        finally:
            if tail:
                # 只保留尾部摘要，供失败时排查
                self._emit('worker.stderr_tail', lines=list(tail))

    def _await_exit(self, proc: subprocess.Popen, mode: str) -> None:
        code = proc.wait()
        with self._lock:
            if self._worker is proc:
                self._last_exit = code
                self._worker = None
        self._emit('gui.job_exited', mode=mode, exit_code=code,
                   ok=(code == 0))

    def stop(self) -> Dict[str, Any]:
        """停止当前任务：先温和终止进程组，超时再强杀"""
        with self._lock:
            proc = self._worker
            running = self._is_running_locked()
        if proc is None or not running:
            return {'ok': True, 'stopped': False, 'message': '没有正在运行的任务'}
        self._emit('gui.stopping', pid=proc.pid)
        if not _kill_process_group(proc):
            return {'ok': False, 'error': 'kill_failed'}
        self._emit('gui.stopped', pid=proc.pid)
        return {'ok': True, 'stopped': True}

    def shutdown(self) -> None:
        """关闭时清理：停掉 worker 与 HTTP 服务，不留孤儿进程"""
        try:
            self.stop()
        except Exception:  # noqa: BLE001
            logger.debug('关闭时停止 worker 失败', exc_info=True)
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:  # noqa: BLE001
                logger.debug('关闭 HTTP 服务失败', exc_info=True)
            self._httpd = None

    # ---------- HTTP 服务 ----------

    def serve_forever_in_thread(self) -> str:
        """在后台线程启动 HTTP 服务，返回窗口可加载的 URL"""
        self._httpd = ThreadingHTTPServer((self.host, self.port), _Handler)
        self._httpd.daemon_threads = True
        self._httpd.gui = self  # type: ignore[attr-defined]
        self.port = self._httpd.server_address[1]
        self._http_thread = threading.Thread(
            target=self._httpd.serve_forever, name='gui-http', daemon=True)
        self._http_thread.start()
        logger.info('GUI 服务已启动: http://%s:%s', self.host, self.port)
        return self.url

    @property
    def url(self) -> str:
        return f'http://{self.host}:{self.port}/?token={self.token}'


class _Queue:
    """带超时的极简队列。用标准库 queue.Queue 亦可，这里只为收窄接口。"""

    def __init__(self, maxsize: int = 1000):
        import queue
        self._q: 'queue.Queue' = queue.Queue(maxsize=maxsize)

    def put(self, item) -> None:
        import queue
        try:
            self._q.put_nowait(item)
        except queue.Full:
            # 前端消费不过来时丢弃最旧的，保证不阻塞抓取
            try:
                self._q.get_nowait()
                self._q.put_nowait(item)
            except Exception:  # noqa: BLE001
                pass

    def get(self, timeout: Optional[float] = None):
        import queue
        return self._q.get(timeout=timeout)


# 供类型标注使用
_QueueLike = _Queue


def _now_iso() -> str:
    from javsp.events import now_iso
    return now_iso()


def _validate_directory(directory: str) -> Optional[Dict[str, Any]]:
    """校验扫描目录，非法时返回错误响应"""
    if not directory:
        return {'ok': False, 'error': 'no_directory', 'message': '请先选择影片目录'}
    path = Path(directory).expanduser()
    if not path.exists():
        return {'ok': False, 'error': 'not_found',
                'message': f'目录不存在: {directory}'}
    if not path.is_dir():
        return {'ok': False, 'error': 'not_a_directory',
                'message': f'不是目录: {directory}'}
    if not os.access(path, os.R_OK):
        return {'ok': False, 'error': 'not_readable',
                'message': f'目录不可读: {directory}'}
    return None


def _kill_process_group(proc: subprocess.Popen, grace: float = 3.0) -> bool:
    """终止 worker 所在的整个进程组

    用进程组而不是单个 PID：worker 内部还会拉起抓取线程与可能的子进程，
    只杀主进程会留下孤儿（P0.5 spike 已验证 killpg 干净）。
    """
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return True
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return True
        except OSError:
            logger.debug('killpg(%s, %s) 失败', pgid, sig, exc_info=True)
        try:
            proc.wait(timeout=grace)
            return True
        except subprocess.TimeoutExpired:
            continue
    return proc.poll() is not None


def find_free_port(host: str = '127.0.0.1') -> int:
    """让操作系统分配一个空闲端口。

    先探测再绑定之间存在竞态，因此实际绑定时仍以 ThreadingHTTPServer
    成功为准（端口 0 由内核分配）。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]
