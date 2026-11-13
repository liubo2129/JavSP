# 影片管理 + 播放功能：依赖调研

> 面向 JavSP 现有代码（`javsp/server.py` 本地 HTTP 层 + `javsp/webui/` 零构建前端 + pywebview 6.2 WKWebView + cx_Freeze 打包）。
> 本文的「实测」项都是在本机（macOS 27.0.1 / arm64 / Python 3.11.15）跑出来的，不是推测。

---

## 0. 一句话结论

两个功能都**不需要引入 web 框架**，也**不需要 QMediaPlayer / VLC**。
真正缺的只有三样：

| 需求 | 唯一真正缺的依赖 | 为什么 |
|---|---|---|
| 影片扫描 + NFO 元数据 | **无新增依赖** | `scan_movies()`、`read_nfo()`、`_collect_gallery()` 已经能用 |
| 影片库落库 | **sqlite3（标准库）** | 已在解释器里，`fts5` / `trigram` / WAL 实测全部可用 |
| 播放 | **PyAV（`av`，自带 FFmpeg 7 动态库）** | 本机没有 ffmpeg/mpv 可执行文件；PyAV wheel 自带全部解码库 |
| 播放（前提改动） | **HTTP Range/206 + video MIME** | 现在这两样都没有，是所有播放问题的根因 |

一句话：**核心只加 1 个 pip 依赖（`av`）+ 标准库 `sqlite3`，其余全是改现有 server 的代码。**

---

## 1. 现状盘点：已经有什么

| 能力 | 现有实现 | 位置 |
|---|---|---|
| 影片扫描（番号识别、分片合并、跳过已整理） | `scan_movies(root, sink, include_scraped)` | `javsp/file.py:52` |
| NFO 写入 | `write_nfo()`（Kodi 规范） | `javsp/nfo.py:12` |
| NFO 读取（返回 dict，含演员+头像） | `read_nfo()` | `javsp/nfo.py:119` |
| 海报 / fanart / 剧照归类 | `_collect_gallery()`（含 `extrafanart/`） | `javsp/server.py:477` |
| 本地图片接口 | `GET /api/image?path=&root=` | `javsp/server.py:249` |
| 远程图片代理（演员头像） | `_serve_remote_image()` | `javsp/server.py:283` |
| 影片详情接口 | `POST /api/movie_info` | `javsp/server.py:769` |
| 本地 HTTP + token 鉴权 + SSE | `GuiServer` / `_Handler` | `javsp/server.py:57`、`594` |
| 前端（零构建 HTML/CSS/JS） | `index.html` / `style.css` / `app.js` | `javsp/webui/` |
| 用户级 JSON 持久化 | `settings.py`（`~/Library/Application Support/JavSP/`） | `javsp/settings.py:69` |
| 挂载盘识别（Windows 映射盘） | `is_remote_drive()` | `javsp/file.py:326` |

**这些都不用重写。** 影片管理功能本质上是「把 `scan_movies` 的结果 + `read_nfo` 的结果落库，再加一页列表 UI」。

> 注意分工：`README.md` 里写着「UI 界面不是此项目的目标」，但当前分支 `feature/gui-macos` 已经有完整 GUI。
> 影片库/播放器属于 GUI 侧能力，应继续只落在 `javsp/webui/` + `javsp/server.py`，
> **不要碰 `javsp/__main__.py` 的 CLI 抓取管线**，否则会和上游冲突、也让 CLI 无谓变重。

---

## 2. 依赖清单

### 2.1 必须新增（1 个）

```toml
# pyproject.toml
av = "^16.0.1"     # PyAV：自带 FFmpeg 7.x 动态库，无需系统装 ffmpeg
```

**实测依据**（本机跑过）：

```
av 16.0.1
ffmpeg libs: libavutil 60.8.100 / libavcodec 62.11.100 / libavformat 62.3.100
             libavfilter 11.4.100 / libswscale 9.1.100 / libswresample 6.1.100
hwdevices: ['videotoolbox']        # 硬件解码可用
```

