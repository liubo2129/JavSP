"""GUI 后端（javsp/server.py）的无人值守验证。

不启动窗口，只验证：API 鉴权、SSE 事件流、扫描/抓取/停止的完整生命周期。

用法::

    python tools/gui_backend_check.py /path/to/videos
"""
from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJ_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ_ROOT))

from javsp.server import GuiServer  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = '') -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{(' -> ' + detail) if detail else ''}")
    if not cond:
        failures.append(name)


def post(server: GuiServer, path: str, body: dict, token: str | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(
        f'http://127.0.0.1:{server.port}{path}',
        data=json.dumps(body).encode('utf-8'),
        headers={'Content-Type': 'application/json',
                 'X-Auth': token if token is not None else server.token},
        method='POST')
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode('utf-8'))
        except Exception:
            return e.code, {}


def get(server: GuiServer, path: str, token: str | None = None) -> tuple[int, bytes]:
    sep = '&' if '?' in path else '?'
    url = f'http://127.0.0.1:{server.port}{path}'
    if token is not None:
        url += f'{sep}token={token}'
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def main() -> int:
    if len(sys.argv) < 2:
        print('用法: gui_backend_check.py /path/to/videos', file=sys.stderr)
        return 2
    directory = str(Path(sys.argv[1]).expanduser().resolve())

    server = GuiServer()
    server.serve_forever_in_thread()
    print(f'[*] 服务已启动: http://127.0.0.1:{server.port}')

    # --- 静态资源与鉴权 ---
    print('[*] 静态资源与 token 鉴权')
    status, body = get(server, '/', token=server.token)
    # 只断言标题前缀，避免因为副标题文案调整而误报
    check('GET / 返回 index.html', status == 200 and b'<title>JavSP' in body)
    check('index.html 引用了样式表', status == 200 and b'style.css' in body)
    status, body = get(server, '/app.js', token=server.token)
    check('GET /app.js 可访问', status == 200 and b'use strict' in body)
    status, _ = get(server, '/api/state', token=None)
    check('缺少 token 时被拒绝(403)', status == 403, f'status={status}')
    status, _ = get(server, '/api/state', token='wrong-token')
    check('错误 token 被拒绝(403)', status == 403, f'status={status}')
    status, _ = get(server, '/../../etc/passwd', token=server.token)
    check('目录穿越被拒绝', status in (403, 404), f'status={status}')

    # --- 状态查询 ---
    status, raw = get(server, '/api/state', token=server.token)
    state = json.loads(raw.decode('utf-8'))
    check('/api/state 返回 initial 状态',
          status == 200 and state['mode'] is None and state['running'] is False,
          f"mode={state.get('mode')} running={state.get('running')}")

    # --- 目录对话框不能阻塞请求线程 ---
    # 注意：这里刻意不真的去弹原生对话框（会阻塞且需要人工点击）。
    # 只验证接口是异步的：立即返回，结果走 gui.directory_selected 事件。
    print('[*] 目录选择接口（异步、不阻塞）')
    t0 = time.time()
    status, res = post(server, '/api/select_directory', {})
    elapsed = time.time() - t0
    check('select_directory 立即返回', elapsed < 2.0, f'{elapsed:.2f}s')
    check('select_directory 声明为异步', res.get('ok') is True and res.get('async') is True,
          str(res))
    # 再次调用应提示已打开（此时无 GUI 会话，对话框线程可能已快速失败，
    # 因此两次调用都视为可接受，只要不阻塞）
    t0 = time.time()
    status, res2 = post(server, '/api/select_directory', {})
    check('重复调用不会阻塞', time.time() - t0 < 2.0, str(res2))

    # --- 目录校验 ---
    status, res = post(server, '/api/scan', {'directory': ''})
    check('空目录被拒绝', res.get('error') == 'no_directory', str(res))
    status, res = post(server, '/api/scan', {'directory': '/nonexistent-xyz'})
    check('不存在目录被拒绝', res.get('error') == 'not_found', str(res))

    # --- SSE 订阅 ---
    events: list[dict] = []
    stop = threading.Event()

    def sse_reader():
        url = f'http://127.0.0.1:{server.port}/api/events?token={server.token}'
        try:
            with urllib.request.urlopen(url, timeout=200) as r:
                name = None
                while not stop.is_set():
                    line = r.readline()
                    if not line:
                        break
                    text = line.decode('utf-8', 'replace').rstrip('\n')
                    if text.startswith('event: '):
                        name = text[7:]
                    elif text.startswith('data: ') and name == 'event':
                        events.append(json.loads(text[6:]))
        except Exception as e:  # noqa: BLE001
            print(f'  [sse] reader 结束: {e!r}', file=sys.stderr)

    t = threading.Thread(target=sse_reader, daemon=True)
    t.start()
    time.sleep(0.5)

    # --- 扫描预览 ---
    print('[*] 扫描预览')
    status, res = post(server, '/api/scan', {'directory': directory})
    check('启动扫描成功', res.get('ok') is True, str(res))

    deadline = time.time() + 60
    while time.time() < deadline:
        if any(e['kind'] == 'scan.finished' for e in events):
            break
        time.sleep(0.2)
    scan_progress = [e for e in events if e['kind'] == 'scan.progress']
    scan_finished = [e for e in events if e['kind'] == 'scan.finished']
    check('收到 scan.progress 事件', len(scan_progress) > 0, f'{len(scan_progress)} 条')
    check('收到 scan.finished 事件', len(scan_finished) == 1)
    if scan_finished:
        detail = scan_finished[0]['payload']
        check('扫描识别到影片', detail.get('movie_count', 0) > 0,
              f"movie_count={detail.get('movie_count')} movies={detail.get('movies')}")
    check('scan_only 不进入抓取',
          not any(e['kind'] == 'run.started' and e['payload'].get('mode') != 'scan_only'
                  for e in events))

    # --- 抓取 ---
    print('[*] 抓取')
    events.clear()
    status, res = post(server, '/api/start', {'directory': directory})
    check('启动抓取成功', res.get('ok') is True, str(res))

    deadline = time.time() + 240
    while time.time() < deadline:
        if any(e['kind'] == 'run.finished' for e in events):
            break
        time.sleep(0.2)

    kinds = [e['kind'] for e in events]
    check('收到 movie.started', 'movie.started' in kinds)
    check('收到 crawler.succeeded', 'crawler.succeeded' in kinds)
    check('收到 movie.step', 'movie.step' in kinds)
    check('收到 movie.finished', 'movie.finished' in kinds)
    check('收到 run.finished', 'run.finished' in kinds)

    finished = [e for e in events if e['kind'] == 'movie.finished']
    succeeded = [e for e in events if e['kind'] == 'crawler.succeeded']
    print(f'  [info] 完成 {len(finished)} 部，抓取成功事件 {len(succeeded)} 条')
    for e in succeeded[:4]:
        p = e['payload']
        print(f'  [info]   {p.get("movie_id")} <- {p.get("crawler")}: {(p.get("title") or "")[:28]}')

    # 同构性：GUI 事件与 worker 的 NDJSON 事件结构一致
    if events:
        sample = events[0]
        check('GUI 事件与 worker 事件同构',
              set(sample) == {'kind', 'ts', 'payload'}, str(sorted(sample)))

    # --- 忙碌保护 ---
    print('[*] 并发保护与停止')
    status, res = post(server, '/api/start', {'directory': directory})
    if res.get('ok') is True:
        # 抓取已结束，这里应能再次启动；随即停止它
        status, res2 = post(server, '/api/start', {'directory': directory})
        check('运行中再次启动被拒绝', res2.get('error') == 'busy', str(res2))
        status, res3 = post(server, '/api/stop', {})
        check('stop 成功', res3.get('ok') is True, str(res3))
        time.sleep(1.5)
        status, st = get(server, '/api/state', token=server.token)
        info = json.loads(st.decode('utf-8'))
        check('停止后无运行中任务', info['running'] is False, f"running={info['running']}")

    stop.set()
    server.shutdown()
    time.sleep(0.3)

    print()
    if failures:
        print(f'[!] {len(failures)} 项失败: {failures}', file=sys.stderr)
        return 1
    print('[+] GUI 后端检查全部通过')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
