// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 andam0202
//
// inkdeus の連携スクリプト。inkdeus のサーバが /manga/ で配信する漫画エディタ
// （Manga Editor Desu! Pro Edition、別プロジェクト）のページに差し込まれ、
//   - 素材パネル（inkdeus のプロジェクトの画像をコマへ入れる／ページへ置く）
//   - プロジェクトへの保存・読み込み・自動保存
//   - ページの書き出し
// を足す。漫画エディタ側の DOM とコードは書き換えず、公開されているグローバル関数を呼ぶだけにする。
// 依存しているグローバルは下の G にまとめてある（漫画エディタの版が変わったらここを見直す）。
(function () {
  'use strict';
  if (window.inkdeusBridge) return;

  // ---- 漫画エディタ側のグローバル（無ければ ReferenceError になるので関数で包んで確かめる） ----
  /* global fabric, canvas, lz4Compressor, btmProjectsMap, stateStack, currentStateIndex, lastCheckObject,
     isGridVisible, canvasContinerScale, ImageUtil, UnsavedGuard, MangaPanelsImage_Vertical,
     MangaPanelsImage_Landscape,
     getCanvasGUID, isProjectBusy, putImageInFrame, isPanel, findPanelUnderPoint, getVisibleCanvasCenter,
     updateLayerPanel, saveStateByManual, btmSaveProjectFile, btmSaveCurrentPage, btmGetGuids, btmGetGuidsSize,
     btmWaitForPageReady, btmUpdateHandleText, chengeCanvasByGuid, multiLoadLz4, clearAllProjectPages,
     getContentObjectCount, getCropAndDownloadLinkByMultiplier, removeGrid, drawGrid, showConfirmDialog,
     flushHistory, OP_showLoading, OP_hideLoading, waitNextFrame */
  const G = {
    fabric: () => fabric,
    canvas: () => canvas,
    lz4Compressor: () => lz4Compressor,
    btmProjectsMap: () => btmProjectsMap,
    stateStack: () => stateStack,
    ImageUtil: () => ImageUtil,
    getCanvasGUID: () => getCanvasGUID,
    isProjectBusy: () => isProjectBusy,
    putImageInFrame: () => putImageInFrame,
    isPanel: () => isPanel,
    updateLayerPanel: () => updateLayerPanel,
    saveStateByManual: () => saveStateByManual,
    btmSaveProjectFile: () => btmSaveProjectFile,
    btmSaveCurrentPage: () => btmSaveCurrentPage,
    btmGetGuids: () => btmGetGuids,
    btmGetGuidsSize: () => btmGetGuidsSize,
    btmWaitForPageReady: () => btmWaitForPageReady,
    chengeCanvasByGuid: () => chengeCanvasByGuid,
    multiLoadLz4: () => multiLoadLz4,
    clearAllProjectPages: () => clearAllProjectPages,
    getContentObjectCount: () => getContentObjectCount,
    getCropAndDownloadLinkByMultiplier: () => getCropAndDownloadLinkByMultiplier,
    removeGrid: () => removeGrid,
  };
  // 機能ごとに必要なグローバル。欠けている機能だけを無効にする
  const FEATURES = {
    place: ['fabric', 'canvas', 'putImageInFrame', 'isPanel', 'updateLayerPanel', 'saveStateByManual'],
    save: ['canvas', 'stateStack', 'btmSaveProjectFile', 'btmProjectsMap', 'lz4Compressor', 'isProjectBusy',
      'getCanvasGUID', 'btmGetGuidsSize', 'getContentObjectCount'],
    open: ['lz4Compressor', 'multiLoadLz4', 'clearAllProjectPages', 'chengeCanvasByGuid', 'btmWaitForPageReady',
      'isProjectBusy', 'btmGetGuidsSize', 'getContentObjectCount'],
    export: ['canvas', 'ImageUtil', 'getCropAndDownloadLinkByMultiplier', 'removeGrid', 'btmGetGuids',
      'getCanvasGUID', 'isProjectBusy'],
    exportAll: ['canvas', 'ImageUtil', 'getCropAndDownloadLinkByMultiplier', 'removeGrid', 'btmGetGuids',
      'getCanvasGUID', 'isProjectBusy', 'btmSaveCurrentPage', 'chengeCanvasByGuid', 'btmWaitForPageReady'],
  };
  const FEATURE_LABELS = { place: '画像の配置', save: '保存', open: '読み込み', export: 'ページの書き出し',
    exportAll: '全ページの書き出し' };

  function has(name) {
    try { return G[name]() != null; } catch (e) { return false; }
  }
  function optional(fn) {
    try { return fn(); } catch (e) { return undefined; }
  }
  function missingFor(feature) {
    return FEATURES[feature].filter((n) => !has(n));
  }

  const AUTOSAVE_NAME = 'autosave';
  const NAME_RE = /^[\p{L}\p{N}_-]{1,64}$/u;
  const SIZES = [[1024, '長辺 1024'], [2048, '長辺 2048'], [4096, '長辺 4096'], [0, '原寸']];
  const PAGE_SIZE = 60;
  const READY_TIMEOUT_MS = 90000;
  const NO_EDITOR_GRACE_MS = 4000;          // 読み込みが終わってもキャンバスすら無いとき、別物と見なすまでの猶予
  const POLL_MS = 2000;
  const FREE_WIDTH_RATIO = 0.25;            // 自由配置する素材の既定の幅（ページ幅に対する割合）

  const state = {
    ready: false,
    features: {},                // 機能名 → 使えるか
    missing: {},                 // 機能名 → 欠けているグローバル
    project: null,               // inkdeus のプロジェクト（無ければ null）
    mangaDir: '',
    sources: [],
    source: null,                // 選択中のソース名
    q: '',
    items: [],
    total: 0,
    imageMax: 2048,
    name: '',                    // 保存名
    saved: [],                   // 保存済みの漫画データ
    busy: false,                 // 連携スクリプトが保存・読み込み・書き出しの最中
    autosave: { enabled: true, seconds: 60, lastSig: null, dirtySince: 0, pageChanged: false, lastAt: 0 },
  };

  // ---- 小物 ----

  const store = {
    get(key, fallback) {
      try {
        const v = localStorage.getItem('inkdeus.manga.' + key);
        return v === null ? fallback : JSON.parse(v);
      } catch (e) { return fallback; }
    },
    set(key, value) {
      try { localStorage.setItem('inkdeus.manga.' + key, JSON.stringify(value)); } catch (e) { /* 保存できなくても動く */ }
    },
  };

  function el(tag, props, children) {
    const node = document.createElement(tag);
    Object.entries(props || {}).forEach(([k, v]) => {
      if (k === 'class') node.className = v;
      else if (k === 'text') node.textContent = v;
      else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
      else if (v === true) node.setAttribute(k, '');
      else if (v !== false && v != null) node.setAttribute(k, v);
    });
    (children || []).forEach((c) => node.appendChild(typeof c === 'string' ? document.createTextNode(c) : c));
    return node;
  }

  function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }
  function nextFrames(n) {
    let p = Promise.resolve();
    for (let i = 0; i < (n || 1); i++) {
      p = p.then(() => new Promise((r) => { const t = setTimeout(r, 120); requestAnimationFrame(() => { clearTimeout(t); r(); }); }));
    }
    return p;
  }

  async function api(method, path, body, raw) {
    const init = { method, headers: {} };
    if (body !== undefined) init.body = body;
    const res = await fetch(path, init);
    if (!res.ok) {
      let detail = '';
      try { detail = (await res.json()).error || ''; } catch (e) { /* JSON でない応答 */ }
      const err = new Error(detail || `サーバがエラーを返しました (HTTP ${res.status})`);
      err.status = res.status;
      throw err;
    }
    return raw ? res : res.json();
  }

  function assetPath(source, rel) {
    return encodeURIComponent(source) + '/__KIND__/' + String(rel).split('/').map(encodeURIComponent).join('/');
  }
  // 画像の URL。相対 URL にしておくと、保存した漫画データが接続先（ポート）に縛られない
  function assetUrl(source, rel, max) {
    const base = '/api/assets/' + assetPath(source, rel).replace('__KIND__', 'image');
    return max ? `${base}?max=${max}` : base;
  }
  function thumbUrl(source, rel) {
    return '/api/assets/' + assetPath(source, rel).replace('__KIND__', 'thumb') + '?h=160';
  }
  function stem(rel) {
    const base = String(rel).split('/').pop();
    return base.replace(/\.[^.]+$/, '');
  }
  function formatTime(seconds) {
    const d = new Date(seconds * 1000);
    const pad = (n) => String(n).padStart(2, '0');
    return `${d.getMonth() + 1}/${d.getDate()} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }
  function validName(name) {
    return typeof name === 'string' && NAME_RE.test(name) && name !== AUTOSAVE_NAME;
  }

  // ---- 画面（素材パネル） ----

  const ui = {};

  function buildPanel() {
    ui.msg = el('div', { class: 'ikb-msg', role: 'status', hidden: true });
    ui.resume = el('div', { class: 'ikb-resume', hidden: true });
    ui.projectName = el('span', { class: 'ikb-project-name' });
    ui.tabs = el('div', { class: 'ikb-tabs', role: 'tablist' });
    ui.search = el('input', { type: 'search', class: 'ikb-search', placeholder: '名前で絞り込み', 'aria-label': '名前で絞り込み' });
    ui.size = el('select', { class: 'ikb-size', title: 'コマへ渡す画像の大きさ', 'aria-label': 'コマへ渡す画像の大きさ' },
      SIZES.map(([value, label]) => el('option', { value: String(value), text: label })));
    ui.reload = el('button', { type: 'button', class: 'ikb-icon', text: '↻', title: '素材の一覧を読み直す',
      'aria-label': '素材の一覧を読み直す', onclick: () => run(() => refresh()) });
    ui.target = el('div', { class: 'ikb-target' });
    ui.grid = el('div', { class: 'ikb-grid' });
    ui.more = el('button', { type: 'button', class: 'ikb-more', text: 'さらに表示', hidden: true, onclick: () => loadItems(true) });
    ui.count = el('div', { class: 'ikb-count' });
    ui.name = el('input', { type: 'text', class: 'ikb-name', placeholder: '保存名', 'aria-label': '保存名', maxlength: '64' });
    ui.btnSave = el('button', { type: 'button', text: 'プロジェクトに保存', onclick: () => run(() => save()) });
    ui.saved = el('select', { class: 'ikb-saved', 'aria-label': '保存済みの漫画データ' });
    ui.btnOpen = el('button', { type: 'button', text: 'プロジェクトから開く', onclick: () => run(() => open(ui.saved.value)) });
    ui.btnExport = el('button', { type: 'button', text: 'このページを書き出す', onclick: () => run(() => exportPage()) });
    ui.btnExportAll = el('button', { type: 'button', text: '全ページを書き出す', onclick: () => run(() => exportAllPages()) });
    ui.autosave = el('input', { type: 'checkbox', id: 'ikb-autosave' });
    ui.autosaveState = el('span', { class: 'ikb-dim' });
    ui.dir = el('div', { class: 'ikb-dim ikb-dir' });

    ui.body = el('div', { class: 'ikb-body' }, [
      el('div', { class: 'ikb-head' }, [
        el('a', { class: 'ikb-link', href: '/', text: '← 白黒化の画面へ' }),
        ui.projectName,
      ]),
      ui.msg,
      ui.resume,
      el('div', { class: 'ikb-section ikb-assets' }, [
        ui.tabs,
        el('div', { class: 'ikb-row' }, [ui.search, ui.size, ui.reload]),
        ui.target,
        ui.grid,
        ui.more,
        ui.count,
      ]),
      el('details', { class: 'ikb-section ikb-file', open: true }, [
        el('summary', { text: '保存と書き出し' }),
        el('div', { class: 'ikb-row' }, [ui.name, ui.btnSave]),
        el('div', { class: 'ikb-row' }, [ui.saved, ui.btnOpen]),
        el('div', { class: 'ikb-row' }, [ui.btnExport, ui.btnExportAll]),
        el('div', { class: 'ikb-row ikb-autosave-row' }, [
          ui.autosave, el('label', { for: 'ikb-autosave', text: '自動保存' }), ui.autosaveState,
        ]),
        ui.dir,
      ]),
    ]);
    ui.toggle = el('button', { type: 'button', class: 'ikb-toggle', title: '素材パネルを開く／閉じる', 'aria-expanded': 'true',
      onclick: () => setOpen(!ui.root.classList.contains('ikb-open')) }, [el('span', { text: '素材' })]);
    ui.root = el('aside', { id: 'inkdeus-bridge', class: 'ikb', 'aria-label': 'inkdeus の素材パネル' }, [ui.toggle, ui.body]);

    // 入力欄でのキー操作が、漫画エディタ側のショートカット（Delete・Ctrl+Z など）として働かないようにする
    ['keydown', 'keyup', 'keypress', 'paste', 'copy', 'cut'].forEach((type) => {
      ui.root.addEventListener(type, (e) => e.stopPropagation());
    });

    let timer = 0;
    ui.search.addEventListener('input', () => {
      clearTimeout(timer);
      timer = setTimeout(() => { state.q = ui.search.value; loadItems(false); }, 250);
    });
    ui.size.addEventListener('change', () => {
      state.imageMax = parseInt(ui.size.value, 10) || 0;
      store.set('imageMax', state.imageMax);
    });
    ui.name.addEventListener('change', () => { state.name = ui.name.value.trim(); });
    ui.autosave.addEventListener('change', () => {
      state.autosave.enabled = ui.autosave.checked;
      store.set('autosave', state.autosave.enabled);
      renderStatus();
    });

    state.imageMax = store.get('imageMax', 2048);
    if (!SIZES.some(([v]) => v === state.imageMax)) state.imageMax = 2048;
    ui.size.value = String(state.imageMax);
    state.autosave.enabled = store.get('autosave', true) !== false;
    ui.autosave.checked = state.autosave.enabled;

    document.documentElement.classList.add('ikb-docked');
    document.body.appendChild(ui.root);
    setOpen(store.get('panelOpen', true) !== false, true);
  }

  function setOpen(open, quiet) {
    ui.root.classList.toggle('ikb-open', open);
    document.documentElement.classList.toggle('ikb-docked-open', open);
    ui.toggle.setAttribute('aria-expanded', String(open));
    if (!quiet) store.set('panelOpen', open);
    // 画面の幅が変わるので、漫画エディタ側のキャンバスを合わせ直させる
    setTimeout(() => window.dispatchEvent(new Event('resize')), 0);
  }

  let msgTimer = 0;
  function say(text, kind) {
    clearTimeout(msgTimer);
    ui.msg.textContent = text || '';
    ui.msg.hidden = !text;
    ui.msg.dataset.kind = kind || 'info';
    if (text && kind !== 'error' && kind !== 'work') msgTimer = setTimeout(() => say(''), 8000);
  }
  function fail(error, prefix) {
    console.error('[inkdeus]', error);                        // 握りつぶさずコンソールにも出す
    say((prefix ? prefix + ': ' : '') + (error && error.message ? error.message : String(error)), 'error');
  }

  // ボタンから呼ぶ処理の共通の包み: 失敗はパネル内に表示して、漫画エディタの動作は止めない
  async function run(fn) {
    try {
      return await fn();
    } catch (e) {
      fail(e);
      return undefined;
    }
  }

  function renderTabs() {
    ui.tabs.replaceChildren(...state.sources.map((src) => el('button', {
      type: 'button', role: 'tab', class: 'ikb-tab' + (src.name === state.source ? ' ikb-active' : ''),
      'aria-selected': String(src.name === state.source),
      title: (src.mode === 'free' ? 'ページ上へ自由に置く素材' : 'コマにはめ込む画像') + (src.missing ? '（フォルダが見つかりません）' : ''),
      onclick: () => selectSource(src.name),
    }, [src.label, el('span', { class: 'ikb-badge', text: String(src.count) })])));
  }

  function currentSource() {
    return state.sources.find((s) => s.name === state.source) || null;
  }

  function renderGrid() {
    const src = currentSource();
    const nodes = [];
    let group = null;
    state.items.forEach((item) => {
      if (src && src.recursive && item.group !== group) {
        group = item.group;
        nodes.push(el('div', { class: 'ikb-group', text: group || '（直下）' }));
      }
      const img = el('img', { loading: 'lazy', decoding: 'async', alt: item.name, src: thumbUrl(src.name, item.path), draggable: 'false' });
      const cell = el('div', {
        role: 'button', tabindex: '0', class: 'ikb-item' + (src.mode === 'free' ? ' ikb-free' : ''), draggable: 'true',
        title: `${item.path}（${item.width}×${item.height}）`,
        'data-path': item.path,
        onclick: () => run(() => placeAsset(src.name, item.path)),
        onkeydown: (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); e.currentTarget.click(); } },
        ondragstart: (e) => {
          e.dataTransfer.setData('application/x-inkdeus-asset', JSON.stringify({ source: src.name, path: item.path }));
          e.dataTransfer.effectAllowed = 'copy';
        },
      }, [img, el('span', { class: 'ikb-caption', text: item.name })]);
      nodes.push(cell);
    });
    if (!state.items.length) {
      const empty = !src ? '' : src.missing ? 'このソースのフォルダが見つかりません'
        : state.q ? '当てはまる画像がありません'
          : src.name === 'inked' ? 'まだ書き出した画像がありません（白黒化の画面で「PNG を書き出す」）' : '画像がありません';
      nodes.push(el('div', { class: 'ikb-empty', text: empty }));
    }
    ui.grid.replaceChildren(...nodes);
    ui.more.hidden = state.items.length >= state.total;
    ui.count.textContent = state.total ? `${state.items.length} / ${state.total} 枚` : '';
  }

  function renderSaved() {
    const rows = state.saved.slice().sort((a, b) => b.mtime - a.mtime);
    ui.saved.replaceChildren(...(rows.length ? rows.map((p) => el('option', {
      value: p.name, text: `${p.autosave ? '自動保存' : p.name}（${formatTime(p.mtime)}）`,
    })) : [el('option', { value: '', text: '保存済みの物はありません' })]));
    ui.btnOpen.disabled = !rows.length || !state.features.open;
  }

  function renderStatus() {
    const f = state.features;
    ui.btnSave.disabled = !f.save;
    ui.btnOpen.disabled = !f.open || !state.saved.length;
    ui.btnExport.disabled = !f.export;
    ui.btnExportAll.disabled = !f.exportAll;
    ui.autosave.disabled = !f.save;
    const a = state.autosave;
    ui.autosaveState.textContent = !f.save ? '' : !a.enabled ? 'オフ'
      : a.lastAt ? `${formatTime(a.lastAt / 1000)} に保存` : `変更から ${a.seconds} 秒後`;
    ui.dir.textContent = state.mangaDir ? `保存先: ${state.mangaDir}` : '';
    ui.dir.title = state.mangaDir;
  }

  function renderTarget() {
    const src = currentSource();
    if (!state.ready || !src) { ui.target.textContent = ''; return; }
    if (!state.features.place) {
      ui.target.textContent = '';
      return;
    }
    if (src.mode === 'free') {
      ui.target.textContent = 'クリックでページ上に置きます（ドラッグで位置を指定）';
      ui.target.dataset.kind = 'ok';
      return;
    }
    const panel = getTargetPanel();
    ui.target.textContent = panel ? `入れ先のコマ: ${panel.name || 'コマ'}`
      : 'コマを選ぶ（コマかレイヤーをクリック）か、画像をコマへドラッグ';
    ui.target.dataset.kind = panel ? 'ok' : 'none';
  }

  // ---- 素材 ----

  async function listSources() {
    const data = await api('GET', '/api/assets');
    state.sources = data.sources || [];
    if (!state.sources.some((s) => s.name === state.source)) {
      state.source = state.sources.length ? state.sources[0].name : null;
    }
    renderTabs();
    return data;
  }

  function listAssets(source, opts) {
    const o = opts || {};
    const params = new URLSearchParams({ q: o.q || '', offset: String(o.offset || 0), limit: String(o.limit || PAGE_SIZE) });
    return api('GET', `/api/assets/${encodeURIComponent(source)}?${params}`);
  }

  let listToken = 0;
  async function loadItems(append) {
    if (!state.source) { state.items = []; state.total = 0; renderGrid(); return; }
    const token = ++listToken;
    try {
      const data = await listAssets(state.source, { q: state.q, offset: append ? state.items.length : 0 });
      if (token !== listToken) return;                       // 後から始めた読み込みを優先する
      state.items = append ? state.items.concat(data.items) : data.items;
      state.total = data.total;
      renderGrid();
    } catch (e) {
      fail(e, '素材の一覧を読めません');
    }
  }

  async function selectSource(name) {
    state.source = name;
    store.set('source', name);
    renderTabs();
    renderTarget();
    await loadItems(false);
  }

  async function refresh() {
    await listSources();
    await loadItems(false);
  }

  // ---- コマ ----

  function panels() {
    if (!has('canvas')) return [];
    return canvas.getObjects().filter((o) => o && o.isPanel);
  }

  // 「選択中のコマ」。コマは既定でロックされていて選択枠が出ないので、
  //   1. 選択中のオブジェクトがコマ（レイヤー一覧で選んだとき）
  //   2. 選択中の画像が入っているコマ
  //   3. 最後にキャンバス上で押したコマ（漫画エディタが覚えている lastCheckObject）
  // の順に探す
  function getTargetPanel() {
    if (!has('canvas')) return null;
    const onCanvas = (o) => o && o.isPanel && canvas.getObjects().indexOf(o) !== -1;
    const active = canvas.getActiveObject();
    if (onCanvas(active)) return active;
    if (active && onCanvas(active.relatedPoly)) return active.relatedPoly;
    const last = optional(() => lastCheckObject);
    if (onCanvas(last)) return last;
    if (last && onCanvas(last.relatedPoly)) return last.relatedPoly;
    return null;
  }

  function resolvePanel(ref) {
    if (ref == null) return getTargetPanel();
    const list = panels();
    if (typeof ref === 'number') return list[ref] || null;
    if (typeof ref === 'string') return list.find((p) => p.guid === ref || p.name === ref) || null;
    return list.indexOf(ref) !== -1 ? ref : null;
  }

  function describe(obj) {
    if (!obj) return null;
    return { type: obj.type, name: obj.name || '', guid: obj.guid || null, isPanel: !!obj.isPanel,
      left: obj.left, top: obj.top, width: obj.width * obj.scaleX, height: obj.height * obj.scaleY,
      panel: obj.relatedPoly ? (obj.relatedPoly.guid || obj.relatedPoly.name || null) : null };
  }

  function loadFabricImage(url) {
    return new Promise((resolve, reject) => {
      fabric.Image.fromURL(url, (img, isError) => {
        if (isError || !img || !img.width) reject(new Error('画像を読み込めません: ' + url));
        else resolve(img);
      });
    });
  }

  function requireFeature(feature) {
    if (!state.ready) throw new Error('漫画エディタの準備がまだ終わっていません');
    if (!state.features[feature]) {
      throw new Error(`対応していない版です（${FEATURE_LABELS[feature]}に必要な関数: ${state.missing[feature].join(', ')}）`);
    }
  }
  // 自動保存やページの読み込みが動いている間は少し待つ。利用者が始めた処理（保存・書き出し）の最中なら断る
  async function waitIdle() {
    const started = Date.now();
    for (;;) {
      if (autosaving) await autosaving.catch(() => {});
      else if (state.busy) throw new Error('別の処理の最中です。終わってからもう一度試してください');
      else if (!(has('isProjectBusy') && isProjectBusy())) return;
      else if (Date.now() - started > 15000) throw new Error('ページの読み込みが終わりません。少し待ってからもう一度試してください');
      else await sleep(100);
    }
  }

  // コマへ入れる。漫画エディタの「コマを選んで画像を貼り付けたとき」と同じ手順
  // （コマの中心を指定して putImageInFrame に入れ先のコマを渡す）
  async function placeInPanel(source, rel, opts) {
    requireFeature('place');
    const o = opts || {};
    const panel = resolvePanel(o.panel);
    if (!panel) {
      const err = new Error('入れ先のコマが選ばれていません。コマ（またはレイヤー一覧のコマ）をクリックしてから選ぶか、画像をコマへドラッグしてください');
      err.code = 'no-panel';
      throw err;
    }
    const max = o.max === undefined ? state.imageMax : o.max;
    const img = await loadFabricImage(assetUrl(source, rel, max));
    if (canvas.getObjects().indexOf(panel) === -1) throw new Error('入れ先のコマが無くなりました');
    img.name = stem(rel);
    const x = panel.left + (panel.width * panel.scaleX) / 2;
    const y = panel.top + (panel.height * panel.scaleY) / 2;
    putImageInFrame(img, x, y, false, false, true, panel);
    return { ok: true, mode: 'panel', object: describe(img), panel: describe(panel) };
  }

  // ページ上へ自由に置く。漫画エディタの「コマに入れない貼り付け」と同じ手順
  // （大きさと位置を決めて canvas.add → 選択 → レイヤー一覧の更新 → 履歴へ記録）
  async function placeFree(source, rel, opts) {
    requireFeature('place');
    const o = opts || {};
    const max = o.max === undefined ? state.imageMax : o.max;
    const img = await loadFabricImage(assetUrl(source, rel, max));
    const ratio = o.widthRatio || FREE_WIDTH_RATIO;
    const scale = (canvas.getWidth() * ratio) / img.width;
    let center = o.at || optional(() => getVisibleCanvasCenter());
    if (!center || !isFinite(center.x) || !isFinite(center.y)) {
      center = { x: canvas.getWidth() / 2, y: canvas.getHeight() / 2 };
    }
    img.name = stem(rel);
    img.set({ scaleX: scale, scaleY: scale,
      left: center.x - (img.width * scale) / 2, top: center.y - (img.height * scale) / 2 });
    canvas.add(img);
    canvas.setActiveObject(img);
    canvas.renderAll();
    updateLayerPanel();
    saveStateByManual();
    return { ok: true, mode: 'free', object: describe(img), panel: null };
  }

  async function placeAsset(source, rel, opts) {
    const o = opts || {};
    let mode = o.mode;
    if (!mode) {
      const src = state.sources.find((s) => s.name === source);
      mode = src ? src.mode : 'panel';
    }
    await waitIdle();
    say('画像を読み込んでいます…', 'work');
    const result = mode === 'free' ? await placeFree(source, rel, o) : await placeInPanel(source, rel, o);
    say(mode === 'free' ? `ページに置きました: ${stem(rel)}` : `コマに入れました: ${result.panel.name || 'コマ'} ← ${stem(rel)}`);
    renderTarget();
    return result;
  }

  // サムネイルをキャンバスへ落としたとき: 落とした位置のコマへ入れる（自由配置の素材はその位置へ置く）
  function wireDrop() {
    const container = document.getElementById('canvas-container');
    if (!container) return;
    container.addEventListener('dragover', (e) => {
      if (Array.from(e.dataTransfer.types || []).includes('application/x-inkdeus-asset')) e.preventDefault();
    });
    container.addEventListener('drop', (e) => {
      const raw = e.dataTransfer.getData('application/x-inkdeus-asset');
      if (!raw) return;
      e.preventDefault();
      run(async () => {
        const { source, path } = JSON.parse(raw);
        const rect = canvas.getElement().getBoundingClientRect();
        const zoom = optional(() => canvasContinerScale) || 1;
        const at = { x: (e.clientX - rect.left) / zoom, y: (e.clientY - rect.top) / zoom };
        const src = state.sources.find((s) => s.name === source);
        if (src && src.mode === 'free') return placeAsset(source, path, { mode: 'free', at });
        const panel = optional(() => findPanelUnderPoint(at.x, at.y, null));
        if (!panel) throw new Error('落とした位置にコマがありません');
        return placeAsset(source, path, { mode: 'panel', panel });
      });
    });
  }

  // ---- 保存と読み込み ----

  async function listSaved() {
    const data = await api('GET', '/api/manga/projects');
    state.saved = data.projects || [];
    state.mangaDir = data.dir || state.mangaDir;
    renderSaved();
    renderStatus();
    return state.saved;
  }

  // 全ページを束ねた保存データ（漫画エディタの「プロジェクトを保存」がダウンロードさせる物と同じ）
  async function buildProjectBlob() {
    if (stateStack.length === 0) throw new Error('保存する内容がありません');
    await btmSaveProjectFile(null, false);                    // いまのページを確定してページ一覧へ反映
    const blobs = Array.from(btmProjectsMap.values()).map((d) => d.blob);
    if (!blobs.length || blobs.some((b) => !b)) throw new Error('保存データの無いページがあります');
    return { blob: await lz4Compressor.mergeLz4Blobs(blobs), pages: blobs.length };
  }

  async function putProject(name) {
    const { blob, pages } = await buildProjectBlob();
    const info = await api('PUT', '/api/manga/project/' + encodeURIComponent(name), blob);
    return Object.assign(info, { pages });
  }

  function signature() {
    try {
      const idx = currentStateIndex;
      const cur = stateStack[idx];
      return [getCanvasGUID(), idx, stateStack.length, cur ? cur.length : 0, btmGetGuids().join(',')].join('|');
    } catch (e) { return null; }
  }
  function currentGuid() { return optional(() => getCanvasGUID()) || null; }

  function markClean() {
    state.autosave.lastSig = signature();
    state.autosave.guid = currentGuid();
    state.autosave.dirtySince = 0;
    state.autosave.pageChanged = false;
  }

  async function busyWhile(label, fn) {
    await waitIdle();
    state.busy = true;
    say(label, 'work');
    try {
      return await fn();
    } finally {
      state.busy = false;
    }
  }

  async function save(name) {
    requireFeature('save');
    const target = (name === undefined ? ui.name.value : name).trim();
    if (!validName(target)) {
      throw new Error(target === AUTOSAVE_NAME ? `「${AUTOSAVE_NAME}」は自動保存用の名前です。別の名前にしてください`
        : '保存名を入れてください（英数字・かな漢字・_ - で 64 文字まで）');
    }
    const info = await busyWhile('保存しています…', () => putProject(target));
    state.name = target;
    ui.name.value = target;
    store.set('lastName', target);
    optional(() => UnsavedGuard.markSaved());                  // 漫画エディタ側の「保存していない変更」の警告を解く
    markClean();
    await listSaved();
    say(`保存しました: ${target}（${info.pages} ページ） → ${info.path}`);
    return info;
  }

  let autosaving = null;                                       // 進行中の自動保存（無ければ null）
  function autosaveNow() {
    requireFeature('save');
    if (autosaving) return autosaving;
    if (state.busy || isProjectBusy()) return Promise.resolve(null);
    if (btmGetGuidsSize() <= 1 && getContentObjectCount() === 0) return Promise.resolve(null);   // 白紙1枚は保存しない
    state.busy = true;
    autosaving = (async () => {
      try {
        const info = await putProject(AUTOSAVE_NAME);
        state.autosave.lastAt = Date.now();
        markClean();
        renderStatus();
        listSaved().catch(() => {});
        return info;
      } finally {
        state.busy = false;
        autosaving = null;
      }
    })();
    return autosaving;
  }

  function contentExists() {
    return getContentObjectCount() > 0 || btmGetGuidsSize() > 1;
  }

  async function confirmReplace(label) {
    const message = `いま開いているページをすべて閉じて「${label}」を開きます。保存していない変更は失われます。`;
    const dialog = optional(() => showConfirmDialog);
    if (dialog) {
      return !!(await dialog({ title: 'プロジェクトから開く', message, okText: '開く', cancelText: 'やめる', danger: true }));
    }
    return window.confirm(message);
  }

  // 読み込み。漫画エディタの「プロジェクトを読み込む」（置き換え）と同じ手順
  async function open(name, opts) {
    requireFeature('open');
    const o = opts || {};
    if (!name || !NAME_RE.test(name)) throw new Error('開く漫画データを選んでください');
    await waitIdle();
    const label = name === AUTOSAVE_NAME ? '自動保存' : name;
    if (!o.force && contentExists() && !(await confirmReplace(label))) return null;
    const result = await busyWhile('読み込んでいます…', async () => {
      const res = await api('GET', '/api/manga/project/' + encodeURIComponent(name), undefined, true);
      const files = await lz4Compressor.unLz4FilesByBuffer(await res.arrayBuffer());
      clearAllProjectPages();
      const first = await multiLoadLz4(files);
      if (!first) throw new Error('ページが入っていません');
      await chengeCanvasByGuid(first);
      await btmWaitForPageReady();
      optional(() => btmUpdateHandleText());
      optional(() => UnsavedGuard.markSaved());
      return { name, pages: files.length, first };
    });
    if (name !== AUTOSAVE_NAME) {
      state.name = name;
      ui.name.value = name;
      store.set('lastName', name);
    }
    ui.resume.hidden = true;
    await nextFrames(2);
    markClean();
    say(`開きました: ${label}（${result.pages} ページ）`);
    return result;
  }

  // 自動保存: 変更に気づいてから一定時間後、またはページを切り替えたときに autosave へ保存する
  function autosaveTick() {
    const a = state.autosave;
    if (!state.ready || !state.features.save || !a.enabled || state.busy) return;
    if (isProjectBusy()) return;
    const sig = signature();
    if (sig === null) return;
    if (sig !== a.lastSig) {
      const guid = currentGuid();
      if (a.guid && guid !== a.guid) a.pageChanged = true;
      a.guid = guid;
      a.lastSig = sig;
      if (!a.dirtySince) a.dirtySince = Date.now();
    }
    if (!a.dirtySince) return;
    if (a.pageChanged || Date.now() - a.dirtySince >= a.seconds * 1000) {
      autosaveNow().catch((e) => {
        a.dirtySince = Date.now();                             // すぐには繰り返さない
        fail(e, '自動保存に失敗しました');
      });
    }
  }

  async function offerResume() {
    if (!state.features.open) return;
    const rows = await listSaved();
    const last = store.get('lastName', '');
    if (last && !ui.name.value) { ui.name.value = last; state.name = last; }
    const candidates = rows.filter((p) => p.name === AUTOSAVE_NAME || p.name === last);
    if (!candidates.length) return;
    const pick = candidates.sort((a, b) => b.mtime - a.mtime)[0];
    const label = pick.name === AUTOSAVE_NAME ? '自動保存' : pick.name;
    ui.resume.replaceChildren(
      el('div', { text: `前回の続きを開きますか？ ${label}（${formatTime(pick.mtime)}）` }),
      el('div', { class: 'ikb-row' }, [
        el('button', { type: 'button', class: 'ikb-primary', text: '開く', onclick: () => run(() => open(pick.name)) }),
        el('button', { type: 'button', text: '閉じる', onclick: () => { ui.resume.hidden = true; } }),
      ]));
    ui.resume.hidden = false;
    ui.resume.dataset.name = pick.name;
    setOpen(true, true);
  }

  // ---- 書き出し ----

  function exportName(name) {
    const target = (name === undefined ? (ui.name.value.trim() || state.name || 'manga') : name).trim();
    if (!NAME_RE.test(target)) throw new Error('書き出し先の名前（保存名）に使えない文字があります');
    return target;
  }

  // いま開いているページを PNG にする。漫画エディタの「画像を保存」と同じ（グリッドを消し、
  // 原稿サイズと DPI から決まる倍率で書き出し、グリッドを戻す）
  async function renderPagePng() {
    removeGrid();
    let size;
    let link;
    try {
      size = ImageUtil.getOutputPixelSize();
      link = getCropAndDownloadLinkByMultiplier(size.multiplier, 'png');
    } finally {
      if (optional(() => isGridVisible)) optional(() => { drawGrid(); isGridVisible = true; });
    }
    const blob = await (await fetch(link.href)).blob();
    // 実際の画素数は PNG の見出し（IHDR）から読む。計算上の大きさとは端数の丸めで 1px ずれることがある
    const head = new DataView(await blob.slice(0, 24).arrayBuffer());
    const real = head.byteLength >= 24 ? { width: head.getUint32(16), height: head.getUint32(20) } : size;
    return { blob, width: real.width, height: real.height };
  }

  async function postPage(name, number) {
    const png = await renderPagePng();
    const info = await api('POST', `/api/manga/pages/${encodeURIComponent(name)}/${number}.png`, png.blob);
    return Object.assign(info, { width: png.width, height: png.height });
  }

  async function exportPage(opts) {
    requireFeature('export');
    const o = opts || {};
    const name = exportName(o.name);
    const index = btmGetGuids().indexOf(getCanvasGUID());
    const number = o.number || (index >= 0 ? index + 1 : 1);
    const info = await busyWhile('書き出しています…', () => postPage(name, number));
    say(`書き出しました: ${info.width}×${info.height} px → ${info.path}`);
    return info;
  }

  async function exportAllPages(opts) {
    requireFeature('exportAll');
    const o = opts || {};
    const name = exportName(o.name);
    const loading = optional(() => OP_showLoading({ icon: 'process', step: 'inkdeus', substep: 'Export', progress: 0 }));
    try {
      const results = await busyWhile('全ページを書き出しています…', async () => {
        const start = getCanvasGUID();
        await btmSaveCurrentPage(false);                       // いまのページを確定してから離れる
        const guids = btmGetGuids();
        const out = [];
        try {
          for (let i = 0; i < guids.length; i++) {
            say(`書き出しています…（${i + 1} / ${guids.length}）`, 'work');
            if (getCanvasGUID() !== guids[i]) {
              await chengeCanvasByGuid(guids[i]);
              await btmWaitForPageReady();
              await nextFrames(2);
            }
            out.push(await postPage(name, i + 1));
          }
        } finally {
          if (getCanvasGUID() !== start && btmProjectsMap.has(start)) {
            await chengeCanvasByGuid(start);
            await btmWaitForPageReady();
            optional(() => btmUpdateHandleText());
          }
        }
        return out;
      });
      await nextFrames(2);
      markClean();
      const dir = results.length ? results[0].path.replace(/[\\/][^\\/]+$/, '') : '';
      say(`全 ${results.length} ページを書き出しました → ${dir}`);
      return results;
    } finally {
      if (loading !== undefined) optional(() => OP_hideLoading(loading));
    }
  }

  // ---- 起動 ----

  // 漫画エディタの準備が終わったか。起動アニメーションが外れて画面が出たあと、
  // キャンバスの作り直し（loadBookSize）が済んで、最初のページがページ一覧へ載るのを待つ
  function upstreamReady() {
    if (!has('canvas') || !has('fabric') || !has('btmProjectsMap') || !has('getCanvasGUID') || !has('isProjectBusy')) return false;
    if (!has('lz4Compressor') || !lz4Compressor.lz4Module) return false;
    const head = document.getElementById('head-id');
    if (!head || head.style.display === 'none') return false;
    if (isProjectBusy()) return false;
    return btmProjectsMap.has(getCanvasGUID());
  }

  async function waitUpstream() {
    const started = Date.now();
    let loadedAt = 0;
    while (Date.now() - started < READY_TIMEOUT_MS) {
      if (upstreamReady()) return true;
      if (document.readyState === 'complete' && !(has('canvas') && has('fabric'))) {
        // ページの読み込みは終わったのに、漫画エディタの土台が見当たらない（版が大きく違う）。長くは待たない
        loadedAt = loadedAt || Date.now();
        if (Date.now() - loadedAt > NO_EDITOR_GRACE_MS) return false;
      }
      await sleep(200);
    }
    return false;                                              // 版が違う・画面が小さすぎる等。使える機能だけで続ける
  }

  function checkFeatures() {
    const problems = [];
    Object.keys(FEATURES).forEach((feature) => {
      const missing = missingFor(feature);
      state.missing[feature] = missing;
      state.features[feature] = missing.length === 0;
      if (missing.length) problems.push(`${FEATURE_LABELS[feature]}（必要な関数: ${missing.join(', ')}）`);
    });
    return problems;
  }

  async function init() {
    try {
      const status = await api('GET', '/api/manga/status');
      state.project = status.project || null;
      state.mangaDir = status.dir || '';
      ui.projectName.textContent = state.project ? state.project.name : '';
      if (state.project) ui.projectName.title = 'プロジェクト: ' + state.project.name;
    } catch (e) {
      fail(e, 'inkdeus のサーバに接続できません');
    }
    state.source = store.get('source', null);
    refresh().catch((e) => fail(e, '素材の一覧を読めません'));
    renderStatus();

    const complete = await waitUpstream();
    const problems = checkFeatures();
    if (has('fabric')) {
      // 画像の src を「書かれたとおり」（相対 URL のまま）保存させる。fabric の設定で、既定は絶対 URL に直した物になる
      fabric.Image.prototype.srcFromAttribute = true;
    }
    state.ready = true;
    wireDrop();
    markClean();
    renderStatus();
    renderTarget();
    setInterval(renderTarget, 700);
    setInterval(autosaveTick, POLL_MS);
    if (problems.length) {
      say('対応していない版です。次の機能は使えません: ' + problems.join(' / '), 'error');
    } else if (!complete) {
      say('漫画エディタの準備の完了を確認できませんでした（画面が小さすぎるかもしれません）。動作が不安定なら再読み込みしてください', 'error');
    }
    try {
      await offerResume();
    } catch (e) {
      fail(e, '保存済みの漫画データの一覧を読めません');
    }
    return { ok: problems.length === 0 && complete, complete, features: Object.assign({}, state.features),
      missing: Object.assign({}, state.missing) };
  }

  buildPanel();

  window.inkdeusBridge = {
    version: 1,
    ready: init(),
    status: () => ({ ready: state.ready, busy: state.busy, features: Object.assign({}, state.features),
      missing: Object.assign({}, state.missing), project: state.project, mangaDir: state.mangaDir,
      name: state.name, source: state.source, imageMax: state.imageMax,
      autosave: { enabled: state.autosave.enabled, seconds: state.autosave.seconds,
        dirty: !!state.autosave.dirtySince, lastAt: state.autosave.lastAt } }),
    // 素材
    listSources, listAssets, refresh, selectSource, assetUrl,
    placeAsset, placeInPanel, placeFree,
    // コマ
    getTargetPanel: () => describe(getTargetPanel()),
    panels: () => panels().map(describe),
    objects: () => (has('canvas') ? canvas.getObjects().map(describe) : []),
    // 保存と読み込み
    listSaved, save, open, autosaveNow,
    setAutosave(opts) {
      const o = opts || {};
      if (typeof o.enabled === 'boolean') { state.autosave.enabled = o.enabled; ui.autosave.checked = o.enabled; }
      if (o.seconds >= 1) state.autosave.seconds = Number(o.seconds);
      renderStatus();
    },
    // 書き出し
    exportPage, exportAllPages,
    // 画面
    setOpen: (open) => setOpen(!!open),
    setImageMax(max) {
      if (!SIZES.some(([v]) => v === max)) throw new Error('大きさは 1024 / 2048 / 4096 / 0（原寸）のどれかです');
      state.imageMax = max;
      ui.size.value = String(max);
    },
  };
})();