- **版本要钉 `16.0.1`，不能取最新**：`av>=19` 要求 Python ≥ 3.12，而本项目 `pyproject.toml` 写的是 `>=3.10,<3.13`，CI 用 3.10。
- `av 16.0.1` 的 macOS arm64 wheel 是 `macosx_14_0_arm64`（要求 macOS 14+）。若要照顾 macOS 13，用 `av 15.1.0`（`macosx_13_0`）。
- wheel 体积 21 MB，解包 54 MB（`.dylibs` 47 MB）。
- **cx_Freeze 实测可以打包**：我建了最小工程 `packages=['av']`，冻结后能正常 `import av` 并输出 libav 版本。副作用是 dylib 被复制两份（`lib/` 和 `lib/av/.dylibs/`），**删掉 `lib/av/.dylibs` 省 46 MB 且仍能运行**（实测通过）。
- 参考：[PyAV Installation](https://pyav.org/docs/stable/overview/installation.html) —— wheel 自带 FFmpeg，`pip install av` 不需要系统 ffmpeg。

### 2.2 必须新增（标准库，无需安装）

```python
import sqlite3     # WAL / FTS5 / trigram 都是内置
```

**实测依据**：

```
sqlite 3.53.1
unicode61: ok    trigram: ok    porter: ok
PRAGMA journal_mode=WAL -> wal
并发读（第二个连接）正常，提交前看不到未提交数据
trigram 匹配 "深田えいみ" / "単体作品" 命中；"巨田" / 两字词不命中（需 LIKE 兜底）
```

### 2.3 强烈建议新增（工程量很小，收益大）

```toml
watchdog = "^6.0.0"    # 挂载目录增量同步；SMB/NFS 上必须用 PollingObserver
```

- ⚠️ `watchdog 7.x` 要求 Python ≥ 3.11，**与本项目 3.10 下限冲突**，钉 `^6`。
- 但**挂载盘上 FSEvents 不可靠**，SMB/NFS 必须退回 `PollingObserver`（[watchdog 网络盘说明](https://codemia.io/knowledge-hub/path/how_do_i_watch_a_file_for_changes)）。
- 实测可用 `statfs` 的 `f_fstypename` 判定挂载类型，据此自动选 Observer：

```python
import ctypes, ctypes.util, os
class Fsid(ctypes.Structure): _fields_=[('val', ctypes.c_int32*2)]
class Statfs(ctypes.Structure):
    _fields_=[('f_bsize',ctypes.c_uint32),('f_iosize',ctypes.c_int32),
              ('f_blocks',ctypes.c_uint64),('f_bfree',ctypes.c_uint64),
              ('f_bavail',ctypes.c_uint64),('f_files',ctypes.c_uint64),
              ('f_ffree',ctypes.c_uint64),('f_fsid',Fsid),
              ('f_owner',ctypes.c_uint32),('f_type',ctypes.c_uint32),
              ('f_flags',ctypes.c_uint32),('f_fssubtype',ctypes.c_uint32),
              ('f_fstypename',ctypes.c_char*16),('f_mntonname',ctypes.c_char*1024),
              ('f_mntfromname',ctypes.c_char*1024)]
NETWORK = {b'smbfs', b'nfs', b'afpfs', b'webdav', b'osxfuse'}
def is_network_mount(path):
    buf = Statfs()
    if ctypes.CDLL(ctypes.util.find_library('c')).statfs(os.fsencode(path), ctypes.byref(buf)) != 0:
        return False
    return buf.f_fstypename in NETWORK
# 实测：/tmp -> ('apfs', False)
```

**替代方案**：不装 watchdog，直接「打开软件时扫一遍 + 手动刷新 + 定时轮询」，对挂载盘反而更稳。**建议先这样，watchdog 留到有性能问题时再加。**

### 2.4 明确不需要的

| 候选 | 结论 | 理由 |
|---|---|---|
| Flask / FastAPI / aiohttp | **不要** | 现有 `ThreadingHTTPServer` 够用；`server.py` 头注释写明「只用标准库，冻结产物里也没有 wsgiref」 |
| SQLAlchemy / peewee / SQLModel | **不要** | 表很少（见 §3），`sqlite3` 直接写更省事，也少一个打包风险 |
| ffmpeg / ffprobe 可执行文件 | **不要** | 本机没有；PyAV wheel 已自带，且能直接做探测/截图/remux |
| mpv / python-mpv / libmpv | **不要** | 需要用户装 mpv；license 是 GPLv2+；与 pywebview 的 DOM 叠层困难 |
| VLC / python-vlc | **不要** | 本机装了 VLC.app 但**不能用**：它没有 python binding，且无法嵌进 WKWebView |
| QMediaPlayer / PySide6 | **不要** | 等于换掉整个 UI 栈，143 MB 的冻结产物再翻倍 |
| pymediainfo | **可选** | 若完全不想碰 decoders 只想读元数据，它更轻（MIT）。但截图/remux 仍要 PyAV，功能重叠 |
| mutagen | **不要** | 只处理音频标签，不解析视频容器 |
| imageio-ffmpeg / static-ffmpeg | **不要** | 它们下的是 ffmpeg **可执行文件**，仍需 subprocess 且要处理 code signing |

---

## 3. 数据库设计（sqlite3）

放在 `~/Library/Application Support/JavSP/library.db`（与现有 `settings.json` 同目录，`settings.py:69` 已有该约定）。

```sql
PRAGMA journal_mode = WAL;      -- 读写并发；实测通过
PRAGMA synchronous  = NORMAL;
PRAGMA foreign_keys = ON;

-- 影片库（一个挂载根目录一条）
CREATE TABLE library (
    id          INTEGER PRIMARY KEY,
    root_path   TEXT NOT NULL UNIQUE,     -- /Volumes/xxx 或 SMB 挂载点
    display_name TEXT,
    fs_type     TEXT,                     -- apfs / smbfs / nfs，决定扫描策略
    is_network  INTEGER DEFAULT 0,
    created_at  TEXT, last_scan_at TEXT
);

-- 影片
CREATE TABLE movie (
    id           INTEGER PRIMARY KEY,
    library_id   INTEGER REFERENCES library(id) ON DELETE CASCADE,
    avid         TEXT,                    -- 番号（ABP-647 / FC2-123）
    title        TEXT,
    original_title TEXT,
    plot         TEXT,
    rating       TEXT,
    runtime      INTEGER,                 -- 秒（nfo 里是分钟，注意换算）
    premiered    TEXT,
    studio       TEXT,
    director     TEXT,
    series       TEXT,
    mpaa         TEXT,
    duration     REAL,                    -- 实际媒体时长（PyAV 探测）
    width INTEGER, height INTEGER,
    video_codec  TEXT, audio_codec TEXT,  -- 决定能不能直出
    container    TEXT,                    -- mp4 / mkv / avi ...
    bitrate      INTEGER,
    video_path   TEXT NOT NULL,           -- 绝对路径（挂载点变化时要能重定位）
    relative_path TEXT,                   -- 相对 library.root_path，换挂载点靠它找回
    size         INTEGER,
    mtime        REAL,
    inode        INTEGER,                 -- 快速判重/判变更
    nfo_path     TEXT, nfo_mtime REAL,
    poster_path  TEXT, fanart_path TEXT,
    added_at     TEXT, updated_at TEXT
);
CREATE UNIQUE INDEX idx_movie_lib_relpath ON movie(library_id, relative_path);
CREATE INDEX idx_movie_avid ON movie(avid);
CREATE INDEX idx_movie_path ON movie(video_path);

-- 多值字段走关联表（不做逗号拼接，否则筛选/统计会痛）
CREATE TABLE genre   (id INTEGER PRIMARY KEY, name TEXT UNIQUE);
CREATE TABLE movie_genre (movie_id INTEGER, genre_id INTEGER, PRIMARY KEY(movie_id, genre_id));

CREATE TABLE actress (id INTEGER PRIMARY KEY, name TEXT UNIQUE,
                      thumb_path TEXT,     -- 头像落本地，别依赖外链
                      alias_of INTEGER REFERENCES actress(id));
CREATE TABLE movie_actress (movie_id INTEGER, actress_id INTEGER,
                            PRIMARY KEY(movie_id, actress_id));

-- 剧照
CREATE TABLE still (id INTEGER PRIMARY KEY,
                    movie_id INTEGER REFERENCES movie(id) ON DELETE CASCADE,
                    path TEXT, ordinal INTEGER, width INTEGER, height INTEGER);

-- 播放进度（播放功能的"记住看到哪"）
CREATE TABLE progress (movie_id INTEGER PRIMARY KEY,
                       position REAL, duration REAL,
                       finished INTEGER DEFAULT 0, updated_at TEXT);

-- 中文搜索：trigram 支持 3 字以上子串，2 字/1 字退回 LIKE
CREATE VIRTUAL TABLE movie_fts USING fts5(
    title, original_title, avid, plot,
    content='movie', content_rowid='id', tokenize='trigram'
);
```

**踩坑提醒（实测过）**：
- trigram 分词器**少于 3 个字符查不到**（`巨乳`、`深田` 都返回空）。UI 里要么强制 ≥3 字，要么 `q LIKE '%x%'` 兜底。
- `content='movie'` 是外部内容表，`movie` 更新后必须手动同步 `movie_fts`（触发器或写库时一并写）。

---

## 4. 播放：为什么必须加 Range，实测证据

这是本次调研最关键的发现。**当前 `javsp/server.py` 的 `_send_file()` 一次性 `path.read_bytes()` 然后 `200 + Content-Length`，完全不处理 `Range` 头，也没有 video 的 MIME 类型。**

### 4.1 复现

对现有接口发请求（`server.py:131`）：

```
=== 带 Range: bytes=0-1（Safari/WKWebView 的起手式）===
HTTP/1.1 200 OK
Content-Type: application/octet-stream      ← 既不是 video/mp4，也没有 206
Content-Length: 6478051
```

`_content_type()` 对 `.mp4/.mkv/.webm/.m4v/.mov/.ts` 全部返回 `application/octet-stream`（实测）。

### 4.2 但 mp4 居然能播——直到你拖动进度条

WKWebView 实测，**同一个 913 MB / 60 秒 / 720p 的 mp4**，只改服务器是否支持 Range，
本地限速 60 MB/s（模拟千兆 NAS 的写入速度）：

| | 起播 | 拖动到 30s | 实际传输 |
|---|---|---|---|
| **无 Range**（= 现状） | ❌ 连 `loadedmetadata` 都没触发 | ⏱ **90 秒超时，进度条卡死** | **693 MB**（还在继续灌） |
| **有 Range（206）** | ✅ `readyState=4` | ✅ **1 秒完成** | **33 MB / 1 个请求** |

> 说明：小文件（6.5 MB）因为很快被浏览器一次性缓冲完，无 Range 也能「假装」拖动成功——
> 这也是为什么这个问题在小样本上测不出来、一上真片源就暴露。

结论：**同一个文件、同一个播放器，只差一个 Range 实现，拖动体验是「卡死」和「秒到」的区别。**
无 Range 时浏览器只能从头顺序下载到目标位置，每次拖动 = 重新下载一遍整个文件。

### 4.3 顺带实测出来的其它结论

| 结论 | 实测数据 |
|---|---|
| **MKV / AVI 无法直出** | `<video>` 加载 mkv → `NotSupportedError`，`readyState=0`。WKWebView 只认 mp4/webm 容器 |
| **HEVC 可以播** | `canPlayType('video/mp4; codecs="hvc1..."') === "probably"`；带 `hvc1` tag 的 HEVC mp4 起播成功 |
| **fMP4（fragmented MP4）可播可拖** | PyAV 用 `movflags=frag_keyframe+empty_moov+default_base_moof` 输出的流式 mp4，WKWebView 正常起播 |
| **playbackRate 完全可用** | 0.1 / 0.25 / 0.5 / 0.75 / 1.25 / 1.5 / 2 / 3 **全部精确生效**，`preservesPitch=true`（变速不变调） |
| **从中间偏移 remux 的流可播** | 从 30s 处 remux 出的 fMP4（时长 30s），WKWebView 正常起播、可拖 |
| **moov 位置不影响本地服务** | 本地 http server 下 moov 在文件尾也能起播（不像网络 CDN 那样卡住） |

### 4.4 播放架构（推荐）

```
┌─ 前端 <video src="/api/stream?path=...">  ─────┐
│                                                │
├─ 原生直出（快路径）                             │
│   mp4 + H.264/AAC（或 hvc1 HEVC）→ 直接 Range 吐字节
│   起播 <100ms，CPU 0%，可任意拖拽                │
│                                                │
└─ PyAV remux（慢路径）                           │
    mkv / avi / 非常规编码 → 零解码 remux 成 fMP4
    `-movflags frag_keyframe+empty_moov+default_base_moof`
    CPU 接近 0（只拆装容器，不重编码），实测 3 秒片源 0.00s 完成
    │
    └─ 若编码也不兼容（如 VC-1/WMV）→ 真转码 libx264 ultrafast
        仅在必要时兜底，GPU 可用 videotoolbox
```

**PyAV 的关键 API（都实测通过）：**

```python
import av

# ① 探测元数据（替代 ffprobe）—— 实测 0.001s
with av.open(path) as ct:
    v = ct.streams.video[0]
    duration = float(ct.duration / av.time_base)
    codec, w, h = v.codec_context.name, v.codec_context.width, v.codec_context.height
    pix_fmt, fps = v.codec_context.pix_fmt, float(v.average_rate)

# ② 定时长截图（替代 ffmpeg -ss ... -frames:v 1）—— 实测 3 张 0.04s
for frac in (0.1, 0.5, 0.9):
    ct.seek(int(ct.duration * frac), stream=ct.streams.video[0])
    for frame in ct.decode(video=0):
        img = frame.to_image(); img.thumbnail((480, 480)); img.save(...)
        break

# ③ 零解码 remux 成流式 mp4（给不支持 mkv 的 WKWebView）
inp = av.open(src)
out = av.open(pipe_or_http_writer, 'w', format='mp4',
              options={'movflags': 'frag_keyframe+empty_moov+default_base_moof',
                       'frag_duration': '300000'})
mapping = {s: out.add_stream_from_template(s) for s in inp.streams}
for pkt in inp.demux():
    if pkt.dts is None:        # flush 包必须跳过
        continue
    pkt.stream = mapping[pkt.stream]
    out.mux(pkt)
out.close()
```

第 ③ 段在**不可 seek 的输出**（模拟管道）上也测通了——这是能边转边发的前提。

---

## 5. 需要改的现有代码（按工作量排序）

### P0 —— 不做就没法播

1. **`_content_type()` 补视频 MIME**（`server.py:345`）
   ```python
   '.mp4': 'video/mp4', '.m4v': 'video/mp4', '.webm': 'video/webm',
   '.mkv': 'video/x-matroska', '.mov': 'video/quicktime',
   ```

2. **`_send_file()` 支持 Range**（`server.py:131`，当前是 `path.read_bytes()`）
   - 解析 `Range: bytes=a-b`、`bytes=a-`、`bytes=-n`
   - 返回 `206` + `Content-Range` + `Accept-Ranges: bytes`
   - 用 `shutil.copyfileobj` 分块吐，**绝不能整个读进内存**（4K 片源 10 GB+）
   - 非法范围返回 `416` + `Content-Range: bytes */<size>`

3. **新增 `GET /api/stream?path=&root=`**
   - 复用现有 `_is_within()` 做路径沙箱（`server.py:579`）
   - 按 §4.4 分流：直出 / remux
   - 支持 `?start=<sec>` 从指定位置起播（配合前端「恢复进度」）

### P1 —— 影片管理

4. **新增 `javsp/library.py`**：建库、迁移（`PRAGMA user_version`）、扫描入库、查询
5. **扫描进度接现有 SSE**：`scan_movies(sink=...)` 已在发 `EventKind.SCAN_PROGRESS`，直接复用事件流推给前端
6. **新增 API**：`GET /api/library/movies?q=&sort=&page=`、`GET /api/library/movie/<id>`、`POST /api/library/scan`、`POST /api/library/roots`
7. **前端加影片库视图**（`index.html` 现在只有「任务 / 扫描预览」两块，需要加一个 grid 列表 + 详情抽屉）
   - 海报墙用 CSS Grid + `aspect-ratio: 2/3`
   - 图片走现有 `/api/image?path=`，**天然支持懒加载**（`loading="lazy"`）
   - 大图预览直接复用现有 `openLightbox()`（`app.js:1534`）

### P2 —— 打磨

8. 缩略图缓存（首次扫描时用 PyAV 生成 320px webp，避免每次列表都读原图）
9. 演员头像本地化（`read_nfo` 里 `actor/thumb` 是外链，现在靠 `/api/image?remote=` 实时代理，离线就没了）
10. 播放进度落 `progress` 表 + 前端 `timeupdate` 节流上报（5 秒一次足够）

---

## 6. 几个必须提前决定的坑

| 坑 | 现状 | 建议 |
|---|---|---|
| **挂载点是 SMB/NFS** | `is_remote_drive()` 只认 Windows 映射盘，macOS 恒返回 `False`（`file.py:326`） | 用 §2.3 的 `statfs` 判定；网络盘上关掉 FSEvents，改轮询 |
| **挂载点变化** | 绝对路径会失效 | 库里同时存 `relative_path`，挂载点变了用相对路径重定位 |
| **nfo 的 `runtime` 是分钟** | `read_nfo()` 返回 `int`（分钟） | 入库时统一成秒，否则进度条 / 剩余时间会错 |
| **演员信息只有 name + 头像** | 爬虫不抓身高/三围/生日（实测 `javsp/web/*.py` 里没有这些字段） | 「演员信息」页面就展示名字 + 头像 + 参演影片列表即可，别承诺更多 |
| **打包体积** | 现在 `JavSP.app` 171 MB（`Resources/lib` 143 MB） | 最小工程实测：基线 33 MB → 加 av 135 MB → **删掉重复 dylib 89 MB**，即 av 净增 ≈ 56 MB。预计 `JavSP.app` ≈ 230 MB（不删重复则 ≈ 270 MB） |
| **code signing** | cx_Freeze 默认 ad-hoc | PyAV 的 46 MB dylib 会拖慢签名；如需公证要先测 `codesign --deep` 耗时 |
| **Docker 镜像** | `python:3.12-slim` | PyAV 在 linux 有 manylinux wheel，但体积涨 50 MB；建议 Dockerfile 里 `exclude` 播放相关模块 |
| **license** | 项目 GPL-3.0 | PyAV 是 BSD-3；但它捆绑的 libx264/libx265 是 GPL —— **本项目已是 GPL-3，兼容，无额外义务** |

---

## 7. 最小可行落地顺序

```
第 1 步（1 天）：Range + video MIME + /api/stream 直出
                  → mp4 能播、能拖、能快进，进度条体验立刻可用
第 2 步（1 天）：装 av，加 /api/stream 的 remux 分支
                  → mkv 也能播了（零重编码，CPU 接近 0）
第 3 步（2 天）：library.py + 建表 + 扫描入库 + 列表 API
第 4 步（2 天）：前端影片墙 + 详情页（海报/大图/剧照/演员）
第 5 步（1 天）：播放页 + playbackRate + 进度记忆
第 6 步（1 天）：缩略图缓存 + 演员头像本地化
```

**第 1 步不依赖任何新依赖**，纯改 `server.py`，建议先做，能立刻验证整条播放链路。

---

## 8. 参考

- [PyAV 安装说明（wheel 自带 FFmpeg）](https://pyav.org/docs/stable/overview/installation.html)
- [Safari / WKWebView 必须有 206 才能播放](https://stackoverflow.com/questions/32996396/safari-9-0-can-not-play-mp4-video-on-the-storage-server)
- [206 Partial Content 与 Range 请求](https://howhttpworks.com/status-codes/206)
- [Browsers 不支持 MKV 容器](http://sureshot.video/blog/best-video-format-for-web)
- [Safari 17 的 HEVC / AV1 支持](https://webkit.org/blog/14445/webkit-features-in-safari-17-0/)
- [HTMLMediaElement.playbackRate（MDN）](https://developer.mozilla.org/en-US/docs/web/api/htmlmediaelement/playbackrate)
- [watchdog 在网络盘上要改用 PollingObserver](https://codemia.io/knowledge-hub/path/how_do_i_watch_a_file_for_changes)
- [SQLite FTS5 trigram 分词器做中文子串检索](https://www.ipipp.com/html/20260910/53791.html)
