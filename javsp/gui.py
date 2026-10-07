"""JavSP 桌面窗口入口。

启动顺序（顺序很重要）：

1. 先起 HTTP 服务（后台线程），拿到带 token 的 URL
2. 再创建 pywebview 窗口并加载该 URL
3. ``webview.start()`` 阻塞主线程 —— GUI 事件循环必须独占主线程
4. 关窗后清理 worker 子进程与 HTTP 服务

从源码运行::

    python -m javsp.gui

自检（无人值守，用于验证整条链路）::

    python -m javsp.gui --selftest --directory /path/to/videos
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
from pathlib import Path

from javsp.server import GuiServer

logger = logging.getLogger('javsp.gui')

WINDOW_TITLE = 'JavSP'


def _configure_logging() -> None:
    """GUI 进程的日志：默认只写 stderr，便于从终端启动时排查"""
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(
            '%(asctime)s [%(levelname)s] %(name)s: %(message)s'))
        root.addHandler(handler)
    root.setLevel(logging.INFO)


def _start_server(directory: str | None = None) -> GuiServer:
    server = GuiServer()
    if not server.webui_dir.is_dir():
        raise SystemExit(f'未找到前端资源目录: {server.webui_dir}')
    url = server.serve_forever_in_thread()
    logger.info('窗口地址: %s', url)
    if directory:
        server._directory = directory  # 供前端快照恢复
    return server


def _selftest(server: GuiServer, directory: str, timeout: float = 180.0) -> int:
    """无人值守自检：启动扫描 + 抓取，收集事件并打印摘要

    用于在没有人工点按的情况下验证「路径 -> 扫描 -> 抓取 -> 完成」整条链路。
    """
    collected: list[dict] = []
    # 用 gui.job_exited（worker 进程真正退出）作为收尾信号，而不是 run.finished：
    # run.finished 之后 worker 还在写文件/退出，此时就关服务会截断输出。
    exited = threading.Event()
    scan_done = threading.Event()

    q = server.subscribe()

    def consume():
        while True:
            try:
                evt = q.get(timeout=1)
            except Exception:
                continue
            if evt is None:
                break
            collected.append(evt)
            kind = evt['kind']
            if kind == 'gui.job_exited':
                payload = evt.get('payload') or {}
                # 必须等 worker 真正退出：scan.finished 早于进程退出，
                # 此时立即启动下一个任务会撞上 busy 保护。
                if payload.get('mode') == 'scan':
                    scan_done.set()
                elif payload.get('mode') == 'scrape':
                    exited.set()
                    break

    threading.Thread(target=consume, daemon=True).start()

    def drive():
        time.sleep(1.0)
        print('[selftest] 启动扫描…', file=sys.stderr)
        res = server.start_scan(directory)
        print(f'[selftest] scan -> {res}', file=sys.stderr)
        if not scan_done.wait(timeout=timeout):
            print('[selftest] 扫描超时', file=sys.stderr)
        else:
            print('[selftest] 扫描完成', file=sys.stderr)

        print('[selftest] 启动抓取…', file=sys.stderr)
        res = server.start_scrape(directory)
        print(f'[selftest] scrape -> {res}', file=sys.stderr)
        if not exited.wait(timeout=timeout):
            print('[selftest] 抓取超时，强制停止', file=sys.stderr)
        else:
            print('[selftest] 抓取完成', file=sys.stderr)

        # 给事件读取线程一点时间收尾，再关服务
        time.sleep(0.5)
        server.shutdown()
        try:
            import webview
            for w in webview.windows:
                w.destroy()
        except Exception:  # noqa: BLE001
            logger.debug('关闭窗口失败', exc_info=True)

    threading.Thread(target=drive, daemon=True).start()
    return 0


def _summarize(events) -> None:
    """打印自检摘要。events 为 GuiServer.recent_events() 返回的事件字典列表。"""
    counts: dict[str, int] = {}
    movies: dict[str, dict] = {}
    for evt in events:
        kind = evt.get('kind')
        p = evt.get('payload') or {}
        counts[kind] = counts.get(kind, 0) + 1
        mid = p.get('movie_id')
        if kind == 'movie.started' and mid:
            movies.setdefault(mid, {'sites': {}, 'status': 'active'})
        elif kind == 'crawler.succeeded' and mid:
            movies.setdefault(mid, {'sites': {}, 'status': 'active'})['sites'][p.get('crawler')] = 'ok'
        elif kind == 'crawler.failed' and mid:
            movies.setdefault(mid, {'sites': {}, 'status': 'active'})['sites'][p.get('crawler')] = 'err'
        elif kind == 'movie.finished' and mid:
            movies.setdefault(mid, {'sites': {}, 'status': 'active'})['status'] = 'done'
        elif kind == 'movie.failed' and mid:
            movies.setdefault(mid, {'sites': {}, 'status': 'active'})['status'] = 'failed'

    print('\n[selftest] 事件统计:', dict(sorted(counts.items())), file=sys.stderr)
    # 失败原因必须打印，否则自检只会显示一个笼统的 run.failed
    for evt in events:
        kind = evt.get('kind')
        p = evt.get('payload') or {}
        if kind in ('run.failed', 'movie.failed'):
            print(f"  [FAIL] {kind}: step={p.get('step')} error={p.get('error')} "
                  f"message={p.get('message')}", file=sys.stderr)
        elif kind == 'worker.stderr' and ('Error' in (p.get('message') or '')
                                         or '错误' in (p.get('message') or '')):
            print(f"  [stderr] {p.get('message')}", file=sys.stderr)
    for mid, info in movies.items():
        ok = [s for s, v in info.get('sites', {}).items() if v == 'ok']
        print(f'  {mid}: {info.get("status")} 成功站点={ok}', file=sys.stderr)


def main(argv=None) -> int:
    if argv is None:
        # 冻结产物用 `--gui` 作为角色标记（见 javsp/__main__.py 的派发），
        # 但它是给派发器看的，不是本模块的参数，这里要先剔除。
        argv = [a for a in sys.argv[1:] if a != '--gui']
    parser = argparse.ArgumentParser(prog='javsp.gui', description='JavSP 桌面界面')
    parser.add_argument('--selftest', action='store_true',
                        help='无人值守自检：自动跑一次扫描+抓取后退出')
    parser.add_argument('--directory', help='自检使用的影片目录')
    parser.add_argument('--headless', action='store_true',
                        help='只启动服务不起窗口（调试后端）')
    args = parser.parse_args(argv)

    _configure_logging()
    directory = None
    if args.directory:
        directory = str(Path(args.directory).expanduser().resolve())

    server = _start_server(directory)

    if args.headless:
        print(f'服务地址: {server.url}', file=sys.stderr)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            server.shutdown()
        return 0

    import webview

    if args.selftest:
        if not directory:
            raise SystemExit('--selftest 需要同时提供 --directory')
        # 自检是幂等的、且不应改动被测目录：config.yml 默认 move_files: true，
        # 会让影片文件被移动到 "#整理完成/" 并导致再次扫描找不到影片。
        # 正常使用时不加这个参数，仍按用户配置执行。
        server.extra_args = ['--osummarizer.move_files', 'false']
        _selftest(server, directory)

    window = webview.create_window(
        WINDOW_TITLE, server.url,
        width=1180, height=760, min_size=(900, 600),
        text_select=True,
    )
    # 目录选择用 pywebview 的原生面板：它内部会把调用分发到 GUI 主线程
    # （AppHelper.callAfter + 信号量），因此是本进程内最可靠的原生对话框实现。
    server.window = window

    def on_closing():
        # 关窗必须清理子进程，否则会留下孤儿 worker（P0.5 spike 验证项）
        logger.info('窗口关闭，正在清理…')
        server.shutdown()

    window.events.closing += on_closing

    try:
        webview.start()
    finally:
        server.shutdown()
        if args.selftest:
            _summarize(server.recent_events())
            return 0
    return 0


def entry() -> None:
    sys.exit(main())


if __name__ == '__main__':
    entry()
