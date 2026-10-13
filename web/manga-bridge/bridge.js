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
     flushHistory, OP_showLoading, OP_hideLoading, waitNextFrame, withoutHistory, saveInitialState,
     fmFontData:writable, FontSelectorManager, collectUsedFontNames, usesFontFamily, projectFontData,
     AutoSaveManager, localforage */
  const G = {
    fabric: () => fabric,
    canvas: () => canvas,
    AutoSaveManager: () => AutoSaveManager,
    localforage: () => localforage,
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
  const AUTOSAVE_SECONDS = [10, 30, 60, 120];   // 変更から自動保存までの間隔の選択肢（既定 30 秒）
  const AUTOSAVE_DEFAULT = 30;
  const UPSTREAM_AUTOSAVE_KEY = 'inkdeus.manga.upstreamAutosave';   // "keep" なら、漫画エディタ自身の自動保存を止めない
  const NAME_RE = /^[\p{L}\p{N}_-]{1,64}$/u;
  const SIZES = [[1024, '長辺 1024'], [2048, '長辺 2048'], [4096, '長辺 4096'], [0, '原寸']];
  const PAGE_SIZE = 60;
  const READY_TIMEOUT_MS = 90000;
  const NO_EDITOR_GRACE_MS = 4000;          // 読み込みが終わってもキャンバスすら無いとき、別物と見なすまでの猶予
  const POLL_MS = 2000;
  const FREE_WIDTH_RATIO = 0.25;            // 自由配置する素材の既定の幅（ページ幅に対する割合）
  const PANEL_WIDTH = { def: 300, min: 220, maxRatio: 0.7 };
  const EDITOR_TAB = 'inkdeus-ink';         // 白黒化の画面を開くタブの名前（同じタブを使い回す）

  const state = {
    ready: false,
    features: {},                // 機能名 → 使えるか
    missing: {},                 // 機能名 → 欠けているグローバル
    project: null,               // inkdeus のプロジェクト（無ければ null）
    mangaDir: '',
    sources: [],
    source: null,                // 選択中のソース名
    folder: 'default',           // 元画像・白黒のタブで見ている画像フォルダ
    folders: [],                 // 画像フォルダの名前（元画像・白黒のソースから集める）
    q: '',
    items: [],
    total: 0,
    imageMax: 2048,
    name: '',                    // 保存名
    saved: [],                   // 保存済みの漫画データ
    busy: false,                 // 連携スクリプトが保存・読み込み・書き出しの最中
    autosave: { enabled: true, seconds: 30, lastSig: null, dirtySince: 0, pageChanged: false, lastAt: 0,
      failed: false, retryMs: 0, retryAt: 0 },                 // 失敗したら、間隔を広げながら再試行する
    // 漫画エディタ自身の自動保存（ブラウザの中 = IndexedDB への保存と、起動時の復元の確認）の扱い。
    // mode: pending = まだ決まっていない / stopped = 止めた（inkdeus の保存に一本化）/ kept = 利用者の設定で、そのまま /
    //       unavailable = 止められなかった（対応していない版）/ none = 自動保存の無い版。
    // old = ブラウザの中に残っている、以前の自動保存
    upstreamAuto: { mode: 'pending', old: null, original: null },
    build: null,                 // 読み込んだときのサーバのビルド識別（変わったら「再読み込みしてください」と知らせる）
    notices: {},                 // 画面の上の帯に出している知らせ（種類 → 文言）
    preferInked: true,           // 元画像のタブから入れるとき、白黒版があれば白黒版を入れる
    autoSwap: false,             // 白黒版ができたら、置いてある元画像を白黒版に置き換える
    inked: { versions: null, changed: new Set(), pairs: new Map(), names: new Map(), lastChanged: 0 },   // 白黒版の更新時刻と、開いてから変わった物
    events: { connected: false, opened: 0, received: 0, last: null },
    hot: { updated: 0, last: null, fallback: 0 },                       // 白黒版を差し替えた回数、既定へ切り替えた枚数
    // ローカルフォント（利用者のPCにあるフォント。inkdeus のサーバが配信する）
    fonts: { list: [], loaded: new Set(), loading: new Map(), failed: new Map(), registered: false, supported: true },
  };

  // ---- 漫画エディタ自身の自動保存を止める ----
  // 漫画エディタは、自分でも 60 秒ごとにブラウザの中（IndexedDB）へ全ページを保存し、次に開いたとき復元するかを
  // 全画面で尋ねる。inkdeus 経由ではプロジェクトの manga/autosave へ保存するので、二重になる。
  // 公開されている入口（AutoSaveManager の init / checkRecovery など）を、漫画エディタの起動処理が呼ぶ前に
  // 「何もしない物」へ差し替えて止める（漫画エディタのファイルは変えない。ブラウザの中に残っているデータも消さない）。
  // この連携スクリプトは </body> の直前で読み込まれ、漫画エディタの起動処理（DOMContentLoaded）より先に登録できる。
  function tameUpstreamAutosave() {
    const u = state.upstreamAuto;
    if (u.mode !== 'pending') return;
    let keep = false;
    try { keep = localStorage.getItem(UPSTREAM_AUTOSAVE_KEY) === 'keep'; } catch (e) { /* 読めなければ既定（止める） */ }
    const M = optional(() => AutoSaveManager);
    if (keep) { u.mode = 'kept'; return; }
    if (!M) { u.mode = 'none'; return; }                       // 自動保存の無い版（止める物が無い）
    if (typeof M.init !== 'function' || typeof M.checkRecovery !== 'function') { u.mode = 'unavailable'; return; }
    u.original = { init: M.init, checkRecovery: M.checkRecovery, save: M.save, clearAutoSave: M.clearAutoSave };
    optional(() => M.stop());                                  // もう始まっていたら止める
    M.init = async function () {};                             // タイマーを始めない（設定欄の変更も拾わない）
    M.start = function () {};
    M.restart = function () {};
    M.setEnabled = function () {};
    M.setInterval = function () {};
    M.save = async function () {};                             // IndexedDB へ書かない
    M.checkRecovery = async function () { return false; };     // 復元の確認を出さない（残っているデータはパネルで尋ねる）
    u.mode = 'stopped';
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', tameUpstreamAutosave);
  else tameUpstreamAutosave();

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
  // 画像の URL から、どのソースのどの画像かを読む（inkdeus の素材でなければ null）
  function parseAssetUrl(src) {
    if (typeof src !== 'string' || !src) return null;
    try {
      const u = new URL(src, location.origin);
      if (u.origin !== location.origin) return null;
      const m = u.pathname.match(/^\/api\/assets\/([^/]+)\/image\/(.+)$/);
      if (!m) return null;
      return { source: decodeURIComponent(m[1]), rel: m[2].split('/').map(decodeURIComponent).join('/'),
        max: parseInt(u.searchParams.get('max'), 10) || 0 };
    } catch (e) { return null; }
  }
  // 元画像・白黒のソースは画像フォルダごとにある（既定のフォルダは source / inked、追加のフォルダは source@<名前> / inked@<名前>）
  const DEFAULT_FOLDER = 'default';
  function kindOf(source) {
    const name = String(source || '');
    return name === 'source' || name.startsWith('source@') ? 'source'
      : name === 'inked' || name.startsWith('inked@') ? 'inked' : '';
  }
  function folderOfSource(source) {
    const at = String(source || '').indexOf('@');
    return at < 0 ? DEFAULT_FOLDER : String(source).slice(at + 1);
  }
  function srcName(kind, folder) { return !folder || folder === DEFAULT_FOLDER ? kind : `${kind}@${folder}`; }
  // 白黒版の更新時刻の表（state.inked.versions / changed）の見出し。既定のフォルダはファイル名のまま
  function ikey(source, rel) { return source === 'inked' ? rel : `${source}//${rel}`; }
  function withVersion(url, version) {
    return version ? url + (url.includes('?') ? '&' : '?') + 'v=' + version : url;
  }
  // 実際に読み込む URL。開いてから書き出し直された白黒版は、古い物をブラウザが使い回さないよう v を付ける
  function loadUrlFor(source, rel, canonical) {
    if (kindOf(source) === 'inked' && state.inked.changed.has(ikey(source, rel)) && state.inked.versions) {
      return withVersion(canonical, state.inked.versions[ikey(source, rel)]);
    }
    return canonical;
  }
  // 保存される src を「v なしの URL」に固定する（v はキャッシュ回避のためだけの物なので、漫画データや履歴には残さない）
  function pinSrc(obj, canonical, version) {
    obj.__ikbSrc = canonical;
    obj.__ikbVersion = version || null;
    obj.getSrc = function () { return this.__ikbSrc; };
  }

  function stem(rel) {
    const base = String(rel).split('/').pop();
    return base.replace(/\.[^.]+$/, '');
  }
  // バージョン（同じ元画像から作る、設定の違う白黒版）の表示名。'' は既定
  function verLabel(version) { return version ? String(version) : '既定'; }
  const VERSION_RE = /^[\p{L}\p{N}_-]{1,32}$/u;
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
    // 漫画エディタ自身の自動保存（ブラウザの中）に、以前のデータが残っているときの問いかけ
    ui.upstreamOld = el('div', { class: 'ikb-resume ikb-upstream-old', hidden: true });
    // 漫画エディタ自身の自動保存を止めていない（止められなかった）ときの案内
    ui.upstreamNote = el('div', { class: 'ikb-dim ikb-upstream-note', hidden: true });
    // 導入済みの漫画エディタに、未適用の差分（inkdeus が上流へ当てる変更）があるときの案内
    ui.patchNotice = el('div', { class: 'ikb-msg ikb-patch-notice', 'data-kind': 'error', hidden: true });
    ui.projectName = el('span', { class: 'ikb-project-name' });
    ui.tabs = el('div', { class: 'ikb-tabs', role: 'tablist' });
    // 画像フォルダが複数あるとき、元画像・白黒のタブで見るフォルダを選ぶ（タブをフォルダの数だけ増やさない）
    ui.folder = el('select', { class: 'ikb-folder', 'aria-label': '画像フォルダ', title: '元画像・白黒のタブで見る画像フォルダ',
      onchange: () => run(() => setFolder(ui.folder.value)) });
    ui.folderRow = el('label', { class: 'ikb-row ikb-folder-row', hidden: true }, [el('span', { text: 'フォルダ:' }), ui.folder]);
    ui.search = el('input', { type: 'search', class: 'ikb-search', placeholder: '名前で絞り込み', 'aria-label': '名前で絞り込み' });
    ui.size = el('select', { class: 'ikb-size', title: 'コマへ渡す画像の大きさ', 'aria-label': 'コマへ渡す画像の大きさ' },
      SIZES.map(([value, label]) => el('option', { value: String(value), text: label })));
    ui.reload = el('button', { type: 'button', class: 'ikb-icon', text: '↻', title: '素材の一覧を読み直す',
      'aria-label': '素材の一覧を読み直す', onclick: () => run(() => refresh()) });
    ui.target = el('div', { class: 'ikb-target' });
    ui.prefer = el('select', { class: 'ikb-prefer', 'aria-label': '入れる画像' }, [
      el('option', { value: '1', text: '白黒版があれば白黒' }), el('option', { value: '0', text: '常に元画像' })]);
    ui.preferRow = el('label', { class: 'ikb-row ikb-prefer-row', hidden: true }, [el('span', { text: '入れる画像:' }), ui.prefer]);
    ui.autoSwap = el('input', { type: 'checkbox', id: 'ikb-autoswap' });
    ui.autoSwapRow = el('div', { class: 'ikb-row ikb-autosave-row', hidden: true }, [
      ui.autoSwap, el('label', { for: 'ikb-autoswap', text: '白黒版ができたら元画像を置き換える',
        title: '元画像のまま置いてある絵を、白黒版が書き出されたときに白黒版へ切り替えます' })]);
    ui.selName = el('div', { class: 'ikb-sel-name' });
    ui.btnAdjust = el('button', { type: 'button', text: '白黒を調整', title: '白黒化の画面を別のタブで開きます',
      onclick: () => run(() => adjustSelected()) });
    ui.btnToInked = el('button', { type: 'button', text: '白黒版に差し替え', onclick: () => run(() => swapSelected('inked')) });
    ui.btnToSource = el('button', { type: 'button', text: '元画像に戻す', onclick: () => run(() => swapSelected('source')) });
    ui.selVer = el('div', { class: 'ikb-sel-ver', hidden: true });
    ui.verButtons = el('div', { class: 'ikb-row ikb-ver-row', hidden: true });
    ui.btnNewVer = el('button', { type: 'button', text: '新しいバージョンを作って調整', hidden: true,
      title: 'いま置いてあるバージョンを複製し、この絵を新しいバージョンへ切り替えて、白黒化の画面で開きます',
      onclick: () => run(() => newVersionAndAdjust()) });
    ui.sel = el('div', { class: 'ikb-sel', hidden: true }, [ui.selName, ui.selVer, ui.verButtons,
      el('div', { class: 'ikb-row' }, [ui.btnAdjust, ui.btnToInked, ui.btnToSource]),
      el('div', { class: 'ikb-row' }, [ui.btnNewVer])]);
    ui.float = el('button', { type: 'button', class: 'ikb-icon', text: '⧉', title: '重ねて表示／並べて表示（漫画エディタの画面を押しのけるかどうか）',
      'aria-label': '重ねて表示／並べて表示', 'aria-pressed': 'false', onclick: () => setFloat(!state.float) });
    ui.resize = el('div', { class: 'ikb-resize', title: 'ドラッグで幅を変える（ダブルクリックで既定の幅）', role: 'separator',
      'aria-orientation': 'vertical' });
    ui.grid = el('div', { class: 'ikb-grid' });
    ui.more = el('button', { type: 'button', class: 'ikb-more', text: 'さらに表示', hidden: true, onclick: () => loadItems(true) });
    ui.count = el('div', { class: 'ikb-count' });
    ui.name = el('input', { type: 'text', class: 'ikb-name', placeholder: '保存名', 'aria-label': '保存名', maxlength: '64' });
    ui.btnSave = el('button', { type: 'button', text: 'プロジェクトに保存', onclick: () => run(() => save()) });
    ui.saved = el('select', { class: 'ikb-saved', 'aria-label': '保存済みの漫画データ' });
    ui.btnOpen = el('button', { type: 'button', text: 'プロジェクトから開く', onclick: () => run(() => open(ui.saved.value)) });
    // 保存のたびに残している前の版（世代バックアップ）から開く
    ui.btnVersions = el('button', { type: 'button', class: 'ikb-versions-btn', text: '以前の版…', hidden: true,
      title: '選んだ保存データの、上書きされる前の版を開きます（いまの保存データは変わりません）',
      onclick: () => run(() => showBackups(ui.saved.value)) });
    ui.versions = el('div', { class: 'ikb-versions', hidden: true });
    ui.saveState = el('span', { class: 'ikb-save-state', title: '漫画データの保存の状態（自動保存）' });
    // 画面の上に出す、消えない知らせの帯（inkdeus の更新・サーバの再起動）
    ui.banner = el('div', { id: 'inkdeus-banner', role: 'status', hidden: true });
    ui.btnExport = el('button', { type: 'button', text: 'このページを書き出す', onclick: () => run(() => exportPage()) });
    ui.btnExportAll = el('button', { type: 'button', text: '全ページを書き出す', onclick: () => run(() => exportAllPages()) });
    ui.autosave = el('input', { type: 'checkbox', id: 'ikb-autosave' });
    ui.autosaveSeconds = el('select', { class: 'ikb-autosave-seconds', 'aria-label': '変更から自動保存までの間隔',
      title: '変更してから、この時間が経つと自動保存します（ページの切り替え・タブを離れたときは、待たずに保存します）' },
    AUTOSAVE_SECONDS.map((v) => el('option', { value: String(v), text: `${v} 秒` })));
    ui.autosaveState = el('span', { class: 'ikb-dim' });
    ui.dir = el('div', { class: 'ikb-dim ikb-dir' });
    ui.fontInfo = el('span', { class: 'ikb-dim ikb-font-info' });
    ui.fontRow = el('div', { class: 'ikb-row ikb-font-row' }, [ui.fontInfo,
      el('button', { type: 'button', class: 'ikb-icon', text: '↻', title: 'フォントの一覧を読み直す',
        'aria-label': 'フォントの一覧を読み直す', onclick: () => run(() => refreshFonts()) })]);

    ui.body = el('div', { class: 'ikb-body' }, [
      el('div', { class: 'ikb-head' }, [
        el('a', { class: 'ikb-link', href: '/', text: '← 白黒化の画面へ' }),
        ui.projectName,
        ui.float,
      ]),
      ui.msg,
      ui.patchNotice,
      ui.resume,
      ui.upstreamOld,
      ui.sel,
      el('div', { class: 'ikb-section ikb-assets' }, [
        ui.tabs,
        ui.folderRow,
        el('div', { class: 'ikb-row' }, [ui.search, ui.size, ui.reload]),
        ui.preferRow,
        ui.autoSwapRow,
        ui.target,
        ui.grid,
        ui.more,
        ui.count,
      ]),
      el('details', { class: 'ikb-section ikb-file', open: true }, [
        el('summary', { text: '保存と書き出し' }),
        el('div', { class: 'ikb-row' }, [ui.name, ui.btnSave]),
        el('div', { class: 'ikb-row' }, [ui.saved, ui.btnOpen, ui.btnVersions]),
        ui.versions,
        el('div', { class: 'ikb-row' }, [ui.btnExport, ui.btnExportAll]),
        el('div', { class: 'ikb-row ikb-autosave-row' }, [
          ui.autosave, el('label', { for: 'ikb-autosave', text: '自動保存' }), ui.autosaveSeconds, ui.autosaveState,
        ]),
        ui.upstreamNote,
        el('div', { class: 'ikb-row ikb-save-row' }, [el('span', { class: 'ikb-dim', text: '保存の状態:' }), ui.saveState]),
        ui.fontRow,
        ui.dir,
      ]),
    ]);
    ui.toggle = el('button', { type: 'button', class: 'ikb-toggle', title: '素材パネルを開く／閉じる', 'aria-expanded': 'true',
      onclick: () => setOpen(!ui.root.classList.contains('ikb-open')) }, [el('span', { text: '素材' })]);
    ui.fileSection = ui.body.querySelector('.ikb-file');
    ui.root = el('aside', { id: 'inkdeus-bridge', class: 'ikb', 'aria-label': 'inkdeus の素材パネル' },
      [ui.resize, ui.toggle, ui.body]);
    ui.saved.addEventListener('change', () => { ui.versions.hidden = true; renderStatus(); });

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
    ui.autosaveSeconds.addEventListener('change', () => {
      setAutosaveSeconds(parseInt(ui.autosaveSeconds.value, 10));
      store.set('autosaveSeconds', state.autosave.seconds);
      renderStatus();
    });

    ui.prefer.addEventListener('change', () => {
      state.preferInked = ui.prefer.value === '1';
      store.set('preferInked', state.preferInked);
    });
    ui.autoSwap.addEventListener('change', () => {
      state.autoSwap = ui.autoSwap.checked;
      store.set('autoSwap', state.autoSwap);
      if (state.autoSwap) queueSync();
    });
    state.preferInked = store.get('preferInked', true) !== false;
    ui.prefer.value = state.preferInked ? '1' : '0';
    state.autoSwap = store.get('autoSwap', false) === true;
    ui.autoSwap.checked = state.autoSwap;
    wireResize();
    state.imageMax = store.get('imageMax', 2048);
    if (!SIZES.some(([v]) => v === state.imageMax)) state.imageMax = 2048;
    ui.size.value = String(state.imageMax);
    state.autosave.enabled = store.get('autosave', true) !== false;
    ui.autosave.checked = state.autosave.enabled;
    const seconds = store.get('autosaveSeconds', AUTOSAVE_DEFAULT);
    setAutosaveSeconds(AUTOSAVE_SECONDS.includes(seconds) ? seconds : AUTOSAVE_DEFAULT);

    document.documentElement.classList.add('ikb-docked');
    document.body.appendChild(ui.root);
    setWidth(store.get('panelWidth', null), true);
    setFloat(store.get('panelFloat', false) === true, true);
    setOpen(store.get('panelOpen', true) !== false, true);
  }

  // ---- パネルの幅と置き方 ----

  function relayout() {
    // 画面の幅が変わるので、漫画エディタ側のキャンバスを合わせ直させる
    setTimeout(() => window.dispatchEvent(new Event('resize')), 0);
  }

  function clampWidth(px) {
    return Math.round(Math.min(Math.max(px, PANEL_WIDTH.min), Math.max(PANEL_WIDTH.min, window.innerWidth * PANEL_WIDTH.maxRatio)));
  }

  // 幅を変える（null で既定の幅）。漫画エディタ側の余白も同じ変数を見ているので一緒に動く
  function setWidth(px, quiet) {
    const width = px == null || !isFinite(px) ? null : clampWidth(Number(px));
    state.width = width === null ? PANEL_WIDTH.def : width;
    if (width === null) document.documentElement.style.removeProperty('--ikb-width');
    else document.documentElement.style.setProperty('--ikb-width', width + 'px');
    if (!quiet) store.set('panelWidth', width);
    return state.width;
  }

  function setFloat(on, quiet) {
    state.float = !!on;
    document.documentElement.classList.toggle('ikb-float', state.float);
    ui.float.setAttribute('aria-pressed', String(state.float));
    ui.float.classList.toggle('ikb-on', state.float);
    if (!quiet) { store.set('panelFloat', state.float); relayout(); }
  }

  function wireResize() {
    let dragging = false;
    let frame = 0;
    const handle = ui.resize;
    handle.addEventListener('pointerdown', (e) => {
      if (e.button !== 0) return;
      dragging = true;
      handle.setPointerCapture(e.pointerId);
      document.documentElement.classList.add('ikb-resizing');
      e.preventDefault();
    });
    handle.addEventListener('pointermove', (e) => {
      if (!dragging) return;
      const tab = ui.toggle.getBoundingClientRect().width;
      setWidth(window.innerWidth - e.clientX - tab, true);
      if (!frame) frame = requestAnimationFrame(() => { frame = 0; window.dispatchEvent(new Event('resize')); });
    });
    const end = () => {
      if (!dragging) return;
      dragging = false;
      document.documentElement.classList.remove('ikb-resizing');
      store.set('panelWidth', state.width);
      relayout();
    };
    handle.addEventListener('pointerup', end);
    handle.addEventListener('pointercancel', end);
    handle.addEventListener('dblclick', () => { setWidth(null); relayout(); });
    window.addEventListener('resize', () => {
      // 画面が狭くなったら、上限（画面幅の 70%）に収め直す
      if (state.width > clampWidth(state.width)) setWidth(state.width, true);
    });
  }

  function setOpen(open, quiet) {
    ui.root.classList.toggle('ikb-open', open);
    document.documentElement.classList.toggle('ikb-docked-open', open);
    ui.toggle.setAttribute('aria-expanded', String(open));
    if (!quiet) store.set('panelOpen', open);
    relayout();
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

  const KIND_LABELS = { source: '元画像', inked: '白黒' };
  function renderTabs() {
    // 元画像・白黒は、選んでいる画像フォルダの物だけをタブにする
    const many = state.folders.length > 1;
    const shown = state.sources.filter((src) => !src.kind || src.folder === state.folder);
    ui.tabs.replaceChildren(...shown.map((src) => el('button', {
      type: 'button', role: 'tab', class: 'ikb-tab' + (src.name === state.source ? ' ikb-active' : ''),
      'aria-selected': String(src.name === state.source), 'data-source': src.name,
      title: (src.mode === 'free' ? 'ページ上へ自由に置く素材' : 'コマにはめ込む画像') + (src.missing ? '（フォルダが見つかりません）' : ''),
      onclick: () => selectSource(src.name),
    }, [src.kind && many ? KIND_LABELS[src.kind] : src.label, el('span', { class: 'ikb-badge', text: String(src.count) })])));
    ui.folder.replaceChildren(...state.folders.map((name) => el('option', {
      value: name, text: name === DEFAULT_FOLDER ? '既定' : name })));
    ui.folder.value = state.folder;
    ui.folderRow.hidden = !(many && kindOf(state.source));
  }

  // 元画像・白黒のタブで見る画像フォルダを切り替える
  async function setFolder(name) {
    if (!state.folders.includes(name)) throw new Error(`画像フォルダが見つかりません: ${name}`);
    const kind = kindOf(state.source);
    state.folder = name;
    store.set('folder', name);
    if (kind) {
      state.source = srcName(kind, name);
      store.set('source', state.source);
    }
    renderTabs();
    renderTarget();
    await loadItems(false);
    return state.source;
  }

  function currentSource() {
    return state.sources.find((s) => s.name === state.source) || null;
  }

  function renderGrid() {
    const src = currentSource();
    const nodes = [];
    let group = null;
    const inkedTab = !!src && src.kind === 'inked';
    // 白黒のタブ: 同じ元画像のバージョンは隣り合って届く。何個あるかを先に数える
    const perBase = new Map();
    if (inkedTab) state.items.forEach((item) => { if (item.base) perBase.set(item.base, (perBase.get(item.base) || 0) + 1); });
    let row = null;                    // 同じ元画像のバージョンを横に並べる枠
    let rowBase = null;
    state.items.forEach((item) => {
      if (src && src.recursive && item.group !== group) {
        group = item.group;
        nodes.push(el('div', { class: 'ikb-group', text: group || '（直下）' }));
      }
      // 白黒の一覧では version がバージョン名、stamp が更新時刻（古いサーバは version に更新時刻を入れていた）
      const version = inkedTab && typeof item.version === 'string' ? item.version : '';
      const stamp = item.stamp || (typeof item.version === 'number' ? item.version : 0);
      const img = el('img', { loading: 'lazy', decoding: 'async', alt: item.name, draggable: 'false',
        src: withVersion(thumbUrl(src.name, item.path), stamp) });
      // 白黒化の画面で開ける画像（元画像と、その白黒版）には「調整」を付ける
      const editable = item.editable !== undefined ? item.editable : (src.kind === 'source' ? item.path : null);
      const adjust = src.builtin && editable ? el('button', {
        type: 'button', class: 'ikb-adjust', text: '調整',
        title: `白黒化の画面で ${editable}${version ? `（バージョン「${version}」）` : ''} を開く（別のタブ）`,
        onclick: (e) => { e.stopPropagation(); openEditor(editable, version, src.folder); },
        onkeydown: (e) => e.stopPropagation(),
      }) : null;
      const many = inkedTab && item.base && perBase.get(item.base) > 1;
      const label = inkedTab && (many || version)
        ? el('span', { class: 'ikb-ver', text: verLabel(version), title: `バージョン: ${verLabel(version)}` }) : null;
      const cell = el('div', {
        role: 'button', tabindex: '0', class: 'ikb-item' + (src.mode === 'free' ? ' ikb-free' : ''), draggable: 'true',
        title: `${item.path}（${item.width}×${item.height}）`,
        'data-path': item.path, 'data-version': inkedTab ? version : null, 'data-base': inkedTab ? (item.base || '') : null,
        onclick: () => run(() => placeAsset(src.name, item.path, uiPlaceOptions(src))),
        onkeydown: (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); e.currentTarget.click(); } },
        ondragstart: (e) => {
          e.dataTransfer.setData('application/x-inkdeus-asset', JSON.stringify({ source: src.name, path: item.path }));
          e.dataTransfer.effectAllowed = 'copy';
        },
      }, [img, el('span', { class: 'ikb-caption', text: many ? stem(item.base) : item.name })]
        .concat(label ? [label] : []).concat(adjust ? [adjust] : []));
      if (src.kind === 'source' && item.inked) cell.classList.add('ikb-has-inked');
      if (many) {
        if (!row || rowBase !== item.base) {
          rowBase = item.base;
          row = el('div', { class: 'ikb-vgroup', 'data-base': item.base, title: `${item.base} のバージョン` });
          nodes.push(row);
        }
        row.appendChild(cell);
      } else {
        row = null;
        rowBase = null;
        nodes.push(cell);
      }
    });
    if (!state.items.length) {
      const empty = !src ? '' : src.missing ? 'このソースのフォルダが見つかりません'
        : state.q ? '当てはまる画像がありません'
          : src.kind === 'inked' ? 'まだ書き出した画像がありません（白黒化の画面で「PNG を書き出す」）' : '画像がありません';
      nodes.push(el('div', { class: 'ikb-empty', text: empty }));
    }
    ui.grid.replaceChildren(...nodes);
    ui.preferRow.hidden = !(src && src.kind === 'source');
    ui.autoSwapRow.hidden = ui.preferRow.hidden;
    ui.more.hidden = state.items.length >= state.total;
    ui.count.textContent = state.total ? `${state.items.length} / ${state.total} 枚` : '';
  }

  // 自動保存までの間隔（秒）。選択肢に無い値（外からの指定）は、その場かぎりの選択肢として出す
  function setAutosaveSeconds(seconds) {
    if (!(seconds >= 1)) return;
    state.autosave.seconds = Number(seconds);
    const value = String(state.autosave.seconds);
    Array.from(ui.autosaveSeconds.options).filter((o) => o.dataset.custom).forEach((o) => o.remove());
    if (!Array.from(ui.autosaveSeconds.options).some((o) => o.value === value)) {
      const custom = el('option', { value, text: `${value} 秒` });
      custom.dataset.custom = '1';
      ui.autosaveSeconds.appendChild(custom);
    }
    ui.autosaveSeconds.value = value;
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
    ui.autosaveSeconds.disabled = !f.save || !state.autosave.enabled;
    const a = state.autosave;
    ui.autosaveState.textContent = !f.save ? '' : !a.enabled ? 'オフ'
      : a.lastAt ? `${formatTime(a.lastAt / 1000)} に保存` : `変更から ${a.seconds} 秒後`;
    ui.dir.textContent = state.mangaDir ? `保存先: ${state.mangaDir}` : '';
    ui.dir.title = state.mangaDir;
    const row = state.saved.find((p) => p.name === ui.saved.value);
    ui.btnVersions.hidden = !(f.open && hasServer('backup') && row && row.backups > 0);
    ui.btnVersions.disabled = !f.open;
    const status = saveStatus();
    ui.saveState.textContent = status.label;
    ui.saveState.dataset.state = status.state;
  }

  function hasServer(feature) { return (state.server || []).includes(feature); }

  // 保存の状態（自動保存）: 保存済み / 保存中… / 未保存の変更あり / 保存に失敗（再試行中）
  function saveStatus() {
    const a = state.autosave;
    if (!state.ready || !state.features.save) return { state: 'none', label: '' };
    if (a.failed) return { state: 'failed', label: '保存に失敗（再試行中）' };
    if (autosaving) return { state: 'saving', label: '保存中…' };
    if (a.dirtySince) return { state: 'dirty', label: a.enabled ? '未保存の変更あり' : '未保存の変更あり（自動保存はオフ）' };
    return { state: 'saved', label: '保存済み' };
  }

  // ---- 画面の上の知らせの帯（inkdeus の更新・サーバの再起動）----

  function notice(key, text, action) {
    if (!text) delete state.notices[key]; else state.notices[key] = { text, action: action || null };
    const rows = Object.entries(state.notices);
    ui.banner.hidden = rows.length === 0;
    ui.banner.replaceChildren(...rows.map(([k, n]) => el('div', { class: 'ikb-banner-row', 'data-key': k }, [
      el('span', { text: n.text }),
      n.action ? el('button', { type: 'button', text: n.action.label, onclick: () => n.action.run() }) : null,
    ].filter(Boolean))));
  }

  // 読み込んだときのサーバのビルド識別を覚える。この画面のファイルの方が新しければ、サーバの再起動を促す
  function checkBuild(build) {
    if (!build) {
      notice('server', 'この画面は、起動中の inkdeus のサーバより新しい版です。サーバを再起動してください');
      return;
    }
    state.build = build;
    if (build.stale) notice('server', 'サーバの起動後に inkdeus が更新されています。サーバを再起動してください');
  }

  // イベントの接続のたびに届く、サーバのビルド識別。読み込んだときと違えば、再読み込みを促す
  function onHello(ev) {
    const build = ev && ev.build;
    if (!build || !state.build) return;
    if (build.boot !== state.build.boot) {
      state.build = Object.assign({}, state.build, { boot: build.boot });
      if (state.autosave.failed) state.autosave.retryAt = 0;   // サーバが起動し直された。失敗していた保存をすぐ試す
    }
    if (build.id !== state.build.id) {
      notice('reload', 'inkdeus が更新されました。再読み込みしてください（保存していない変更は、先に保存されます）', {
        label: '保存して再読み込み',
        run: () => run(async () => {
          if (state.autosave.dirtySince || state.autosave.failed) {
            checkDirty();
            await autosaveNow();
            if (state.autosave.failed) throw new Error('保存できていない変更があります。サーバにつながってから再読み込みしてください');
          }
          optional(() => UnsavedGuard.markSaved());
          leaving = true;
          location.reload();
        }),
      });
    } else if (!build.stale) {
      notice('server', '');
    }
  }
  let leaving = false;                                         // 保存を済ませてから自分で再読み込みする（確認を出さない）

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
    // 古いサーバは kind / folder を返さない（そのときは既定のフォルダだけ）
    state.sources = (data.sources || []).map((src) => Object.assign({}, src, {
      kind: src.kind !== undefined ? src.kind : (src.builtin ? src.name : ''),
      folder: src.folder || (src.builtin ? DEFAULT_FOLDER : null) }));
    state.folders = state.sources.filter((src) => src.kind === 'source').map((src) => src.folder);
    if (!state.sources.some((src) => src.name === state.source)) {
      state.source = state.sources.length ? state.sources[0].name : null;
    }
    if (kindOf(state.source)) state.folder = folderOfSource(state.source);
    else if (!state.folders.includes(state.folder)) state.folder = DEFAULT_FOLDER;
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
    if (kindOf(name)) { state.folder = folderOfSource(name); store.set('folder', state.folder); }
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

  // 素材を fabric の画像として読み込む（保存される src は v なしの URL）
  async function loadAsset(source, rel, max) {
    const canonical = assetUrl(source, rel, max);
    const url = loadUrlFor(source, rel, canonical);
    const img = await loadFabricImage(url);
    if (url !== canonical) pinSrc(img, canonical, state.inked.versions[ikey(source, rel)]);
    return img;
  }

  function uiPlaceOptions(src) {
    return src && src.kind === 'source' && state.preferInked ? { inked: 'prefer' } : {};
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
    const img = await loadAsset(source, rel, max);
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
    const img = await loadAsset(source, rel, max);
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
    let label = '';
    if (o.inked === 'prefer' && kindOf(source) === 'source') {
      // 白黒版（書き出した PNG）があればそちらを入れる
      const pair = await pairOf(source, rel).catch(() => null);
      if (pair && pair.inked) { source = pair.inkedName || 'inked'; rel = pair.inked.path; label = '（白黒版）'; }
    }
    const result = mode === 'free' ? await placeFree(source, rel, o) : await placeInPanel(source, rel, o);
    say(mode === 'free' ? `ページに置きました: ${stem(rel)}${label}`
      : `コマに入れました: ${result.panel.name || 'コマ'} ← ${stem(rel)}${label}`);
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
        return placeAsset(source, path, Object.assign({ mode: 'panel', panel }, uiPlaceOptions(src)));
      });
    });
  }

  // ---- 白黒化の画面との行き来（別のタブで調整 → 書き出しの通知を受けて、使っている白黒版を差し替える） ----

  function openEditor(name, version, folder) {
    // 同じ名前のタブを使い回す（既に開いていれば、そのタブの画像が切り替わる）
    const win = window.open('/?' + (folder && folder !== DEFAULT_FOLDER ? 'folder=' + encodeURIComponent(folder) + '&' : '')
      + 'image=' + encodeURIComponent(name)
      + (version ? '&version=' + encodeURIComponent(version) : ''), EDITOR_TAB);
    say(win ? `白黒化の画面で開きました: ${name}${version ? `（バージョン「${version}」）` : ''}（調整すると、ここで使っている白黒版も替わります）`
      : 'タブを開けませんでした（ポップアップがブロックされていないか確かめてください）', win ? 'info' : 'error');
    return !!win;
  }

  // 元画像と白黒版の対応（どちらの名前からでも引ける）
  async function pairOf(source, rel) {
    const key = source + '|' + rel;
    if (!state.inked.pairs.has(key)) {
      state.inked.pairs.set(key, await api('GET', '/api/assets/' + assetPath(source, rel).replace('__KIND__', 'pair')));
    }
    return state.inked.pairs.get(key);
  }

  function loadHtmlImage(url) {
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => resolve(img);
      img.onerror = () => reject(new Error('画像を読み込めません: ' + url));
      img.src = url;
    });
  }

  function assetImages() {
    if (!has('canvas')) return [];
    return canvas.getObjects().filter((o) => o && o.type === 'image')
      .map((o) => ({ obj: o, ref: parseAssetUrl(optional(() => o.getSrc())) }))
      .filter((x) => x.ref && kindOf(x.ref.source));
  }

  // オブジェクトの絵だけを入れ替える。位置・角度・切り抜き・コマとの紐づけ・重なり順はそのままにし、
  // 画素数が違う画像に替わったときは、見た目の大きさが変わらないよう拡大率を直す
  async function replaceElement(obj, source, rel, version) {
    const ref = parseAssetUrl(obj.getSrc());
    const canonical = assetUrl(source, rel, ref ? ref.max : state.imageMax);
    const element = await loadHtmlImage(withVersion(canonical, version));
    if (canvas.getObjects().indexOf(obj) === -1) return false;          // 読み込みの間に消された・ページが替わった
    note('replace', { source, rel, version: version || null });
    const before = { w: obj.width, h: obj.height, sx: obj.scaleX, sy: obj.scaleY };
    const apply = () => {
      obj.setElement(element);
      pinSrc(obj, canonical, version);
      if (obj.width !== before.w || obj.height !== before.h) {
        obj.scaleX = before.sx * (before.w / obj.width);
        obj.scaleY = before.sy * (before.h / obj.height);
        if (obj.initial) optional(() => saveInitialState(obj));         // 画面に合わせ直すときの基準も更新する
      }
      obj.dirty = true;
      obj.setCoords();
    };
    const quiet = optional(() => withoutHistory);
    if (quiet) quiet(apply); else apply();
    return true;
  }

  // いま開いているページを見直す:
  //   - 開いてから書き出し直された白黒版を使っている絵を、新しい画像に替える（履歴には積まない）
  //   - 「白黒版ができたら元画像を置き換える」がオンなら、白黒版のある元画像を白黒版に切り替える（履歴に1件）
  // 取り消し・やり直し・ページの切り替えのあと（オブジェクトが作り直されたあと）にも呼ばれる
  async function syncPage() {
    if (!state.ready || !has('canvas') || !state.inked.versions) return { refreshed: 0, swapped: 0, fallback: 0 };
    let refreshed = 0;
    let swapped = 0;
    const lost = [];
    const names = new Set();                                   // 差し替えた絵の、元画像の名前
    const nameOf = (source, rel) => state.inked.names.get(ikey(source, rel)) || rel;
    for (const { obj, ref } of assetImages()) {
      try {
        const changedAt = (src, path, stamp) => (state.inked.changed.has(ikey(src, path)) ? stamp : null);
        if (kindOf(ref.source) === 'inked') {
          const version = state.inked.versions[ikey(ref.source, ref.rel)];
          if (!version) {
            // その白黒版が無い（消された・名前の変わったバージョン）。既定のバージョン、無ければ元画像へ切り替える
            const pair = await pairOf(ref.source, ref.rel).catch(() => null);
            if (pair && pair.missing) {
              const done = pair.default
                ? await replaceElement(obj, ref.source, pair.default.path, changedAt(ref.source, pair.default.path, pair.default.version))
                : pair.source ? await replaceElement(obj, pair.sourceName || 'source', pair.source.path, null) : false;
              if (done) lost.push(`${stem(pair.name)}「${verLabel(pair.version)}」→ ${pair.default ? '既定' : '元画像'}`);
            }
          } else if (state.inked.changed.has(ikey(ref.source, ref.rel)) && obj.__ikbVersion !== version) {
            if (await replaceElement(obj, ref.source, ref.rel, version)) { refreshed += 1; names.add(nameOf(ref.source, ref.rel)); }
          }
        } else if (state.autoSwap) {
          const pair = await pairOf(ref.source, ref.rel);
          const inkedSrc = pair && (pair.inkedName || 'inked');
          if (pair && pair.inked && await replaceElement(obj, inkedSrc, pair.inked.path,
            changedAt(inkedSrc, pair.inked.path, pair.inked.version))) { swapped += 1; names.add(pair.name || ref.rel); }
        }
      } catch (e) {
        console.error('[inkdeus]', e);
      }
    }
    if (refreshed || swapped || lost.length) {
      canvas.requestRenderAll();
      optional(() => updateLayerPanel());
      if (swapped || lost.length) optional(() => saveStateByManual());
    }
    if (refreshed || swapped) {
      // どのきっかけ（書き出しの通知・つなぎ直し・オブジェクトの作り直し）で見直したときでも、差し替えたここで数える。
      // 通知を受けた側で数えると、先に走っていた別の見直しが差し替えを済ませたときに数え漏らす
      state.hot.updated += 1;
      state.hot.last = Array.from(names).pop() || null;
      say(`${Array.from(names).join('、')} を更新しました`);
    }
    if (lost.length) {
      state.hot.fallback += lost.length;
      say(`見つからないバージョンを使っていた絵を切り替えました: ${lost.join('、')}`, 'error');
      renderSelection(true);
    }
    return { refreshed, swapped, fallback: lost.length };
  }

  let syncChain = Promise.resolve();
  let syncTimer = 0;
  let syncWaiters = [];
  function queueSync(delay) {
    // 予約を取り直すと前のタイマーは捨てられる。前に待っていた相手も、次に走る見直しの結果で起こす
    // （捨てたタイマーの約束が永久に終わらないと、それを待つ処理が止まったままになる）
    clearTimeout(syncTimer);
    return new Promise((resolve) => {
      syncWaiters.push(resolve);
      syncTimer = setTimeout(() => {
        const waiters = syncWaiters;
        syncWaiters = [];
        syncChain = syncChain.then(async () => {
          for (let i = 0; i < 100 && has('isProjectBusy') && isProjectBusy(); i++) await sleep(100);
          return syncPage();
        }).catch((e) => { console.error('[inkdeus]', e); return { refreshed: 0, swapped: 0, fallback: 0 }; });
        syncChain.then((result) => waiters.forEach((wake) => wake(result)));
      }, delay === undefined ? 150 : delay);
    });
  }

  // 白黒版の更新時刻を読み直す。前に読んだ時から変わっていた物は「開いてから変わった物」に加える
  async function refreshVersions() {
    // 白黒のソースは画像フォルダごとにある。全部の更新時刻を1つの表にまとめる
    const names = state.sources.filter((x) => x.kind === 'inked').map((x) => x.name);
    if (!names.includes('inked')) names.unshift('inked');
    const now = {};
    for (const name of names) {
      let data;
      try {
        data = await api('GET', `/api/assets/${encodeURIComponent(name)}/versions`);
      } catch (e) {
        if (name === 'inked') return false;                     // 古いサーバ（この API が無い）。差し替えは行わない
        continue;
      }
      Object.entries(data.versions || {}).forEach(([rel, stamp]) => { now[ikey(name, rel)] = stamp; });
    }
    const old = state.inked.versions;
    let changed = 0;
    if (old) {
      Object.keys(now).forEach((key) => { if (old[key] !== now[key]) { state.inked.changed.add(key); changed += 1; } });
    }
    state.inked.versions = now;
    state.inked.lastChanged = changed;
    return true;
  }

  // 白黒化の画面（別のタブ）や一括書き出しが PNG を保存したとき
  async function onExported(ev) {
    if (!ev || !ev.file) return;
    state.events.received += 1;
    if (!state.inked.versions) state.inked.versions = {};
    // stamp が PNG の更新時刻（古いサーバは version に入れていた。いまの version はバージョン名）
    const key = ikey(ev.source || 'inked', ev.file);          // source = その画像フォルダの白黒のソース名
    state.inked.versions[key] = ev.stamp || (typeof ev.version === 'number' ? ev.version : Date.now());
    state.inked.changed.add(key);
    if (ev.name) state.inked.names.set(key, ev.name);          // 知らせに出す名前（元画像の名前）
    state.inked.pairs.clear();
    const viewing = !!kindOf(state.source);
    listSources().then(() => (viewing ? loadItems(false) : null)).catch(() => {});
    await queueSync(0);                                         // 差し替えた回数と知らせは syncPage が出す
    renderSelection(true);
  }

  // バージョンが作られた・名前が変わった・消された（白黒化の画面や ctl から）
  async function onVersionsEvent(ev) {
    state.events.received += 1;
    await refreshVersions();
    state.inked.pairs.clear();
    const inkedSrc = srcName('inked', ev.folder);
    if (ev.action === 'rename' && ev.file && ev.toFile && has('canvas')) {
      // 開いているページで使っている絵を、新しい名前のファイルへ付け替える
      let moved = 0;
      for (const { obj, ref } of assetImages()) {
        if (ref.source === inkedSrc && ref.rel === ev.file && await replaceElement(obj, inkedSrc, ev.toFile, null)) moved += 1;
      }
      if (moved) {
        canvas.requestRenderAll();
        optional(() => updateLayerPanel());
        optional(() => saveStateByManual());
        say(`バージョンの名前の変更に合わせました: ${ev.version} → ${ev.to}`);
      }
    }
    await queueSync(0);                                         // 消されたバージョンを使っている絵は既定へ
    const viewing = !!kindOf(state.source);
    listSources().then(() => (viewing ? loadItems(false) : null)).catch(() => {});
    renderSelection(true);
  }

  // 直近の出来事の控え（接続・受けた通知・差し替え）。不具合を追うときに window.inkdeusBridge.debug() で読む
  const trail = [];
  function note(kind, detail) {
    trail.push({ at: Date.now(), kind, detail: detail === undefined ? null : detail });
    if (trail.length > 200) trail.shift();
    if (kind === 'event') state.events.last = { at: Date.now(), type: detail && detail.type, name: detail && detail.name,
      file: detail && detail.file, stamp: detail && detail.stamp };
  }

  let eventSource = null;
  function connectEvents() {
    if (typeof EventSource === 'undefined') return;
    if (eventSource) eventSource.close();
    state.events.connected = false;
    // ?role=manga: 聞くだけの相手として接続する（白黒化の画面への指示は実行しない）
    eventSource = new EventSource('/api/events?role=manga');
    eventSource.onopen = () => {
      const again = state.events.opened > 0;
      note('open', { again });
      state.events.connected = true;
      state.events.opened += 1;
      // つながった（つながり直した）時点で、白黒版の更新時刻を必ず突き合わせる。
      // 最初の接続でも行う: 起動時に更新時刻を読んでから購読が始まるまでの間に書き出された分は、通知が届かない
      refreshVersions().then(() => {
        if (!again && !state.inked.lastChanged) return null;     // 最初の接続で、取りこぼしが無ければ何もしない
        note('reconcile', { again, changed: state.inked.lastChanged });
        state.inked.pairs.clear();
        return (again ? refresh() : Promise.resolve()).then(() => queueSync(0));
      }).catch((e) => console.error('[inkdeus]', e));
    };
    eventSource.onerror = () => { state.events.connected = false; note('error', { readyState: eventSource.readyState }); };      // 再接続はブラウザが自動で行う
    eventSource.onmessage = (msg) => {
      let ev;
      try { ev = JSON.parse(msg.data); } catch (e) { return; }
      note('event', ev);
      if (ev.type === 'hello') onHello(ev);
      else if (ev.type === 'exported') onExported(ev).catch((e) => console.error('[inkdeus]', e));
      else if (ev.type === 'versions') onVersionsEvent(ev).catch((e) => console.error('[inkdeus]', e));
      else if (ev.type === 'manga-command') runCommand(ev);
    };
  }

  // ---- 外部（inkdeus ctl manga …）からの指示。追加の部品（pdf.js など）が commands に受け口を登録する ----

  const commands = {};
  async function runCommand(ev) {
    const post = (body) => api('POST', '/api/manga/result/' + encodeURIComponent(ev.id), JSON.stringify(body))
      .catch((e) => console.error('[inkdeus]', e));
    const fn = commands[ev.command];
    if (!fn) { await post({ state: 'error', error: `この画面は指示「${ev.command}」に対応していません` }); return; }
    await post({ state: 'running' });
    try {
      const result = await fn(ev.args || {}, (progress) => post({ state: 'running', progress }));
      await post({ state: 'done', result: result === undefined ? null : result });
    } catch (e) {
      console.error('[inkdeus]', e);
      await post({ state: 'error', error: e && e.message ? e.message : String(e) });
    }
  }

  // ---- 選択中の画像（inkdeus の素材なら、調整・差し替えのボタンを出す） ----

  function selectedAsset() {
    if (!has('canvas')) return null;
    const obj = canvas.getActiveObject();
    if (!obj || obj.type !== 'image') return null;
    const ref = parseAssetUrl(optional(() => obj.getSrc()));
    return ref && kindOf(ref.source) ? { obj, ref } : null;
  }

  let selKey = '';
  function renderSelection(force) {
    const sel = state.ready ? selectedAsset() : null;
    const key = sel ? sel.ref.source + '|' + sel.ref.rel : '';
    if (key === selKey && !force) return;
    selKey = key;
    ui.sel.hidden = !sel;
    if (!sel) return;
    const selKind = kindOf(sel.ref.source);
    const selFolder = folderOfSource(sel.ref.source);
    ui.selName.textContent = `選択中の画像: ${stem(sel.ref.rel)}（${selKind === 'inked' ? '白黒版' : '元画像'}`
      + `${selFolder === DEFAULT_FOLDER ? '' : '・' + selFolder}）`;
    ui.btnToInked.hidden = selKind !== 'source';
    ui.btnToSource.hidden = selKind !== 'inked';
    ui.btnAdjust.disabled = false;
    ui.btnToSource.disabled = false;
    ui.selVer.hidden = true;
    ui.verButtons.hidden = true;
    ui.btnNewVer.hidden = true;
    pairOf(sel.ref.source, sel.ref.rel).then((pair) => {
      if (selKey !== key) return;
      ui.btnAdjust.disabled = !pair.name;
      ui.btnToSource.disabled = !pair.source;
      if (pair.name) ui.selName.title = pair.name;
      // バージョン: いま置いてある物の名前と、同じ元画像の別バージョンへ切り替えるボタン
      const inked = selKind === 'inked' && Array.isArray(pair.versions);     // 古いサーバには versions が無い
      const others = inked ? pair.versions.filter((v) => v.path !== sel.ref.rel) : [];
      ui.selVer.hidden = !inked;
      ui.selVer.textContent = `バージョン: ${verLabel(pair.version)}`;
      ui.verButtons.replaceChildren(el('span', { class: 'ikb-dim', text: '切り替え:' }), ...others.map((v) => el('button', {
        type: 'button', class: 'ikb-ver-btn', 'data-version': v.name, text: verLabel(v.name),
        title: `この絵をバージョン「${verLabel(v.name)}」へ切り替える（位置・大きさ・切り抜きはそのまま）`,
        onclick: () => run(() => switchVersion(v.name)),
      })));
      ui.verButtons.hidden = !others.length;
      ui.btnNewVer.hidden = !inked || !pair.name;
    }).catch(() => {});
  }

  async function adjustSelected() {
    const sel = selectedAsset();
    if (!sel) throw new Error('inkdeus の画像が選ばれていません');
    const pair = await pairOf(sel.ref.source, sel.ref.rel);
    if (!pair.name) throw new Error('この白黒版の元画像が見つかりません');
    return openEditor(pair.name, kindOf(sel.ref.source) === 'inked' ? (pair.version || '') : '', pair.folder);
  }

  function selectedInked() {
    const sel = selectedAsset();
    if (!sel || kindOf(sel.ref.source) !== 'inked') throw new Error('白黒版の画像が選ばれていません');
    return sel;
  }

  // 選択中の絵を、同じ元画像の別バージョンへ切り替える（位置・見た目の大きさ・切り抜き・コマとの紐づけは保つ。取り消せる）
  async function switchVersion(version) {
    requireFeature('place');
    const sel = selectedInked();
    await waitIdle();
    state.inked.pairs.delete(sel.ref.source + '|' + sel.ref.rel);
    const pair = await pairOf(sel.ref.source, sel.ref.rel);
    const target = (pair.versions || []).find((v) => v.name === (version || ''));
    if (!target) throw new Error(`バージョン「${verLabel(version)}」の白黒版がありません（白黒化の画面で開くと書き出されます）`);
    if (target.path === sel.ref.rel) return { ok: true, changed: false, version: target.name, src: sel.obj.getSrc() };
    const done = await replaceElement(sel.obj, sel.ref.source, target.path,
      state.inked.changed.has(ikey(sel.ref.source, target.path)) ? target.version : null);
    if (done) {
      canvas.requestRenderAll();
      updateLayerPanel();
      saveStateByManual();                                   // 絵の内容の変更なので、取り消せるよう履歴に積む
      say(`バージョンを切り替えました: ${stem(pair.name || sel.ref.rel)} → ${verLabel(target.name)}`);
    }
    renderSelection(true);
    return { ok: !!done, changed: !!done, version: target.name, object: describe(sel.obj), src: sel.obj.getSrc() };
  }

  // いま置いてあるバージョンを複製 → 置いてある絵を新しいバージョンへ切り替え → 白黒化の画面で開く
  async function newVersionAndAdjust(name, opts) {
    requireFeature('place');
    const sel = selectedInked();
    const pair = await pairOf(sel.ref.source, sel.ref.rel);
    if (!pair.name || !Array.isArray(pair.versions)) throw new Error('この白黒版の元画像が見つかりません');
    let version = name;
    if (version === undefined) {
      version = window.prompt('新しいバージョンの名前（英数字・かな漢字・_ - で 32 文字まで）。'
        + `「${verLabel(pair.version)}」の設定を複製します`, '');
      if (version === null || !version.trim()) return null;
    }
    version = String(version).trim();
    if (!VERSION_RE.test(version)) throw new Error('バージョン名に使えるのは英数字・かな漢字・_ - だけです（1〜32 文字）');
    await waitIdle();
    const made = await api('POST', '/api/versions/' + encodeURIComponent(pair.name)
      + (pair.folder && pair.folder !== DEFAULT_FOLDER ? '?folder=' + encodeURIComponent(pair.folder) : ''),
      JSON.stringify({ version, copyFrom: pair.version || '', copyExport: true }));
    await refreshVersions();
    state.inked.pairs.clear();
    if (!made.copiedExport) throw new Error('複製元の白黒版が見つからないため、絵は切り替えていません（バージョンは作りました）');
    const done = await replaceElement(sel.obj, sel.ref.source, made.file, null);
    if (done) {
      canvas.requestRenderAll();
      updateLayerPanel();
      saveStateByManual();
    }
    renderSelection(true);
    const opened = (opts && opts.open === false) ? false : openEditor(pair.name, version, pair.folder);
    return { ok: !!done, version, file: made.file, opened, src: sel.obj.getSrc() };
  }

  // 選択中の絵を、白黒版 ⇔ 元画像で差し替える（位置・見た目の大きさ・切り抜き・コマとの紐づけは保つ）
  async function swapSelected(to) {
    requireFeature('place');
    const sel = selectedAsset();
    if (!sel) throw new Error('inkdeus の画像が選ばれていません');
    await waitIdle();
    const pair = await pairOf(sel.ref.source, sel.ref.rel);
    let done;
    if (to === 'inked') {
      if (kindOf(sel.ref.source) === 'inked') return { ok: true, changed: false };
      if (!pair.inked) {
        const err = new Error('白黒版がまだありません。「白黒を調整」で白黒化の画面を開くと、自動で書き出されます');
        err.code = 'no-inked';
        throw err;
      }
      const inkedSrc = pair.inkedName || 'inked';
      done = await replaceElement(sel.obj, inkedSrc, pair.inked.path,
        state.inked.changed.has(ikey(inkedSrc, pair.inked.path)) ? pair.inked.version : null);
    } else {
      if (kindOf(sel.ref.source) === 'source') return { ok: true, changed: false };
      if (!pair.source) throw new Error('元画像が見つかりません');
      done = await replaceElement(sel.obj, pair.sourceName || 'source', pair.source.path, null);
    }
    if (done) {
      canvas.requestRenderAll();
      updateLayerPanel();
      saveStateByManual();                                   // これは絵の内容の変更なので、取り消せるよう履歴に積む
      say(to === 'inked' ? `白黒版に差し替えました: ${stem(sel.ref.rel)}` : `元画像に戻しました: ${stem(sel.ref.rel)}`);
    }
    renderSelection(true);
    return { ok: !!done, changed: !!done, object: describe(sel.obj), src: sel.obj.getSrc() };
  }

  // ---- ローカルフォント ----
  // 漫画エディタのフォント選択は fmFontData（分類 → フォントの並び）から作られる。そこへ「inkdeus」の分類を足す。
  // フォント本体は @font-face（/api/fonts/css）で宣言するだけにして、使われたときに初めて読み込む
  // （数十MB の日本語フォントを起動時に全部は読まない）。漫画エディタ側の IndexedDB には何も保存しない。

  function renderFonts() {
    const f = state.fonts;
    const failed = Array.from(f.failed.keys());
    ui.fontInfo.textContent = !f.supported ? 'フォント: 対応していない版です'
      : !f.list.length ? 'フォント: 登録なし'
        : `フォント: ${f.list.length} 個` + (failed.length ? `（読み込み失敗: ${failed.join('、')}）` : '');
    ui.fontInfo.title = f.list.map((x) => `${x.name}（${x.file}）`).join('\n');
    ui.fontInfo.dataset.kind = failed.length ? 'error' : 'ok';
  }

  function loadStylesheet(href) {
    return new Promise((resolve, reject) => {
      const old = document.getElementById('ikb-fonts');
      const link = el('link', { rel: 'stylesheet', href });
      link.onload = () => { if (old) old.remove(); link.id = 'ikb-fonts'; resolve(); };
      link.onerror = () => { link.remove(); reject(new Error('フォントの定義を読み込めません')); };
      document.head.appendChild(link);
    });
  }

  async function refreshFonts() {
    const f = state.fonts;
    let rows = [];
    try {
      rows = await api('GET', '/api/fonts');
    } catch (e) {
      rows = [];                                               // 古いサーバ（この API が無い）
    }
    f.list = Array.isArray(rows) ? rows : [];
    f.failed.clear();
    if (f.list.length || f.registered) {
      const data = optional(() => fmFontData);
      f.supported = !!data && typeof data === 'object';
      if (f.supported) {
        if (f.list.length) await loadStylesheet('/api/fonts/css?t=' + Date.now());
        const entries = f.list.map((x) => ({ name: x.name, type: 'web', url: x.css }));
        const rest = Object.assign({}, data);
        delete rest.inkdeus;
        // 見つけやすいよう先頭の分類にする。プレビュー用の書体指定（.fm-font-…）は足さないので、
        // 選択の一覧を開いただけでは本体を読みに行かない（名前だけが並ぶ）
        fmFontData = f.list.length ? Object.assign({ inkdeus: { color: '#ff9532', fonts: entries } }, rest) : rest;
        optional(() => FontSelectorManager.reloadAll());
        f.registered = true;
      }
    }
    renderFonts();
    fontTick();
    return f.list;
  }

  function fontUsedBy(obj, name) {
    const check = optional(() => usesFontFamily);
    return check ? check(obj, name) : obj.fontFamily === name;
  }

  // フォント本体を読み込み、それを使っている文字を描き直す（読み込み前は代わりの書体で描かれているため）
  function ensureFont(name) {
    const f = state.fonts;
    if (f.loaded.has(name)) return Promise.resolve(true);
    if (f.loading.has(name)) return f.loading.get(name);
    const job = document.fonts.load(`32px "${name}"`, 'あA').then((faces) => {
      if (!faces.length) throw new Error('フォントが見つかりません');
      f.loaded.add(name);
      f.failed.delete(name);
      if (has('canvas')) {
        optional(() => fabric.util.clearFabricFontCache(name));
        const redo = () => canvas.getObjects().forEach((obj) => {
          if (!fontUsedBy(obj, name)) return;
          obj.dirty = true;
          if (typeof obj.initDimensions === 'function') obj.initDimensions();
          obj.setCoords();
        });
        const quiet = optional(() => withoutHistory);
        if (quiet) quiet(redo); else redo();
        canvas.requestRenderAll();
        optional(() => updateLayerPanel());
      }
      return true;
    }).catch((e) => {
      console.error('[inkdeus]', name, e);
      f.failed.set(name, e.message || String(e));
      return false;
    }).finally(() => { f.loading.delete(name); renderFonts(); });
    f.loading.set(name, job);
    return job;
  }

  function usedLocalFonts() {
    const used = optional(() => collectUsedFontNames());
    if (!used) return [];
    return state.fonts.list.map((x) => x.name).filter((name) => used.has(name));
  }

  // 定期的に呼ぶ: いまのページで使われているフォントを読み込み、漫画データに「どこから取れるか」を記録させる
  function fontTick() {
    const f = state.fonts;
    if (!f.list.length || !f.supported) return;
    const carried = optional(() => projectFontData);
    if (carried && typeof carried.set === 'function') {
      // 保存される漫画データ（fonts.json）に {name, type, url} が入るようにする。URL は相対なのでポートが変わっても使える
      f.list.forEach((x) => { if (!carried.has(x.name)) carried.set(x.name, { name: x.name, type: 'web', url: x.css }); });
    }
    usedLocalFonts().forEach((name) => { if (!f.loaded.has(name) && !f.failed.has(name)) ensureFont(name); });
  }

  // いまのページで使っているフォントが読み込み終わるまで待つ（書き出しの前に呼ぶ）
  async function ensurePageFonts() {
    await Promise.all(usedLocalFonts().map((name) => ensureFont(name)));
    if (has('canvas')) canvas.renderAll();
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
      const a = state.autosave;
      try {
        renderStatus();
        const info = await putProject(AUTOSAVE_NAME);
        a.lastAt = Date.now();
        if (a.failed) say('自動保存できました（サーバにつながりました）');
        Object.assign(a, { failed: false, retryMs: 0, retryAt: 0 });
        markClean();
        listSaved().catch(() => {});
        return info;
      } catch (e) {
        // 変更は画面が持ったまま。間隔を広げながら再試行する（2 秒 → 4 秒 → … → 60 秒）
        if (!a.failed) fail(e, '自動保存に失敗しました（変更はこの画面が持っています。つながり次第、保存します）');
        a.failed = true;
        a.retryMs = Math.min(60000, Math.max(2000, a.retryMs * 2));
        a.retryAt = Date.now() + a.retryMs;
        if (!a.dirtySince) a.dirtySince = Date.now();
        return null;
      } finally {
        state.busy = false;
        autosaving = null;
        renderStatus();
      }
    })();
    return autosaving;
  }

  // ---- 以前の版（保存のたびに残している、上書きされる前の漫画データ）----

  async function listBackups(name) {
    if (!name || !NAME_RE.test(name)) throw new Error('漫画データを選んでください');
    return (await api('GET', '/api/manga/backups/' + encodeURIComponent(name))).backups || [];
  }

  async function showBackups(name) {
    const rows = await listBackups(name);
    const label = name === AUTOSAVE_NAME ? '自動保存' : name;
    ui.versions.replaceChildren(
      el('div', { class: 'ikb-dim', text: rows.length ? `「${label}」の以前の版（新しい順。開くだけで、いまの保存データは変わりません）`
        : `「${label}」の以前の版は、まだありません` }),
      ...rows.map((row) => el('div', { class: 'ikb-row ikb-version', 'data-at': row.at }, [
        el('span', { text: row.label }),
        el('span', { class: 'ikb-dim', text: `${Math.max(1, Math.round(row.bytes / 1024))} KB` }),
        el('button', { type: 'button', text: '開く', onclick: () => run(() => openBackup(name, row.at)) }),
      ])),
      el('div', { class: 'ikb-row' }, [el('button', { type: 'button', text: '閉じる', onclick: () => { ui.versions.hidden = true; } })]));
    ui.versions.hidden = false;
    return rows;
  }

  // 以前の版を開く（読み込むだけ。保存し直すまで、いまの保存データは変わらない）
  function openBackup(name, at, opts) {
    return open(name, Object.assign({}, opts, { backup: at }));
  }

  function contentExists() {
    return getContentObjectCount() > 0 || btmGetGuidsSize() > 1;
  }

  async function confirmDanger(title, message, okText) {
    const dialog = optional(() => showConfirmDialog);
    if (dialog) return !!(await dialog({ title, message, okText, cancelText: 'やめる', danger: true }));
    return window.confirm(message);
  }

  function confirmReplace(label) {
    return confirmDanger('プロジェクトから開く',
      `いま開いているページをすべて閉じて「${label}」を開きます。保存していない変更は失われます。`, '開く');
  }

  // いまの中身を「未保存の変更あり」にする（漫画エディタ側の復元など、履歴の印が変わらない読み込みのあと用）
  function markDirtyNow() {
    const a = state.autosave;
    a.lastSig = signature();
    a.guid = currentGuid();
    if (!a.dirtySince && optional(() => contentExists())) a.dirtySince = Date.now();
    renderStatus();
  }

  // 読み込み。漫画エディタの「プロジェクトを読み込む」（置き換え）と同じ手順
  async function open(name, opts) {
    requireFeature('open');
    const o = opts || {};
    if (!name || !NAME_RE.test(name)) throw new Error('開く漫画データを選んでください');
    await waitIdle();
    const label = (name === AUTOSAVE_NAME ? '自動保存' : name) + (o.backup ? `（以前の版 ${o.backup}）` : '');
    if (!o.force && contentExists() && !(await confirmReplace(label))) return null;
    const result = await busyWhile('読み込んでいます…', async () => {
      const res = await api('GET', o.backup
        ? `/api/manga/backup/${encodeURIComponent(name)}/${encodeURIComponent(o.backup)}`
        : '/api/manga/project/' + encodeURIComponent(name), undefined, true);
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
    ui.versions.hidden = true;
    await nextFrames(2);
    markClean();
    say(`開きました: ${label}（${result.pages} ページ）`
      + (o.backup ? '。この版を残すには「プロジェクトに保存」を押してください' : ''));
    return result;
  }

  // 自動保存: 変更に気づいてから一定時間後、またはページを切り替えたときに autosave へ保存する
  // 前に見たときから変わっていたら「未保存の変更あり」にする
  function checkDirty() {
    const a = state.autosave;
    if (!state.ready || !state.features.save || isProjectBusy()) return false;
    const sig = signature();
    if (sig === null) return false;
    if (sig !== a.lastSig) {
      const guid = currentGuid();
      // 白紙 1 枚（保存する物が無い）のうちは「未保存」にしない。起動直後に漫画エディタが積む履歴を、変更と数えないため
      const blank = !optional(() => contentExists());
      if (a.guid && guid !== a.guid && !blank) a.pageChanged = true;
      a.guid = guid;
      a.lastSig = sig;
      if (!a.dirtySince && !blank) { a.dirtySince = Date.now(); renderStatus(); }
    }
    return !!a.dirtySince;
  }

  // 保存していない変更がある（保存の途中・再試行中を含む）。閉じる前の確認に使う
  function unsavedNow() {
    checkDirty();
    const a = state.autosave;
    return !!(a.dirtySince || autosaving || a.failed);
  }

  function autosaveTick() {
    const a = state.autosave;
    if (!state.ready || !state.features.save || state.busy) return;
    if (isProjectBusy()) return;
    if (!checkDirty() || !a.enabled) return;
    if (a.failed) {
      if (Date.now() >= a.retryAt) autosaveNow();              // 失敗のあとは、広げた間隔で再試行する
      return;
    }
    if (a.pageChanged || Date.now() - a.dirtySince >= a.seconds * 1000) autosaveNow();
  }

  // タブが隠れた（別のタブへ移った・最小化した・閉じかけている）ら、待ち時間を待たずに保存する
  function saveEarly() {
    const a = state.autosave;
    if (!state.ready || !state.features.save || !a.enabled) return null;
    if (!checkDirty()) return null;
    return autosaveNow();
  }

  function savedLabel(p) {
    const label = p.name === AUTOSAVE_NAME ? '自動保存' : p.name;
    return `${label} — ${formatTime(p.mtime)}${p.pages ? `・${p.pages} ページ` : ''}`;
  }

  // 前回の続き: 名前を付けて保存した最後の物と、自動保存。新しい方を既定で示し、どちらを開くか選べる
  async function offerResume() {
    if (!state.features.open) return;
    const rows = await listSaved();
    const last = store.get('lastName', '');
    if (last && !ui.name.value) { ui.name.value = last; state.name = last; }
    const candidates = rows.filter((p) => p.name === AUTOSAVE_NAME || p.name === last)
      .sort((a, b) => b.mtime - a.mtime);
    if (!candidates.length) return;
    const picked = () => {
      const on = ui.resume.querySelector('input[name="ikb-resume-pick"]:checked');
      return on ? on.value : candidates[0].name;
    };
    ui.resume.replaceChildren(
      el('div', { text: candidates.length > 1 ? '前回の続きを開きますか？（新しい方を選んであります）' : '前回の続きを開きますか？' }),
      ...candidates.map((p, i) => el('label', { class: 'ikb-row ikb-resume-pick', 'data-name': p.name }, [
        el('input', { type: 'radio', name: 'ikb-resume-pick', value: p.name, checked: i === 0,
          onchange: () => { ui.resume.dataset.name = picked(); } }),
        el('span', { text: savedLabel(p) }),
      ])),
      el('div', { class: 'ikb-row' }, [
        el('button', { type: 'button', class: 'ikb-primary', text: '開く', onclick: () => run(() => open(picked())) }),
        el('button', { type: 'button', text: '閉じる', onclick: () => { ui.resume.hidden = true; } }),
      ]));
    ui.resume.hidden = false;
    ui.resume.dataset.name = candidates[0].name;
    setOpen(true, true);
  }

  // ---- 漫画エディタ自身の自動保存（ブラウザの中）に残っているデータ ----

  function upstreamStore() {
    return localforage.createInstance({ name: 'autoSaveStorage', storeName: 'projectAutoSave' });
  }

  async function readUpstreamOld() {
    if (!has('localforage')) return null;
    try {
      const meta = await upstreamStore().getItem('metadata');
      if (!meta || !meta.timestamp) return null;
      return { timestamp: meta.timestamp, pages: Array.isArray(meta.pageOrder) ? meta.pageOrder.length : 0 };
    } catch (e) { return null; }
  }

  function renderUpstreamNote() {
    const mode = state.upstreamAuto.mode;
    const text = mode === 'kept'
      ? '漫画エディタ側の自動保存が有効です（inkdeus の自動保存と二重に保存され、開くたびに復元の確認が出ます）'
      : mode === 'unavailable'
        ? '漫画エディタ側の自動保存が有効です（この版では止められませんでした。inkdeus の自動保存と二重に保存されます）'
        : '';
    ui.upstreamNote.textContent = text;
    ui.upstreamNote.hidden = !text;
    if (mode === 'stopped') {
      // 漫画エディタの設定欄の「自動保存」は働かない。そのことが分かるようにしておく
      ['autoSaveCheckbox', 'autoSaveInterval'].forEach((id) => {
        const node = document.getElementById(id);
        if (node) { node.disabled = true; node.title = 'inkdeus 経由では、素材パネルの「自動保存」（プロジェクトの manga/autosave）を使います'; }
      });
    }
  }

  // 止める前に漫画エディタが残した自動保存があれば、初回だけ尋ねる（開く / 無視する / 消す）。勝手には消さない
  async function offerUpstreamOld() {
    const u = state.upstreamAuto;
    renderUpstreamNote();
    if (u.mode !== 'stopped') return;
    u.old = await readUpstreamOld();
    if (!u.old || store.get('upstreamOldSeen', 0) === u.old.timestamp) return;
    const done = (seen) => { if (seen) store.set('upstreamOldSeen', u.old.timestamp); ui.upstreamOld.hidden = true; };
    ui.upstreamOld.replaceChildren(
      el('div', { text: '漫画エディタ側に、以前の自動保存が残っています'
        + `（${formatTime(u.old.timestamp / 1000)}${u.old.pages ? `・${u.old.pages} ページ` : ''}）。`
        + 'いまは inkdeus がプロジェクトへ自動保存するので、漫画エディタ側の自動保存は止めてあります' }),
      el('div', { class: 'ikb-row' }, [
        el('button', { type: 'button', class: 'ikb-primary', 'data-act': 'open', text: '開く',
          title: '漫画エディタの復元の画面で、中身を確かめて開きます',
          onclick: () => run(async () => {
            ui.upstreamOld.hidden = true;
            const recovered = await u.original.checkRecovery.call(AutoSaveManager);
            if (recovered) { done(true); markDirtyNow(); say('漫画エディタ側の自動保存を開きました。残すには「プロジェクトに保存」を押してください'); }
            else if (await readUpstreamOld()) ui.upstreamOld.hidden = false;      // 「あとで決める」。また尋ねる
            else done(false);                                                     // 復元の画面で削除した
          }) }),
        el('button', { type: 'button', 'data-act': 'ignore', text: '無視する（次回から尋ねない）',
          title: 'データは消さずに残します', onclick: () => done(true) }),
        el('button', { type: 'button', 'data-act': 'delete', text: '消す',
          onclick: () => run(async () => {
            if (!(await confirmDanger('漫画エディタ側の自動保存を消す',
              '漫画エディタ側（ブラウザの中）に残っている以前の自動保存を消します。元に戻せません。', '消す'))) return;
            await u.original.clearAutoSave.call(AutoSaveManager);
            u.old = null;
            ui.upstreamOld.hidden = true;
            say('漫画エディタ側の以前の自動保存を消しました');
          }) }),
      ]));
    ui.upstreamOld.hidden = false;
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
  // multiplier を渡すと、原稿サイズと DPI の設定によらずその倍率で書き出す（PDF の書き出しが使う）
  async function renderPagePng(multiplier) {
    await ensurePageFonts();                                   // フォントが読み込み終わってから描く
    removeGrid();
    let size;
    let link;
    try {
      size = multiplier ? { multiplier, width: Math.round(canvas.width * multiplier), height: Math.round(canvas.height * multiplier) }
        : ImageUtil.getOutputPixelSize();
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
      state.patches = status.patches || null;
      ui.patchNotice.textContent = status.patchNotice || '';
      ui.patchNotice.hidden = !status.patchNotice;
    } catch (e) {
      fail(e, 'inkdeus のサーバに接続できません');
    }
    state.source = store.get('source', null);
    state.folder = store.get('folder', DEFAULT_FOLDER) || DEFAULT_FOLDER;
    refresh().catch((e) => fail(e, '素材の一覧を読めません'));
    renderStatus();

    try {
      const config = await api('GET', '/api/config');
      state.server = config.features || [];
      checkBuild(config.build || null);
    } catch (e) {
      state.server = [];
    }
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
    setInterval(() => { renderTarget(); renderSelection(false); fontTick(); }, 700);
    await refreshFonts().catch((e) => fail(e, 'フォントの一覧を読めません'));
    await refreshVersions();
    connectEvents();
    if (has('canvas')) {
      // 取り消し・やり直し・ページの切り替えではオブジェクトが作り直される。そのあとでページを見直す
      canvas.on('object:added', (e) => { if (e.target && e.target.type === 'image') queueSync(); });
      canvas.on('selection:created', () => renderSelection(false));
      canvas.on('selection:updated', () => renderSelection(false));
      canvas.on('selection:cleared', () => renderSelection(false));
    }
    setInterval(autosaveTick, POLL_MS);
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) optional(() => saveEarly());
    });
    window.addEventListener('pagehide', () => optional(() => saveEarly()));
    // 保存していない変更があるときは、閉じる・再読み込みの前に確かめる（その間に保存を進める）
    window.addEventListener('beforeunload', (e) => {
      if (leaving || !state.features.save || !unsavedNow()) return;
      optional(() => saveEarly());
      e.preventDefault();
      e.returnValue = '';
    });
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
    tameUpstreamAutosave();                                    // （DOMContentLoaded で済んでいる。念のため）
    await offerUpstreamOld().catch((e) => console.warn('[inkdeus]', e));
    await loadModules();
    return { ok: problems.length === 0 && complete, complete, features: Object.assign({}, state.features),
      missing: Object.assign({}, state.missing) };
  }

  // ---- 追加の部品（同じフォルダの別ファイル。この連携スクリプトが読み込む） ----
  // 部品は window.inkdeusBridge.x（下の内部の入口）を使い、公開する関数を window.inkdeusBridge に足す

  const MODULES = ['pdf.js', 'layout-core.js', 'layout.js'];
  function loadScript(src) {
    return new Promise((resolve, reject) => {
      const node = el('script', { src });
      node.onload = () => resolve(true);
      node.onerror = () => { node.remove(); reject(new Error('読み込めません: ' + src)); };
      document.head.appendChild(node);
    });
  }
  async function loadModules() {
    state.modules = [];
    for (const name of MODULES) {
      try {
        await loadScript('/manga-bridge/' + name);
        state.modules.push(name);
      } catch (e) {
        console.error('[inkdeus]', e);                          // 部品が無くても、ほかの機能は動かす
      }
    }
  }

  buildPanel();
  document.body.appendChild(ui.banner);

  window.inkdeusBridge = {
    version: 1,
    // 追加の部品のための内部の入口（外からは使わない）
    x: { state, ui, store, el, api, say, fail, run, has, optional, sleep, nextFrames, commands, requireFeature, waitIdle,
      busyWhile, exportName, renderPagePng, markClean, panels, describe, getTargetPanel, renderStatus },
    ready: init(),
    status: () => ({ ready: state.ready, busy: state.busy, features: Object.assign({}, state.features),
      server: (state.server || []).slice(), modules: (state.modules || []).slice(),
      // 差分: applied = 導入のときに当てた物（サーバの記録）、active = 実際に読み込まれた物（差分が自分で名乗る）
      patches: { applied: state.patches ? state.patches.applied.slice() : [], pending: state.patches ? state.patches.pending.slice() : [],
        active: (window.__inkdeusPatches || []).slice() },
      missing: Object.assign({}, state.missing), project: state.project, mangaDir: state.mangaDir,
      name: state.name, source: state.source, folder: state.folder, folders: state.folders.slice(),
      imageMax: state.imageMax,
      width: state.width, float: !!state.float, preferInked: state.preferInked, autoSwap: state.autoSwap,
      events: Object.assign({}, state.events), hot: Object.assign({}, state.hot),
      inkedFiles: Object.keys(state.inked.versions || {}),
      fonts: { names: state.fonts.list.map((x) => x.name), loaded: Array.from(state.fonts.loaded),
        failed: Array.from(state.fonts.failed.keys()), supported: state.fonts.supported },
      upstreamAutosave: { mode: state.upstreamAuto.mode, old: state.upstreamAuto.old },
      autosave: { enabled: state.autosave.enabled, seconds: state.autosave.seconds,
        dirty: !!state.autosave.dirtySince, lastAt: state.autosave.lastAt, failed: state.autosave.failed,
        retryMs: state.autosave.retryMs },
      save: saveStatus(), unsaved: state.ready && !!state.features.save && unsavedNow(), build: state.build,
      notices: Object.fromEntries(Object.entries(state.notices).map(([k, n]) => [k, n.text])) }),
    // 素材
    listSources, listAssets, refresh, selectSource, setFolder, assetUrl,
    placeAsset, placeInPanel, placeFree,
    // コマ
    getTargetPanel: () => describe(getTargetPanel()),
    panels: () => panels().map(describe),
    objects: () => (has('canvas') ? canvas.getObjects().map(describe) : []),
    // 保存と読み込み
    listSaved, save, open, autosaveNow, listBackups, openBackup, showBackups, saveEarly,
    setAutosave(opts) {
      const o = opts || {};
      if (typeof o.enabled === 'boolean') { state.autosave.enabled = o.enabled; ui.autosave.checked = o.enabled; }
      if (o.seconds >= 1) setAutosaveSeconds(Number(o.seconds));
      renderStatus();
    },
    // 書き出し
    exportPage, exportAllPages,
    // 白黒化の画面との行き来
    openEditor, adjustSelected, swapSelected, syncPage: () => queueSync(0), reconnectEvents: connectEvents,
    // 不具合を追うための控え: 直近の出来事と、白黒版の更新時刻の表
    debug: () => ({ now: Date.now(), trail: trail.slice(), versions: Object.assign({}, state.inked.versions || {}),
      changed: Array.from(state.inked.changed) }),
    // バージョン（同じ元画像から作る、設定の違う白黒版）
    switchVersion, newVersionAndAdjust,
    setPreferInked(on) { state.preferInked = !!on; ui.prefer.value = on ? '1' : '0'; },
    setAutoSwap(on) { state.autoSwap = !!on; ui.autoSwap.checked = !!on; if (on) return queueSync(0); return null; },
    // フォント
    refreshFonts, ensureFont, ensurePageFonts,
    // 画面
    setOpen: (open) => setOpen(!!open),
    setWidth: (px) => { const w = setWidth(px); relayout(); return w; },
    setFloat: (on) => setFloat(!!on),
    setImageMax(max) {
      if (!SIZES.some(([v]) => v === max)) throw new Error('大きさは 1024 / 2048 / 4096 / 0（原寸）のどれかです');
      state.imageMax = max;
      ui.size.value = String(max);
    },
  };
})();
