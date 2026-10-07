'use strict';
/**
 * JavSP 界面逻辑。
 *
 * 数据来源只有两个：
 *   1. /api/*        —— 用户动作（选目录、扫描、开始、停止）
 *   2. /api/events   —— SSE 事件流，与 worker 的 NDJSON 事件同构
 *
 * 零构建：不使用框架与打包器（CI 与 Dockerfile 都没有 Node）。
 *
 * 任务状态是**派生**的（scanRunning / scrapeRunning / terminal），不是直接
 * 存一个字段：页面刷新要回放历史事件重建状态，派生值不会因重复回放而错乱。
 */
(() => {
  const TOKEN = new URLSearchParams(location.search).get('token') || '';
  const LOG_LIMIT = 300;
  const RECENT_KEY = 'javsp.recent';
  const RECENT_MAX = 6;

  // 与 javsp/__main__.py 的 _planned_steps() 对应
  const STEP_LABELS = {
    crawl: '抓取站点',
    summarize: '汇总数据',
    translate: '翻译',
    generate_names: '生成文件名',
    download_cover: '下载封面',
    process_poster: '生成海报',
    extrafanart: '下载剧照',
    write_nfo: '写入 NFO',
    move_files: '移动文件',
  };

  const state = {
    // 'dir' = 按目录获取；'id' = 按番号获取
    mode: 'dir',
    directory: '',
    targetFolder: '',
    idsText: '',
    scanRunning: false,
    scrapeRunning: false,
    terminal: null,          // null | done | failed | stopped
    total: 0,
    finished: 0,
    failed: 0,
    movies: new Map(),       // id -> movie（侧边栏实时进度）
    // 按番号获取的列表项：{id, path, scraped}。path 在抓取完成后才有，
    // 有点击路径才能打开信息气泡。顺序即用户输入的番号顺序。
    idItems: [],
    idFolder: '',
    scan: {
      active: false,
      phase: '',
      currentDir: '',
      fileCount: 0,
      videoCount: 0,
      movies: [],            // scan.finished 的结构化结果
      unrecognized: [],
      summary: '',
      done: false,
    },
    logs: [],
    startedAt: null,
    endedAt: null,
    timer: null,
  };

  const $ = (id) => document.getElementById(id);
  const el = {
    conn: $('conn'), jobPill: $('job-pill'), jobHint: $('job-hint'),
    bar: $('bar'), barLabel: $('bar-label'),
    mTotal: $('m-total'), mDone: $('m-done'), mFail: $('m-fail'), mTime: $('m-time'),
    movies: $('movies'), movieCount: $('movie-count'),
    logs: $('logs'), clearLog: $('btn-clear-log'),
    dirInput: $('dir-input'), browse: $('btn-browse'), scan: $('btn-scan'),
    scanHint: $('scan-hint'), start: $('btn-start'), stop: $('btn-stop'),
    recent: $('recent'), recentItems: $('recent-items'),
    previewSummary: $('preview-summary'), previewChips: $('preview-chips'),
    previewBody: $('preview-body'), toasts: $('toasts'),
    popover: $('movie-popover'), popoverArrow: $('popover-arrow'),
    popoverClose: $('popover-close'), popoverContent: $('popover-content'),
    tabDir: $('tab-dir'), tabId: $('tab-id'),
    paneDir: $('pane-dir'), paneId: $('pane-id'),
    pageTitle: $('page-title'), pageSub: $('page-sub'),
    startLabel: $('btn-start-label'), previewTitle: $('preview-title'),
    targetInput: $('target-input'), browseTarget: $('btn-browse-target'),
    idsInput: $('ids-input'), idHint: $('id-hint'),
    lightbox: $('lightbox'), lbImg: $('lb-img'), lbClose: $('lb-close'),
    lbPrev: $('lb-prev'), lbNext: $('lb-next'), lbCounter: $('lb-counter'),
  };

  /* --------------------------- 模式切换 --------------------------- */
  function setMode(mode) {
    if (state.mode === mode) return;
    state.mode = mode;
    const isDir = mode === 'dir';
    el.tabDir.classList.toggle('active', isDir);
    el.tabId.classList.toggle('active', !isDir);
    el.tabDir.setAttribute('aria-selected', String(isDir));
    el.tabId.setAttribute('aria-selected', String(!isDir));
    el.paneDir.classList.toggle('hidden', !isDir);
    el.paneId.classList.toggle('hidden', isDir);

    el.pageTitle.textContent = isDir ? '按目录获取' : '按番号获取';
    el.pageSub.textContent = isDir
      ? '选择要刮削的文件夹，先预览识别结果再开始'
      : '按番号直接抓取元数据，输出为 <目标文件夹>/<番号>/';
    el.startLabel.textContent = isDir ? '开始整理' : '开始获取';
    el.previewTitle.textContent = isDir ? '扫描预览' : '获取结果';
    closePopover();
    schedule();
  }

  /* ------------------------------------------------------------------ *
   * 渲染调度：一帧内的多次更新合并成一次 DOM 写入
   * ------------------------------------------------------------------ */
  let scheduled = false;
  function schedule() {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => { scheduled = false; render(); });
  }

  function render() {
    renderJob();
    renderMovies();
    renderPreview();
    renderRecent();
  }

  const JOB_LABEL = {
    idle: '空闲', scan: '扫描中', scrape: '抓取中',
    done: '已完成', failed: '有失败', stopped: '已停止',
  };
  function jobState() {
    if (state.scanRunning) return 'scan';
    if (state.scrapeRunning) return 'scrape';
    if (state.terminal) return state.terminal;
    return 'idle';
  }

  function renderJob() {
    const job = jobState();
    const running = job === 'scan' || job === 'scrape';

    el.jobPill.textContent = JOB_LABEL[job] || job;
    el.jobPill.className = 'pill ' + (
      running ? 'running' : job === 'done' ? 'done'
        : (job === 'failed' || job === 'stopped') ? 'failed' : 'idle');

    // 进度
    const total = state.total || 0;
    const pct = total > 0 ? Math.min(100, Math.round((state.finished / total) * 100)) : 0;
    el.bar.style.width = pct + '%';
    el.barLabel.textContent = `${state.finished} / ${total}`;

    // 提示行
    if (job === 'scan') {
      el.jobHint.textContent = state.scan.currentDir
        ? '正在扫描 ' + shortPath(state.scan.currentDir)
        : '正在扫描…';
    } else if (job === 'scrape') {
      const active = [...state.movies.values()].find((m) => m.status === 'active');
      el.jobHint.textContent = active
        ? `正在处理 ${active.id}${active.step ? ' · ' + (STEP_LABELS[active.step] || active.step) : ''}`
        : '正在处理…';
    } else if (job === 'done') {
      el.jobHint.textContent = `全部完成，共 ${state.finished} 部`;
    } else if (job === 'failed') {
      el.jobHint.textContent = `${state.failed} 部失败`;
    } else if (job === 'stopped') {
      el.jobHint.textContent = '任务已停止';
    } else if (state.mode === 'id') {
      const n = parseIds(state.idsText).length;
      el.jobHint.textContent = n ? `已填写 ${n} 个番号，可开始获取` : '输入番号后开始';
    } else if (state.directory) {
      el.jobHint.textContent = '已就绪，可开始整理';
    } else {
      el.jobHint.textContent = '选择影片目录后开始';
    }

    el.mTotal.textContent = total;
    el.mDone.textContent = state.finished;
    el.mFail.textContent = state.failed;
    el.mTime.textContent = elapsed();

    // 控件可用性：按目录模式要求目录，按番号模式要求至少一个番号
    const ready = state.mode === 'dir'
      ? !!state.directory
      : parseIds(state.idsText).length > 0;
    el.start.disabled = running || !ready;
    el.stop.disabled = !running;
    el.scan.disabled = running || !state.directory;
    el.browse.disabled = running;
    el.dirInput.disabled = running;
    el.browseTarget.disabled = running;
    el.idsInput.disabled = running;
    el.targetInput.disabled = running;
    el.tabDir.disabled = running;
    el.tabId.disabled = running;

    el.movieCount.textContent = state.movies.size ? `${state.movies.size} 部` : '';
  }

  function elapsed() {
    if (!state.startedAt) return '—';
    const running = state.scanRunning || state.scrapeRunning;
    const end = running ? Date.now() : (state.endedAt || Date.now());
    const sec = Math.max(0, Math.round((end - state.startedAt) / 1000));
    const m = Math.floor(sec / 60), s = sec % 60;
    return m > 0 ? `${m}:${String(s).padStart(2, '0')}` : `${s}s`;
  }

  /* ---------------------------- 影片列表 ---------------------------- */
  function renderMovies() {
    if (state.movies.size === 0) {
      el.movies.innerHTML =
        '<li class="empty"><span>尚无进行中的任务</span>' +
        '<span>开始抓取后这里会显示每一部的实时进度</span></li>';
      return;
    }
    const parts = [];
    for (const m of state.movies.values()) {
      const cls = m.status === 'active' ? 'active'
        : m.status === 'done' ? 'done'
          : m.status === 'failed' ? 'failed' : '';

      const stepText = m.step
        ? `${STEP_LABELS[m.step] || m.step}${m.stepIndex ? ` ${m.stepIndex}/${m.stepTotal}` : ''}`
        : '';
      const sites = [...m.sites.entries()]
        .map(([n, s]) => `<span class="site ${s}">${esc(n)}</span>`).join('');

      parts.push(`<li class="movie ${cls}">
        <div class="movie-top">
          <span class="movie-id">${esc(m.id)}</span>
          <span class="movie-step">${esc(stepText)}</span>
        </div>
        ${m.title ? `<div class="movie-title" title="${esc(m.title)}">${esc(m.title)}</div>` : ''}
        ${m.error ? `<div class="movie-error"><b>失败</b><span>${esc(m.error)}</span></div>` : ''}
        ${sites ? `<div class="sites">${sites}</div>` : ''}
      </li>`);
    }
    el.movies.innerHTML = parts.join('');
  }

  /* ---------------------------- 扫描预览 ---------------------------- */
  function renderPreview() {
    const s = state.scan;
    // 按番号模式的数据源是 state.idItems（没有目录扫描的概念）
    const byId = state.mode === 'id';
    const hasData = byId
      ? state.idItems.length > 0
      : (s.active || s.movies.length > 0 || s.unrecognized.length > 0 || s.done);

    // 摘要 + 统计 chip
    if (!hasData) {
      el.previewSummary.textContent = byId ? '尚未开始' : '尚未扫描';
      el.previewChips.innerHTML = '';
    } else if (byId) {
      const done = state.idItems.filter((it) => it.scraped).length;
      el.previewSummary.textContent = state.idFolder
        ? `输出到 ${state.idFolder}`
        : `${state.idItems.length} 个番号`;
      const chips = [];
      chips.push(`<span class="chip ok">${state.idItems.length} 个番号</span>`);
      if (done) chips.push(`<span class="chip ok">已完成 ${done}</span>`);
      el.previewChips.innerHTML = chips.join('');
    } else {
      el.previewSummary.textContent = s.summary
        || (s.active ? '正在扫描…' : `识别到 ${s.movies.length} 部影片`);
      const chips = [];
      if (s.movies.length) chips.push(`<span class="chip ok">${s.movies.length} 部影片</span>`);
      if (s.videoCount) chips.push(`<span class="chip">${s.videoCount} 个视频</span>`);
      // 这里是"遍历到的全部文件"，包含 nfo/海报等附属文件，故与视频数分开讲清楚
      if (s.fileCount > s.videoCount) chips.push(`<span class="chip">扫描 ${s.fileCount} 个文件</span>`);
      if (s.unrecognized.length) {
        chips.push(`<span class="chip warn">${s.unrecognized.length} 需人工</span>`);
      }
      el.previewChips.innerHTML = chips.join('');
    }

    if (!hasData) {
      el.previewBody.innerHTML = `
        <div class="empty-state">
          <div class="empty-icon">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"
                 stroke-linecap="round" stroke-linejoin="round">
              <rect x="3" y="4" width="18" height="16" rx="2.5"/><path d="M3 9h18"/>
              <circle cx="12" cy="14.5" r="2.5"/>
            </svg>
          </div>
          <h4>${byId ? '还没有获取结果' : '还没有扫描结果'}</h4>
          <p>${byId
          ? '填入番号并点「开始获取」，这里会按输入顺序列出每个番号；获取完成后点击可查看影片信息。'
          : '先选择影片目录并点「扫描预览」，这里会列出识别到的番号、文件明细，以及需要人工处理的文件。'}</p>
        </div>`;
      return;
    }

    // 按番号模式：列表即用户输入的番号，产出后点击行为与按目录模式一致
    if (byId) {
      const rows = state.idItems.map((it, i) => {
        const m = state.movies.get(it.id);
        const status = !it.path
          ? (m ? (m.status === 'failed' ? '失败' : '进行中') : '待获取')
          : '已完成';
        const cls = it.path ? '' : (m && m.status === 'failed' ? ' class="row-failed"' : '');
        return `<tr${cls} data-idx="${i}" data-path="${esc(it.path)}"
            title="${it.path ? '点击查看影片信息' : '尚未获取完成'}">
          <td class="num">${esc(it.id)}${it.scraped
          ? '<span class="scraped-dot" title="已获取"></span>' : ''}</td>
          <td class="files">${esc(it.path ? it.path.split('/').slice(-2).join('/') : '—')}</td>
          <td class="meta">${esc(status)}</td>
        </tr>`;
      }).join('');
      el.previewBody.innerHTML = `<table class="movie-table">
        <thead><tr><th>番号</th><th>输出</th><th>状态</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
      return;
    }

    const out = [];

    if (s.active && s.movies.length === 0) {
      out.push(`<div class="scanning">
        <div class="spinner"></div>
        <div class="scanning-text">
          <b>正在扫描文件…</b>
          <span>${esc(s.currentDir || '准备中')}</span>
        </div>
      </div>`);
    }

    if (s.movies.length) {
      const rows = s.movies.map((m, i) => {
        const path = (m.paths && m.paths[0]) || '';
        return `<tr data-idx="${i}" data-path="${esc(path)}" title="点击查看影片信息">
        <td class="num">${esc(m.id)}${m.scraped
          ? '<span class="scraped-dot" title="已整理（有 NFO）"></span>' : ''
        }${m.data_src && m.data_src !== 'normal'
          ? `<span class="badge-src">${esc(m.data_src)}</span>` : ''}</td>
        <td class="files">${esc((m.files || []).join('  ·  '))}</td>
        <td class="meta">${m.file_count > 1 ? m.file_count + ' 个分片' : ''}</td>
      </tr>`;
      }).join('');
      out.push(`<table class="movie-table">
        <thead><tr><th>番号</th><th>文件</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`);
    }

    if (s.unrecognized.length) {
      out.push(`<div class="issue-head">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
             stroke-linecap="round"><path d="M12 8v5M12 16.5v.01"/>
             <circle cx="12" cy="12" r="9"/></svg>
        <h4>无法识别番号 · 需人工处理</h4>
        <div class="line"></div>
      </div>
      <ul class="issue-list">${s.unrecognized.map((p) =>
        `<li><span>${esc(shortPath(p))}</span></li>`).join('')}</ul>`);
    }

    el.previewBody.innerHTML = out.join('');
  }

  /* ---------------------------- 最近目录 ---------------------------- */
  function loadRecent() {
    try { return JSON.parse(localStorage.getItem(RECENT_KEY) || '[]'); } catch { return []; }
  }
  function pushRecent(dir) {
    if (!dir) return;
    const list = loadRecent().filter((d) => d !== dir);
    list.unshift(dir);
    try { localStorage.setItem(RECENT_KEY, JSON.stringify(list.slice(0, RECENT_MAX))); } catch {}
    schedule();
  }
  function renderRecent() {
    const list = loadRecent();
    if (!list.length) { el.recent.classList.add('hidden'); return; }
    el.recent.classList.remove('hidden');
    el.recentItems.innerHTML = list.map((d) =>
      `<button type="button" class="recent-item" data-dir="${esc(d)}" title="${esc(d)}">${esc(d)}</button>`
    ).join('');
  }
  el.recentItems.addEventListener('click', (e) => {
    const btn = e.target.closest('.recent-item');
    if (!btn) return;
    setDirectory(btn.dataset.dir);
  });

  /* ------------------------------ 日志 ------------------------------ */
  function pushLog(text, cls) {
    const now = new Date();
    const t = `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}:${String(now.getSeconds()).padStart(2, '0')}`;
    state.logs.push({ t, text, cls });
    if (state.logs.length > LOG_LIMIT) state.logs.shift();

    const li = document.createElement('li');
    if (cls) li.className = cls;
    li.innerHTML = `<time>${t}</time><span></span>`;
    li.querySelector('span').textContent = text;
    el.logs.appendChild(li);
    while (el.logs.childElementCount > LOG_LIMIT) el.logs.removeChild(el.logs.firstChild);
    el.logs.scrollTop = el.logs.scrollHeight;
  }

  function toast(text, cls = '') {
    const div = document.createElement('div');
    div.className = 'toast ' + cls;
    div.textContent = text;
    el.toasts.appendChild(div);
    setTimeout(() => div.remove(), 4200);
  }

  /* ---------------------------- 事件处理 ---------------------------- */
  function handleEvent(evt) {
    const p = evt.payload || {};
    switch (evt.kind) {
      case 'scan.started':
        state.scanRunning = true;
        state.terminal = null;
        // 只重置预览区（scan.*）。侧边栏的抓取列表另有 state.movies，
        // 不能在这里清空——否则扫描阶段会把侧边栏的进度抹掉。
        state.scan = {
          active: true, phase: 'walking', currentDir: p.root || '',
          fileCount: 0, videoCount: 0, movies: [], unrecognized: [],
          summary: '', done: false,
        };
        state.startedAt = Date.now();
        startTimer();
        pushLog('开始扫描: ' + (p.root || ''));
        break;

      case 'scan.progress':
        handleScanProgress(p);
        break;

      case 'scan.finished': {
        state.scan.active = false;
        state.scan.done = true;
        // worker 现在发结构化 movie 列表；兼容旧的字符串形式
        const raw = p.movies || [];
        state.scan.movies = raw.map((m) =>
          typeof m === 'string'
            ? { id: String(m).replace(/^Movie\('(.*)'\)$/, '$1'), files: [], file_count: 1 }
            : m);
        state.total = p.movie_count || state.scan.movies.length;
        state.scan.summary = `识别到 ${state.total} 部影片`;
        pushLog(`扫描完成：${state.total} 部影片` +
          (state.scan.unrecognized.length ? `，${state.scan.unrecognized.length} 个文件无法识别` : ''),
          'ok');
        break;
      }

      case 'run.started':
        if (p.mode === 'scan_only') break;
        // 服务端会告知本次运行的实际模式（按目录 / 按番号）
        if (p.mode === 'by_id' && state.mode !== 'id') setMode('id');
        state.scrapeRunning = true;
        state.scanRunning = false;
        state.terminal = null;
        state.total = p.movie_count || state.total;
        state.startedAt = Date.now();
        state.movies.clear();
        state.finished = 0; state.failed = 0;
        startTimer();
        pushLog(`开始整理 ${p.movie_count} 部影片`);
        break;

      case 'movie.started':
        state.movies.set(p.movie_id, {
          id: p.movie_id, files: p.files || [], sites: new Map(),
          step: '', stepIndex: 0, stepTotal: 0, status: 'active', error: null,
        });
        break;

      case 'movie.step': {
        const m = state.movies.get(p.movie_id);
        if (m) {
          m.step = p.step;
          m.stepIndex = p.step_index;
          m.stepTotal = p.step_total;
        }
        break;
      }

      case 'movie.finished': {
        const m = state.movies.get(p.movie_id);
        if (m) { m.status = 'done'; m.step = ''; if (p.title) m.title = p.title; }
        // 按番号模式：把 nfo 路径补到列表项，使其可点击查看信息
        const item = state.idItems.find((it) => it.id === p.movie_id);
        if (item) {
          item.path = p.nfo_file || (p.save_dir ? p.save_dir + '/movie.nfo' : '');
          item.scraped = !!item.path;
          item.title = p.title || '';
        }
        state.finished += 1;
        pushLog(`完成 ${p.movie_id}`, 'ok');
        break;
      }

      case 'movie.failed': {
        const m = state.movies.get(p.movie_id);
        const msg = p.message || p.error || '';
        if (m) { m.status = 'failed'; m.error = msg; }
        state.failed += 1;
        pushLog(`失败 ${p.movie_id}${p.step ? ' @' + (STEP_LABELS[p.step] || p.step) : ''}: ${msg}`, 'err');
        break;
      }

      case 'crawler.started':
      case 'crawler.succeeded':
      case 'crawler.failed':
      case 'crawler.retry':
        handleCrawler(evt.kind, p);
        break;

      case 'log':
        if (p.level === 'ERROR') pushLog(p.message || '', 'err');
        else if (p.level === 'WARNING') pushLog(p.message || '', 'warn');
        break;

      case 'worker.stderr':
        if (/error|traceback|错误|失败/i.test(p.message || '')) pushLog(p.message, 'err');
        break;

      case 'worker.stderr_tail':
        if (state.terminal === 'failed' && Array.isArray(p.lines)) {
          for (const line of p.lines.slice(-5)) {
            if (/error|traceback|错误|失败/i.test(line)) pushLog(line, 'err');
          }
        }
        break;

      case 'gui.directory_dialog_opened':
        el.scanHint.textContent = '已打开目录选择窗口…';
        break;

      case 'gui.directory_changed':
        // 扫描/抓取开始时服务端会记录目录，前端据此同步输入框，
        // 这样 "root" 参数始终与后端一致（图片接口用它限定可读范围）
        if (p.directory && p.directory !== state.directory) setDirectory(p.directory);
        break;

      case 'gui.directory_selected':
        el.scanHint.textContent = '';
        if (p.ok) {
          // 选择结果写到当前模式对应的输入框
          if (state.mode === 'id') {
            state.targetFolder = p.path;
            el.targetInput.value = p.path;
            el.idHint.textContent = '目标文件夹：' + p.path;
          } else {
            setDirectory(p.path);
            pushRecent(p.path);
          }
          pushLog('已选择: ' + p.path, 'ok');
        } else if (p.error === 'cancelled') {
          pushLog('已取消目录选择');
        } else {
          const msg = p.message || p.error || '未知错误';
          pushLog('无法选择目录: ' + msg, 'err');
          toast('无法打开目录选择窗口：' + msg, 'err');
        }
        break;

      case 'scan.skipped':
        // 按番号获取模式没有目录扫描阶段。用输入的番号直接作为列表展示，
        // 抓取完成后每项补上 nfo 路径，即可像按目录模式一样点击查看信息。
        state.idItems = (p.movie_ids || []).map((id) => ({ id, path: '', scraped: false }));
        state.idFolder = p.folder || p.root || '';
        state.total = state.idItems.length;
        pushLog(`按番号获取 ${p.movie_count} 个番号` +
          (state.idFolder ? `，输出到 ${state.idFolder}` : ''), '');
        break;

      case 'gui.job_started':
        if (!state.startedAt) { state.startedAt = Date.now(); startTimer(); }
        break;

      case 'gui.stopping':
        pushLog('正在停止…', 'warn');
        break;

      case 'gui.stopped':
        state.terminal = 'stopped';
        state.scanRunning = false; state.scrapeRunning = false;
        state.scan.active = false;
        stopTimer();
        pushLog('任务已停止', 'warn');
        break;

      case 'run.finished':
        if (p.mode === 'scan_only') break;
        state.terminal = state.failed > 0 ? 'failed' : 'done';
        if (p.finished_count != null) state.finished = p.finished_count;
        stopTimer();
        pushLog(`整理完成：成功 ${p.finished_count}/${p.movie_count}`,
          state.failed ? 'warn' : 'ok');
        if (!state.failed) toast(`整理完成，共 ${p.finished_count} 部`, 'ok');
        break;

      case 'run.failed':
        state.terminal = 'failed';
        state.scanRunning = false; state.scrapeRunning = false;
        state.scan.active = false;
        stopTimer();
        pushLog('运行失败: ' + (p.message || p.error || ''), 'err');
        toast('运行失败：' + (p.message || p.error || ''), 'err');
        break;

      case 'gui.job_exited':
        // worker 退出是权威信号：据此收敛运行标志，避免状态卡在"进行中"。
        // 注意 mode 有三种取值：'scan' / 'scrape' / 'by_id'——
        // 按番号获取用的是 'by_id'，早前只处理前两种，导致按番号模式
        // 抓取完成后状态一直停在"抓取中"、停止按钮也无法点击。
        state.exitOk = !!p.ok;
        if (p.mode === 'scan') {
          state.scanRunning = false;
          state.scan.active = false;
        } else if (p.mode === 'scrape' || p.mode === 'by_id') {
          state.scrapeRunning = false;
          if (!p.ok && state.terminal !== 'stopped') state.terminal = 'failed';
        } else {
          // 未知 mode：一律视为任务已结束，宁可显示"已完成"也不能卡在运行态
          state.scanRunning = false;
          state.scrapeRunning = false;
          state.scan.active = false;
        }
        stopTimer();
        break;

      default:
        break;
    }
    schedule();
  }

  function handleScanProgress(p) {
    const s = state.scan;
    s.active = true;
    if (p.current_dir) s.currentDir = p.current_dir;
    if (p.file_count != null) s.fileCount = p.file_count;
    if (p.video_count != null) s.videoCount = p.video_count;

    if (p.phase === 'recognized' && p.avid) {
      // 扫描阶段的实时番号（文件明细要等 scan.finished 的结构化结果）
      if (!s.movies.some((m) => m.id === p.avid)) {
        s.movies.push({ id: p.avid, files: [], file_count: 1, pending: true });
      }
      state.total = s.movies.length;
    } else if (p.phase === 'unrecognized' && p.path) {
      if (!s.unrecognized.includes(p.path)) s.unrecognized.push(p.path);
    } else if (p.phase === 'done') {
      s.summary = `识别到 ${p.movie_count} 部 · 视频 ${p.video_count} 个` +
        (p.unrecognized ? ` · 无法识别 ${p.unrecognized}` : '') +
        (p.skipped_small ? ` · 跳过小文件 ${p.skipped_small}` : '');
    }
  }

  function handleCrawler(kind, p) {
    const key = p.movie_id;
    let m = key ? state.movies.get(key) : null;
    if (!m) {
      for (const cand of state.movies.values()) {
        if (cand.status === 'active') { m = cand; break; }
      }
    }
    if (!m || !p.crawler) return;
    if (kind === 'crawler.succeeded') m.sites.set(p.crawler, 'ok');
    else if (kind === 'crawler.failed') m.sites.set(p.crawler, 'err');
    else if (kind === 'crawler.retry' && m.sites.get(p.crawler) !== 'ok') {
      m.sites.set(p.crawler, 'retry');
    }
  }

  function startTimer() {
    stopTimer();
    state.endedAt = null;
    state.timer = setInterval(() => { el.mTime.textContent = elapsed(); }, 1000);
    el.mTime.textContent = elapsed();
  }
  function stopTimer() {
    if (state.timer) { clearInterval(state.timer); state.timer = null; }
    state.endedAt = Date.now();
    el.mTime.textContent = elapsed();
  }

  /* ------------------------------- API ------------------------------- */
  async function api(path, body) {
    try {
      const res = await fetch(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Auth': TOKEN },
        body: JSON.stringify(body || {}),
      });
      return await res.json();
    } catch (e) {
      return { ok: false, error: 'network', message: '无法连接到本地服务' };
    }
  }

  function setDirectory(dir) {
    state.directory = dir || '';
    el.dirInput.value = state.directory;
    el.scanHint.textContent = '';
    schedule();
  }

  el.browse.addEventListener('click', async () => {
    el.scanHint.textContent = '正在打开目录选择窗口…';
    const res = await api('/api/select_directory', { initial: state.directory });
    if (!res.ok) {
      el.scanHint.textContent = '';
      const msg = res.message || res.error || '未知错误';
      pushLog('无法打开目录选择窗口: ' + msg, 'err');
      toast('无法打开目录选择窗口：' + msg, 'err');
    }
    // 成功时结果通过 gui.directory_selected 事件返回
  });

  el.scan.addEventListener('click', async () => {
    if (!state.directory) return;
    el.scanHint.textContent = '正在扫描…';
    const res = await api('/api/scan', { directory: state.directory });
    if (!res.ok) {
      el.scanHint.textContent = '';
      const msg = res.message || res.error || '';
      pushLog('扫描失败: ' + msg, 'err');
      toast(msg || '扫描失败', 'err');
    } else {
      pushRecent(state.directory);
    }
  });

  el.start.addEventListener('click', async () => {
    if (state.mode === 'id') {
      const ids = parseIds(state.idsText);
      if (!ids.length) { toast('请先输入番号', 'warn'); return; }
      const res = await api('/api/start_by_id', {
        folder: state.targetFolder, ids: ids.join('\n'),
      });
      if (!res.ok) {
        const msg = res.message || res.error || '';
        pushLog('无法开始: ' + msg, 'err');
        toast(msg || '无法开始', 'err');
      }
      return;
    }
    if (!state.directory) return;
    pushRecent(state.directory);
    const res = await api('/api/start', { directory: state.directory });
    if (!res.ok) {
      const msg = res.message || res.error || '';
      pushLog('无法开始: ' + msg, 'err');
      toast(msg || '无法开始', 'err');
    }
  });

  el.stop.addEventListener('click', async () => {
    const res = await api('/api/stop');
    if (!res.ok) toast(res.message || '停止失败', 'err');
  });

  el.clearLog.addEventListener('click', () => {
    state.logs = [];
    el.logs.innerHTML = '';
  });

  el.dirInput.addEventListener('change', () => setDirectory(el.dirInput.value.trim()));
  el.dirInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { setDirectory(el.dirInput.value.trim()); el.scan.focus(); }
  });

  /* ----------------------- 模式切换与番号输入 ----------------------- */
  el.tabDir.addEventListener('click', () => setMode('dir'));
  el.tabId.addEventListener('click', () => setMode('id'));

  el.targetInput.addEventListener('input', () => {
    state.targetFolder = el.targetInput.value.trim();
  });

  el.idsInput.addEventListener('input', () => {
    state.idsText = el.idsInput.value;
    const n = parseIds(state.idsText).length;
    const running = state.scrapeRunning || state.scanRunning;
    el.start.disabled = running || n === 0;
    el.idHint.textContent = n
      ? `将获取 ${n} 个番号，每个生成 <目标文件夹>/<番号>/`
      : '每个番号会生成 <目标文件夹>/<番号>/，内含 NFO、封面与海报。';
  });

  el.browseTarget.addEventListener('click', async () => {
    el.idHint.textContent = '正在打开目录选择窗口…';
    const res = await api('/api/select_directory', { initial: state.targetFolder });
    if (!res.ok) {
      el.idHint.textContent = '';
      const msg = res.message || res.error || '未知错误';
      pushLog('无法打开目录选择窗口: ' + msg, 'err');
      toast('无法打开目录选择窗口：' + msg, 'err');
    }
    // 成功时结果通过 gui.directory_selected 事件返回（按当前模式写入对应输入框）
  });

  /* ------------------------- 气泡的交互绑定 ------------------------- */  el.previewBody.addEventListener('click', (e) => {
    // "在访达中显示" 按钮
    const revealBtn = e.target.closest('[data-reveal]');
    if (revealBtn) {
      const path = revealBtn.dataset.reveal;
      api('/api/reveal', { path }).then((r) => {
        if (!r.ok) toast(r.message || '无法在访达中显示', 'err');
      });
      return;
    }
    // 点击影片行
    const tr = e.target.closest('tr[data-path]');
    if (tr) openPopover(tr);
  });
  el.popoverClose.addEventListener('click', closePopover);

  /* 点击气泡里的图片放大查看（海报 / 封面 / 剧照都支持） */
  el.popoverContent.addEventListener('click', (e) => {
    const holder = e.target.closest('[data-enlarge], img[data-full]');
    if (!holder) return;
    const img = holder.tagName === 'IMG' ? holder : holder.querySelector('img[data-full]');
    if (img && img.dataset.full) openLightbox(img.dataset.full);
  });

  el.lbClose.addEventListener('click', closeLightbox);
  el.lbPrev.addEventListener('click', (e) => { e.stopPropagation(); stepLightbox(-1); });
  el.lbNext.addEventListener('click', (e) => { e.stopPropagation(); stepLightbox(1); });
  // 点击空白处关闭（点图片本身不关）
  el.lightbox.addEventListener('click', (e) => {
    if (e.target === el.lightbox) closeLightbox();
  });

  // 点击气泡与触发行之外的地方收起
  document.addEventListener('click', (e) => {
    if (!popover.open) return;
    if (el.popover.contains(e.target)) return;
    if (e.target.closest('tr[data-path]')) return;
    // 放大查看层自己有独立的关闭逻辑（点空白关、点图片不关），
    // 不能让它的点击顺带把气泡也关掉
    if (e.target.closest('#lightbox')) return;
    closePopover();
  });

  document.addEventListener('keydown', (e) => {
    // 放大查看优先响应（Esc 先关放大，再关气泡）
    if (!el.lightbox.classList.contains('hidden')) {
      if (e.key === 'Escape') { closeLightbox(); return; }
      if (e.key === 'ArrowLeft') { stepLightbox(-1); return; }
      if (e.key === 'ArrowRight') { stepLightbox(1); return; }
      return;
    }
    if (e.key === 'Escape') closePopover();
  });

  // 窗口尺寸变化或滚动时重新定位，避免气泡飘走
  window.addEventListener('resize', () => {
    if (popover.open && popover.anchor) positionPopover(popover.anchor);
  });
  el.previewBody.addEventListener('scroll', () => {
    if (popover.open) closePopover();
  }, { passive: true });

  /* ------------------------- 影片信息气泡 ------------------------- */
  // 点击预览表格里的某一行 -> 在该行下方弹出气泡展示元数据
  const popover = { open: false, path: '', anchor: null };

  function closePopover() {
    popover.open = false;
    popover.path = '';
    popover.anchor = null;
    el.popover.classList.add('hidden');
    // 气泡关了，放大查看也要一起收起来，避免留下孤立的浮层
    closeLightbox();
    const selected = el.previewBody.querySelector('tr.selected');
    if (selected) selected.classList.remove('selected');
  }

  async function openPopover(tr) {
    const path = tr.dataset.path;
    if (!path) {
      toast('该条目没有可用的文件路径', 'warn');
      return;
    }
    // 再次点击同一行则收起
    if (popover.open && popover.path === path) { closePopover(); return; }

    closePopover();
    popover.open = true;
    popover.path = path;
    popover.anchor = tr;
    tr.classList.add('selected');

    el.popoverContent.innerHTML =
      '<div class="pc-loading"><div class="spinner"></div><span>正在读取影片信息…</span></div>';
    positionPopover(tr);
    el.popover.classList.remove('hidden');

    const res = await api('/api/movie_info', { path, root: state.directory });
    // 请求返回时用户可能已经点了别处
    if (!popover.open || popover.path !== path) return;
    el.popoverContent.innerHTML = renderMovieInfo(res, path);
    positionPopover(tr);
  }

  // 气泡宽度上限。详情里有图片画廊，需要比旧版（520）更宽才放得下；
  // 这个值同时被 positionPopover 用作内联宽度，因此必须与 CSS 里的上限一致。
  const POPOVER_MAX_WIDTH = 760;

  function positionPopover(tr) {
    const rect = tr.getBoundingClientRect();
    const margin = 12;
    const gap = 10;
    const width = Math.min(POPOVER_MAX_WIDTH, window.innerWidth - margin * 2);
    // 左对齐到行首，并保证不超出窗口右边界
    let left = rect.left;
    if (left + width > window.innerWidth - margin) left = window.innerWidth - width - margin;
    if (left < margin) left = margin;

    const spaceBelow = window.innerHeight - rect.bottom - gap - margin;
    const spaceAbove = rect.top - gap - margin;
    // 空间够就朝下，否则翻到上方；两者都很紧时选空间更大的一侧并允许内部滚动
    const flip = spaceBelow < 300 && spaceAbove > spaceBelow;

    el.popover.style.left = left + 'px';
    el.popover.style.width = width + 'px';
    // 画廊需要更多高度，上限相应放宽
    el.popover.style.maxHeight = Math.max(220, Math.min(640, flip ? spaceAbove : spaceBelow)) + 'px';

    if (flip) {
      el.popover.style.top = 'auto';
      el.popover.style.bottom = (window.innerHeight - rect.top + gap) + 'px';
    } else {
      el.popover.style.bottom = 'auto';
      el.popover.style.top = (rect.bottom + gap) + 'px';
    }

    // 三角指向行的左内侧
    const arrow = el.popoverArrow;
    arrow.style.left = Math.max(16, Math.min(rect.left - left + 14, width - 34)) + 'px';
    if (flip) {
      arrow.style.top = 'auto';
      arrow.style.bottom = '-6px';
      arrow.style.transform = 'rotate(225deg)';
    } else {
      arrow.style.bottom = 'auto';
      arrow.style.top = '-6px';
      arrow.style.transform = 'rotate(45deg)';
    }
  }

  /** 本地图片经服务端读取（带 root 限制，只能读扫描目录内的文件） */
  function localImg(p) {
    return `/api/image?path=${encodeURIComponent(p)}` +
      `&root=${encodeURIComponent(state.directory)}&token=${encodeURIComponent(TOKEN)}`;
  }

  /** 可点击放大的图片（kind: 'poster' | 'fanart' | 'still' | 'other'） */
  function clickableImg(src, cls, alt = '') {
    return `<img class="${cls}" src="${esc(src)}" alt="${esc(alt)}" loading="lazy"
      data-full="${esc(src)}"
      onerror="this.classList.add('broken');this.removeAttribute('src')">`;
  }

  function renderMovieInfo(res, path) {
    if (!res || !res.ok) {
      return `<div class="pc-note">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
             stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16.5v.01"/></svg>
        <span>${esc((res && (res.message || res.error)) || '无法读取影片信息')}</span>
      </div>`;
    }

    const filename = res.filename || '';
    const base = `<div class="pc-sub">${esc(filename)}</div>`;
    const gallery = res.gallery || { poster: null, fanart: null, stills: [], other: [] };

    // 尚未整理：给出明确提示而不是空白
    if (!res.scraped) {
      return `<div class="pc-head"><div class="pc-head-text">
          <h4 class="pc-title">${esc(baseId(filename))}</h4>${base}
        </div></div>
        <div class="pc-note">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
               stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16.5v.01"/></svg>
          <span>${esc(res.message || '这部影片尚未整理，没有可展示的元数据。')}</span>
        </div>
        ${revealButton(path)}`;
    }

    const info = res.info || {};
    const out = [];

    /* ---------- 头部：海报 + 标题 ---------- */
    const posterSrc = gallery.poster || res.poster;
    const poster = posterSrc
      ? clickableImg(localImg(posterSrc), 'pc-poster-img', info.title || '')
      : `<div class="pc-poster-ph">
           <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
             <rect x="3" y="4" width="18" height="16" rx="2.5"/><path d="M3 9h18"/>
             <circle cx="12" cy="14.5" r="2.5"/></svg>
           <span>无海报</span>
         </div>`;

    // 徽章：番号 / 分级 / 时长 / 发布时间，集中放在标题下方
    const badges = [];
    if (info.dvdid) badges.push(`<span class="chip strong">${esc(info.dvdid)}</span>`);
    if (info.cid) badges.push(`<span class="chip">${esc(info.cid)}</span>`);
    if (info.runtime) badges.push(`<span class="chip">${info.runtime} 分钟</span>`);
    if (info.premiered) badges.push(`<span class="chip">${esc(info.premiered)}</span>`);
    if (info.mpaa) badges.push(`<span class="chip">${esc(info.mpaa)}</span>`);
    if (info.score) badges.push(`<span class="chip">★ ${esc(info.score)}</span>`);
    if (info.image_count || res.image_count) {
      badges.push(`<span class="chip">共 ${res.image_count} 张图片</span>`);
    }

    out.push(`<div class="pc-head">
      <button class="pc-poster" type="button" data-enlarge="poster" title="点击查看大图">${poster}</button>
      <div class="pc-head-text">
        <h4 class="pc-title">${esc(info.title || baseId(filename))}</h4>
        ${base}
        <div class="pc-badges">${badges.join('')}</div>
      </div>
    </div>`);

    /* ---------- 图库：封面 / 剧照 ---------- */
    const stills = gallery.stills || [];
    const others = gallery.other || [];
    if (gallery.fanart || stills.length || others.length) {
      const cards = [];
      if (gallery.fanart) {
        cards.push(`<figure class="pc-shot wide" data-enlarge="fanart" title="点击查看大图">
          ${clickableImg(localImg(gallery.fanart), 'pc-shot-img', '封面')}
          <figcaption>封面</figcaption></figure>`);
      }
      stills.forEach((p, i) => {
        cards.push(`<figure class="pc-shot" data-enlarge="still" data-index="${i}" title="点击查看大图">
          ${clickableImg(localImg(p), 'pc-shot-img', `剧照 ${i + 1}`)}
          <figcaption>${i + 1}</figcaption></figure>`);
      });
      others.forEach((p) => {
        cards.push(`<figure class="pc-shot" data-enlarge="other" title="点击查看大图">
          ${clickableImg(localImg(p), 'pc-shot-img', baseId(p))}
          <figcaption>${esc(baseId(p))}</figcaption></figure>`);
      });
      out.push(`<div class="pc-section">
        <h5>图片 <span class="pc-count">${cards.length} 张</span></h5>
        <div class="pc-gallery">${cards.join('')}</div>
      </div>`);
    }

    /* ---------- 元数据 ---------- */
    const rows = [
      ['系列', info.series],
      ['制作商', info.studio],
      ['发行商', info.publisher],
      ['导演', info.director],
      ['原始标题', info.original_title],
      ['文件大小', res.size ? fmtSize(res.size) : null],
      ['所在目录', res.dir],
    ].filter(([, v]) => v);
    if (rows.length) {
      out.push(`<dl class="pc-rows">${rows.map(([k, v]) =>
        `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('')}</dl>`);
    }

    /* ---------- 分类 ---------- */
    const genres = dedupe(info.genres || []);
    if (genres.length) {
      out.push(`<div class="pc-section"><h5>分类</h5>
        <div class="pc-genres">${genres.map((g) =>
        `<span class="pc-genre">${esc(g)}</span>`).join('')}</div></div>`);
    }

    /* ---------- 演员 ---------- */
    const actresses = info.actresses || [];
    if (actresses.length) {
      out.push(`<div class="pc-section"><h5>演员</h5>
        <div class="pc-actresses">${actresses.map((a) => {
        const img = a.thumb
          ? `<img src="${esc(proxyImg(a.thumb))}" alt="" loading="lazy"
                 onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'ph',textContent:${JSON.stringify((a.name || '?')[0])}}))">`
          : `<div class="ph">${esc((a.name || '?')[0])}</div>`;
        return `<div class="pc-actress">${img}<span>${esc(a.name)}</span></div>`;
      }).join('')}</div></div>`);
    }

    /* ---------- 剧情简介 ---------- */
    if (info.plot) {
      out.push(`<div class="pc-section"><h5>剧情简介</h5>
        <p class="pc-plot">${esc(info.plot)}</p></div>`);
    }

    /* ---------- 预告片 ---------- */
    if (info.trailer) {
      out.push(`<div class="pc-section"><h5>预告片</h5>
        <div class="pc-sub">${esc(info.trailer)}</div></div>`);
    }

    out.push(revealButton(path, res.nfo));
    return out.join('');
  }

  function revealButton(path, nfo) {
    return `<div class="pc-actions">
      <button class="btn subtle" type="button" data-reveal="${esc(path)}">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
             stroke-linecap="round" stroke-linejoin="round">
          <path d="M3 7.5A2 2 0 015 5.5h3.6a2 2 0 011.5.7l.9 1.1H19a2 2 0 012 2v7.2a2 2 0 01-2 2H5a2 2 0 01-2-2z"/>
        </svg>
        在访达中显示
      </button>
      ${nfo ? `<span class="pc-nfo">${esc(nfo.split('/').pop())}</span>` : ''}
    </div>`;
  }

  function baseId(filename) {
    return String(filename).replace(/\.[^.]+$/, '');
  }

  /** 远程图片经本地服务代理，避免 webview 的跨域/CSP 问题 */
  function proxyImg(url) {
    return `/api/image?remote=${encodeURIComponent(url)}&token=${encodeURIComponent(TOKEN)}`;
  }

  function dedupe(list) {
    // nfo 里同时写入了单个 genre 和合并后的字符串，展示时去重更清爽
    const cleaned = [];
    for (const g of list) {
      if (g && !cleaned.includes(g)) cleaned.push(g);
    }
    return cleaned;
  }

  function fmtSize(bytes) {
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let n = Number(bytes) || 0, i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
    return `${n.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
  }

  /* --------------------------- 图片放大查看 --------------------------- */
  const lightbox = { list: [], index: 0 };

  function openLightbox(src) {
    // 以当前气泡里的全部图片作为可切换列表，选中被点击的那张
    const imgs = [...el.popoverContent.querySelectorAll('img[data-full]')];
    if (!imgs.length) return;
    lightbox.list = imgs.map((i) => i.dataset.full);
    lightbox.index = Math.max(0, lightbox.list.indexOf(src));
    showLightbox();
    el.lightbox.classList.remove('hidden');
  }

  function showLightbox() {
    const src = lightbox.list[lightbox.index];
    if (!src) return;
    el.lbImg.src = src;
    const many = lightbox.list.length > 1;
    el.lbPrev.classList.toggle('hidden', !many);
    el.lbNext.classList.toggle('hidden', !many);
    el.lbCounter.textContent = many
      ? `${lightbox.index + 1} / ${lightbox.list.length}` : '';
  }

  function stepLightbox(delta) {
    if (lightbox.list.length < 2) return;
    lightbox.index = (lightbox.index + delta + lightbox.list.length) % lightbox.list.length;
    showLightbox();
  }

  function closeLightbox() {
    el.lightbox.classList.add('hidden');
    el.lbImg.removeAttribute('src');
    lightbox.list = [];
  }

  /* ------------------------------ 工具 ------------------------------ */
  /** 解析用户输入的番号（与后端 javsp.worker.parse_movie_ids 规则一致） */
  function parseIds(text) {
    const seen = new Set();
    const out = [];
    for (const raw of String(text || '').split(/[\s,，;；、|/]+/)) {
      const item = raw.trim();
      if (!item) continue;
      const key = item.toLowerCase();
      if (seen.has(key)) continue;
      seen.add(key);
      out.push(item);
    }
    return out;
  }
  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function shortPath(p) {
    const parts = String(p).split('/').filter(Boolean);
    return parts.length <= 3 ? p : '…/' + parts.slice(-3).join('/');
  }

  /* ------------------------------ 启动 ------------------------------ */
  function connect() {
    const es = new EventSource(`/api/events?token=${encodeURIComponent(TOKEN)}`);

    es.addEventListener('open', () => {
      el.conn.className = 'conn on';
      el.conn.title = '已连接';
    });

    es.addEventListener('snapshot', (e) => {
      try { applySnapshot(JSON.parse(e.data)); } catch (err) { console.error(err); }
    });

    es.addEventListener('event', (e) => {
      try { handleEvent(JSON.parse(e.data)); }
      catch (err) { console.error('事件解析失败', err, e.data); }
    });

    es.addEventListener('error', () => {
      el.conn.className = 'conn off';
      el.conn.title = '连接中断，正在重试';
    });
  }

  function applySnapshot(snap) {
    // 历史事件是增量语义（movie.finished 会让计数 +1），直接在旧状态上回放
    // 会重复累加；因此先重置派生状态再按顺序重建。
    state.movies.clear();
    state.total = 0; state.finished = 0; state.failed = 0;
    state.terminal = null;
    state.idItems = [];      // 由历史里的 scan.skipped / movie.finished 重建
    state.idFolder = '';
    state.scan = {
      active: false, phase: '', currentDir: '', fileCount: 0, videoCount: 0,
      movies: [], unrecognized: [], summary: '', done: false,
    };
    state.logs = [];
    el.logs.innerHTML = '';

    if (snap.directory) setDirectory(snap.directory);

    for (const raw of snap.recent || []) {
      try { handleEvent(raw); } catch { /* 单条历史事件异常不应中断重建 */ }
    }

    // 以服务端实时状态为准做最终校准。
    // 这条必须严格服从 snap.running：历史回放可能已经收到 gui.job_exited，
    // 若只按 mode 判断会把已结束的任务重新标成"进行中"。
    state.scanRunning = !!snap.running && snap.mode === 'scan';
    state.scrapeRunning = !!snap.running && snap.mode !== 'scan';
    state.scan.active = state.scanRunning;
    if (snap.running && !state.timer) {
      state.startedAt = Date.now();
      startTimer();
    }
    schedule();
  }

  setDirectory('');
  render();
  connect();
})();
