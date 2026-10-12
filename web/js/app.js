// 画面全体の制御: 入出力（サーバあり/なし）、履歴、表示の拡大縮小、パネルとの橋渡し。

import { Renderer } from './renderer.js';
import { encodeGrayPNG } from './png.js';
import { buildPanel } from './panel.js';
import {
  SCHEMA, normalize, defaultParams, applyStyle, effectiveUnit, effectiveWatermark, STYLES,
} from './schema.js';

const $ = (id) => document.getElementById(id);
const BITMAP_OPTS = { premultiplyAlpha: 'none', colorSpaceConversion: 'none' };
const SAVE_DELAY_MS = 700;
const STATS_DELAY_MS = 400;
const AUTO_EXPORT_DELAY_MS = 1500;      // 最後の変更からこの時間が経ったら自動で書き出す
const AUTO_EXPORT_KEY = 'inkdeus.autoExport';
// このブラウザ画面の ID。自分の保存で出たイベントを「外部からの変更」と取り違えないために使う
const CLIENT_ID = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
const HISTORY_MAX = 200;
const ZOOM_MIN = 0.02;
const ZOOM_MAX = 32;

const state = {
  server: false,        // サーバの API が使えるか
  images: [],           // サーバの画像一覧
  index: -1,
  name: null,           // 表示中の画像名
  remote: false,        // 表示中の画像がサーバの一覧のものか（ドロップした画像は false）
  params: null,
  rev: null,            // 表示中の設定に対応する設定ファイルの版（未保存の変更があるときは古い）
  hasMask: false,
  solo: null,           // { region, index } でその層だけ表示
  tab: 'person',        // トーンのパネルで開いているタブ
  project: null,
  lastExport: '',
  autoExport: true,     // 変更を自動で書き出す（漫画の構成の画面へ反映するため）
  batchForce: false,    // 一括書き出しで、書き出し済みの画像も作り直す
  lastBatch: '',        // 直近の一括書き出しの結果
  view: { zoom: 1, cx: 0, cy: 0 },
  compare: false,
  split: 0.5,           // 比較の仕切り位置（キャンバス幅に対する割合）
  original: false,
};

let renderer = null;
let history = [];
let hpos = -1;
let saveTimer = null;
let saving = false;
let statsTimer = null;
let lastStats = null;
let renderPending = false;
let drawPending = false;
let lastRenderMs = 0;
let renderSeq = 0;                      // 描画のたびに増える（書き出しの途中で絵が変わったことを知るため）

// ---- 表示まわりの小物 ----

function showMessage(text, kind = 'info', timeout = kind === 'error' ? 0 : 9000) {
  const box = document.createElement('div');
  box.className = `msg ${kind}`;
  box.textContent = text;
  box.title = 'クリックで閉じる';
  box.addEventListener('click', () => box.remove());
  $('messages').append(box);
  if (timeout) setTimeout(() => box.remove(), timeout);
}

function setBusy(on) {
  $('busy').hidden = !on;
}

function setSaveState(text) {
  $('saveState').textContent = text;
}

function stemOf(name) {
  const i = name.lastIndexOf('.');
  return i > 0 ? name.slice(0, i) : name;
}

function download(blob, filename) {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 30000);
}

async function getJSON(url) {
  try {
    const r = await fetch(url, { cache: 'no-store' });
    if (!r.ok) return null;
    return await r.json();
  } catch {
    return null;
  }
}

const apiPath = (kind, name) => `api/${kind}/${encodeURIComponent(name)}`;

// ---- 描画 ----

function drawNow() {
  drawPending = false;
  if (!renderer) return;
  const canvas = $('gl');
  renderer.draw({
    zoom: state.view.zoom, cx: state.view.cx, cy: state.view.cy,
    original: state.original,
    split: state.compare && !state.original ? state.split * canvas.width : -1,
  });
  $('zoomLabel').textContent = renderer.W ? `${Math.round(state.view.zoom * 100)}%` : '-';
  const handle = $('splitHandle');
  handle.hidden = !state.compare;
  handle.style.left = `${state.split * 100}%`;
}

function requestDraw() {
  if (drawPending) return;
  drawPending = true;
  requestAnimationFrame(drawNow);
}

function renderNow() {
  renderPending = false;
  if (!renderer) return null;
  let result = null;
  try {
    renderSeq += 1;
    result = renderer.render(state.params, { solo: state.solo });
    lastRenderMs = result.ms;
    if (renderer.W) $('timing').textContent = `再描画 ${result.ms.toFixed(1)} ms`;
  } catch (e) {
    showMessage(`描画に失敗しました: ${e.message}`, 'error');
  }
  drawNow();
  setBusy(false);
  scheduleStats();
  return result;
}

function requestRender() {
  if (renderPending) return;
  renderPending = true;
  if (lastRenderMs > 120) {
    // 重い描画のときは「処理中」を先に画面へ出してから描く
    setBusy(true);
    requestAnimationFrame(() => requestAnimationFrame(renderNow));
  } else {
    requestAnimationFrame(renderNow);
  }
}

// ---- 表示の拡大縮小・移動 ----

function resizeCanvas() {
  const canvas = $('gl');
  const dpr = window.devicePixelRatio || 1;
  const w = Math.max(1, Math.round(canvas.clientWidth * dpr));
  const h = Math.max(1, Math.round(canvas.clientHeight * dpr));
  if (canvas.width !== w || canvas.height !== h) {
    canvas.width = w;
    canvas.height = h;
  }
  drawNow();
}

function fitView() {
  if (!renderer || !renderer.W) return;
  const canvas = $('gl');
  const barPx = 34 * (window.devicePixelRatio || 1);
  const zoom = Math.min(canvas.width / renderer.W, Math.max(1, canvas.height - barPx) / renderer.H) * 0.98;
  state.view = {
    zoom: Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, zoom)),
    cx: renderer.W / 2,
    cy: renderer.H / 2 + (barPx / 2) / zoom,
  };
  requestDraw();
}

function zoomAt(newZoom, px, py) {
  const canvas = $('gl');
  const v = state.view;
  const z = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, newZoom));
  const dx = px - canvas.width / 2;
  const dy = py - canvas.height / 2;
  // カーソル下の画像座標が動かないように中心をずらす
  v.cx += dx / v.zoom - dx / z;
  v.cy += dy / v.zoom - dy / z;
  v.zoom = z;
  requestDraw();
}

function canvasPoint(e) {
  const canvas = $('gl');
  const rect = canvas.getBoundingClientRect();
  const sx = canvas.width / Math.max(1, rect.width);
  const sy = canvas.height / Math.max(1, rect.height);
  return [(e.clientX - rect.left) * sx, (e.clientY - rect.top) * sy];
}

function wireViewer() {
  const canvas = $('gl');
  canvas.addEventListener('wheel', (e) => {
    e.preventDefault();
    const [px, py] = canvasPoint(e);
    const delta = e.deltaMode === 1 ? e.deltaY * 33 : e.deltaY;
    zoomAt(state.view.zoom * Math.exp(-delta * 0.0015), px, py);
  }, { passive: false });

  let drag = null;
  canvas.addEventListener('pointerdown', (e) => {
    if (e.button !== 0 && e.button !== 1) return;
    canvas.setPointerCapture(e.pointerId);
    drag = { p: canvasPoint(e), cx: state.view.cx, cy: state.view.cy };
    canvas.classList.add('dragging');
  });
  canvas.addEventListener('pointermove', (e) => {
    if (!drag) return;
    const [px, py] = canvasPoint(e);
    state.view.cx = drag.cx - (px - drag.p[0]) / state.view.zoom;
    state.view.cy = drag.cy - (py - drag.p[1]) / state.view.zoom;
    requestDraw();
  });
  const end = () => { drag = null; canvas.classList.remove('dragging'); };
  canvas.addEventListener('pointerup', end);
  canvas.addEventListener('pointercancel', end);

  const handle = $('splitHandle');
  let splitting = false;
  handle.addEventListener('pointerdown', (e) => { handle.setPointerCapture(e.pointerId); splitting = true; });
  handle.addEventListener('pointermove', (e) => {
    if (!splitting) return;
    const rect = canvas.getBoundingClientRect();
    state.split = Math.min(0.98, Math.max(0.02, (e.clientX - rect.left) / Math.max(1, rect.width)));
    requestDraw();
  });
  handle.addEventListener('pointerup', () => { splitting = false; });
  handle.addEventListener('pointercancel', () => { splitting = false; });

  $('btnFit').addEventListener('click', fitView);
  $('btnActual').addEventListener('click', () => zoomAt(1, canvas.width / 2, canvas.height / 2));
  $('btnCompare').addEventListener('click', () => {
    state.compare = !state.compare;
    $('btnCompare').classList.toggle('on', state.compare);
    requestDraw();
  });
  const showOriginal = (on) => {
    if (state.original === on) return;
    state.original = on;
    $('btnOriginal').classList.toggle('on', on);
    requestDraw();
  };
  $('btnOriginal').addEventListener('pointerdown', () => showOriginal(true));
  for (const ev of ['pointerup', 'pointerleave', 'pointercancel']) {
    $('btnOriginal').addEventListener(ev, () => showOriginal(false));
  }
  const typing = (t) => t && (t.tagName === 'TEXTAREA' || t.tagName === 'SELECT'
    || (t.tagName === 'INPUT' && ['text', 'number'].includes(t.type)));
  window.addEventListener('keydown', (e) => {
    if (e.code === 'Space' && !typing(e.target)) {
      e.preventDefault();
      if (!e.repeat) showOriginal(true);
      return;
    }
    const mod = e.ctrlKey || e.metaKey;
    if (!mod || typing(e.target)) return;
    const k = e.key.toLowerCase();
    if (k === 'z' && !e.shiftKey) { e.preventDefault(); undo(); }
    else if (k === 'y' || (k === 'z' && e.shiftKey)) { e.preventDefault(); redo(); }
  });
  window.addEventListener('keyup', (e) => {
    if (e.code === 'Space' && !typing(e.target)) { e.preventDefault(); showOriginal(false); }
  });
  window.addEventListener('blur', () => showOriginal(false));
  new ResizeObserver(resizeCanvas).observe($('viewer'));
}

// ---- 設定・履歴・保存 ----

const ctx = {
  state,
  imageSize: () => ({ w: renderer ? renderer.W : 0, h: renderer ? renderer.H : 0 }),
  live: () => { requestRender(); deferAutoExport(); },
  commit: () => commit(),
  rebuild: () => rebuild(),
  setSolo: (solo, redraw) => {
    state.solo = solo;
    if (redraw) { requestRender(); rebuild(); }
  },
  setTab: (tab) => { state.tab = tab; rebuild(); },
  style: (key, region) => {
    state.params = applyStyle(state.params, key, region);
    state.solo = null;
    commit(); rebuild(); requestRender();
  },
  exportNow: () => exportNow(),
  setAutoExport: (on) => setAutoExport(on),
  exportAll: () => exportAll({ force: state.batchForce }),
  setBatchForce: (on) => { state.batchForce = !!on; },
  saveJSON: () => download(new Blob([JSON.stringify(state.params, null, 2) + '\n'], { type: 'application/json' }),
    `${state.name ? stemOf(state.name) : 'inkdeus'}.inkdeus.json`),
  openJSON: () => $('fileParams').click(),
  openMask: () => $('fileMask').click(),
};

function rebuild() {
  buildPanel($('panel'), ctx);
  $('btnUndo').disabled = hpos <= 0;
  $('btnRedo').disabled = hpos >= history.length - 1;
}

function resetHistory() {
  history = [JSON.stringify(state.params)];
  hpos = 0;
}

/** いまの設定を履歴に積む。save=false は「すでにファイルと同じ内容」のとき（外部からの更新） */
function commit(save = true) {
  const snap = JSON.stringify(state.params);
  if (snap === history[hpos]) return;
  history.length = hpos + 1;
  history.push(snap);
  if (history.length > HISTORY_MAX) history.shift();
  hpos = history.length - 1;
  $('btnUndo').disabled = hpos <= 0;
  $('btnRedo').disabled = true;
  if (save) scheduleSave();
  touchAutoExport();
}

function stepHistory(delta) {
  const next = hpos + delta;
  if (next < 0 || next >= history.length) return;
  hpos = next;
  state.params = JSON.parse(history[hpos]);
  state.solo = null;
  rebuild();
  requestRender();
  scheduleSave();
  touchAutoExport();
}
const undo = () => stepHistory(-1);
const redo = () => stepHistory(1);

/** 任意の JSON を設定として読み込む（警告があれば表示する） */
function loadParams(raw, { fresh = false, save = true } = {}) {
  const { params, warnings } = normalize(raw);
  state.params = params;
  state.solo = null;
  if (warnings.length) showMessage(`設定の読み込みで気になる点がありました:\n・${warnings.join('\n・')}`, 'info', 20000);
  if (fresh) resetHistory(); else commit(save);
  rebuild();
  return warnings;
}

function canAutoSave() {
  return state.server && state.remote && !!state.name;
}

function scheduleSave() {
  if (!canAutoSave()) return;
  setSaveState('未保存');
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => { saveTimer = null; saveNow(); }, SAVE_DELAY_MS);
}

async function saveNow() {
  if (!canAutoSave()) return;
  const name = state.name;
  saving = true;
  try {
    const r = await fetch(apiPath('params', name), {
      method: 'PUT', body: JSON.stringify(state.params),
      headers: { 'Content-Type': 'application/json', 'X-Inkdeus-Client': CLIENT_ID },
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const j = await r.json();
    const item = state.images.find((i) => i.name === name);
    if (item) item.hasParams = true;
    if (name === state.name) state.rev = j.rev;
    if (!saveTimer) setSaveState('保存済み');
  } catch (e) {
    setSaveState('保存に失敗');
    showMessage(`設定を保存できませんでした: ${e.message}`, 'error');
  } finally {
    saving = false;
    scheduleStats();
  }
}

async function flushSave() {
  if (!saveTimer) return;
  clearTimeout(saveTimer);
  saveTimer = null;
  await saveNow();
}

// ---- 外部との連携（統計の送信・設定ファイルの外部変更・指示の受け取り） ----

/** 再描画が落ち着いたら、描画結果の統計をサーバへ送る（どの版の設定に対する物かを添えて） */
function scheduleStats() {
  if (!canAutoSave()) return;
  clearTimeout(statsTimer);
  statsTimer = setTimeout(postStats, STATS_DELAY_MS);
}

function computeStats() {
  if (!renderer || !renderer.W) return null;
  const stats = renderer.stats(state.params, { solo: state.solo });
  return { name: state.name, rev: state.rev, renderMs: Math.round(lastRenderMs * 10) / 10, ...stats };
}

async function postStats() {
  statsTimer = null;
  // 未保存の変更があるうちは送らない（保存が済むと改めて呼ばれる）
  if (!canAutoSave() || saveTimer || saving || renderPending || state.solo) return;
  const name = state.name;
  try {
    const stats = computeStats();
    if (!stats) return;
    lastStats = stats;
    await fetch(apiPath('stats', name), {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(stats),
    });
  } catch (e) {
    console.warn('統計を送れませんでした', e);
  }
}

function postState() {
  if (!state.server) return;
  fetch('api/state', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ current: state.remote ? state.name : null, client: CLIENT_ID }),
  }).catch(() => {});
}

/** 設定ファイルが外から書き換えられたときに読み直す（取り消し履歴にも1件として積む） */
async function onExternalParams(ev) {
  const item = state.images.find((i) => i.name === ev.name);
  if (item) item.hasParams = ev.rev !== null;
  if (ev.source === CLIENT_ID || !state.remote || ev.name !== state.name) return;
  if (ev.rev === null || ev.rev === state.rev) return;
  const r = await fetch(apiPath('params', ev.name), { cache: 'no-store' });
  if (!r.ok || ev.name !== state.name) return;
  const rev = r.headers.get('X-Inkdeus-Rev');
  if (rev === state.rev) return;
  let raw;
  try {
    raw = await r.json();
  } catch {
    showMessage('外部から書き換えられた設定ファイルが JSON として読めません', 'error');
    return;
  }
  clearTimeout(saveTimer);        // 手元の未保存の変更より外部の変更を優先する
  saveTimer = null;
  loadParams(raw, { save: false });
  state.rev = rev;
  setSaveState('外部から更新');
  renderNow();
}

function connectEvents() {
  if (!state.server || typeof EventSource === 'undefined') return;
  const source = new EventSource('api/events');
  source.onmessage = (msg) => {
    let ev;
    try { ev = JSON.parse(msg.data); } catch { return; }
    if (ev.type === 'params') onExternalParams(ev).catch((e) => console.warn(e));
    else if (batch.running && (ev.type === 'open' || ev.type === 'export')) return;   // 一括書き出しの最中は受けない
    else if (ev.type === 'open') openByName(ev.name);
    else if (ev.type === 'export') openByName(ev.name).then(() => exportNow()).catch((e) => console.warn(e));
    else if (ev.type === 'export-all') exportAll({ force: !!ev.force }).catch((e) => console.warn(e));
    else if (ev.type === 'export-all-abort') abortExportAll();
  };
}

async function openByName(name) {
  if (state.remote && state.name === name) return;
  let index = state.images.findIndex((i) => i.name === name);
  if (index < 0) {
    await refreshImages();          // 起動後に増えた画像かもしれない
    index = state.images.findIndex((i) => i.name === name);
  }
  if (index >= 0) await openIndex(index);
}

async function refreshImages() {
  const list = await getJSON('api/images');
  if (!list || !Array.isArray(list.images)) return;
  state.images = list.images;
  const sel = $('selImage');
  sel.replaceChildren(...state.images.map((item) => {
    const opt = document.createElement('option');
    opt.value = item.name;
    opt.textContent = `${item.name}${item.hasMask ? '  [マスク]' : ''}`;
    return opt;
  }));
  if (state.remote && state.name) sel.value = state.name;
  $('nav').hidden = state.images.length < 2;
}

// ---- 画像・マスクの読み込み ----

async function setImageBlob(blob, name, remote) {
  const bitmap = await createImageBitmap(blob, BITMAP_OPTS);
  try {
    renderer.setImage(bitmap, bitmap.width, bitmap.height);
  } finally {
    bitmap.close();
  }
  state.name = name;
  state.remote = remote;
  state.hasMask = false;
  state.lastExport = '';
  state.solo = null;
  state.rev = null;
  $('dropHint').hidden = true;
  document.title = `${name} — inkdeus`;
  setSaveState('');
  fitView();
}

async function setMaskBlob(blob) {
  if (!renderer.W) {
    showMessage('先に画像を読み込んでください', 'error');
    return;
  }
  if (!blob) {
    renderer.setMask(null);
    state.hasMask = false;
    return;
  }
  const bitmap = await createImageBitmap(blob, BITMAP_OPTS);
  try {
    renderer.setMask(bitmap);
  } finally {
    bitmap.close();
  }
  state.hasMask = renderer.hasMask;
}

/** サーバの一覧の i 番目を開く（マスクと保存済みの設定も読む） */
async function openIndex(i) {
  if (i < 0 || i >= state.images.length) return;
  await flushSave();
  await flushAutoExport();             // 前の画像の「未反映の変更」を書き出してから離れる
  const item = state.images[i];
  setBusy(true);
  try {
    const r = await fetch(apiPath('image', item.name), { cache: 'no-store' });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    await setImageBlob(await r.blob(), item.name, true);
    state.index = i;
    $('selImage').value = item.name;
    if (item.hasMask) {
      const m = await fetch(apiPath('mask', item.name), { cache: 'no-store' });
      if (m.ok) await setMaskBlob(await m.blob());
    }
    // 保存済みの設定があればそれを、無ければ直前の設定を引き継ぐ
    const saved = item.hasParams ? await fetch(apiPath('params', item.name), { cache: 'no-store' }) : null;
    if (saved && saved.ok) {
      const rev = saved.headers.get('X-Inkdeus-Rev');
      loadParams(await saved.json(), { fresh: true });
      state.rev = rev;
      setSaveState('保存済み');
    } else {
      resetHistory();
      rebuild();
      // 既定値と違う設定を引き継いだときは、サーバ（と外部の操作）にも分かるようすぐ保存する
      if (JSON.stringify(state.params) !== JSON.stringify(defaultParams())) await saveNow();
    }
    postState();
    renderNow();
    // 白黒版がまだ無い（または設定より古い）画像は、開いた時点で書き出しの対象にする
    if (item.exportFresh) setExportState(state.autoExport ? '書き出し済み' : '');
    else touchAutoExport();
  } catch (e) {
    showMessage(`${item.name} を開けませんでした: ${e.message}`, 'error');
  } finally {
    setBusy(false);
    $('btnPrev').disabled = state.index <= 0;
    $('btnNext').disabled = state.index >= state.images.length - 1;
  }
}

async function openLocalImage(file) {
  await flushSave();
  setBusy(true);
  try {
    await setImageBlob(file, file.name, false);
    postState();
    resetHistory();
    rebuild();
    renderNow();
  } catch (e) {
    showMessage(`画像を読み込めませんでした: ${e.message}`, 'error');
  } finally {
    setBusy(false);
  }
}

async function openLocalMask(file) {
  try {
    await setMaskBlob(file);
    rebuild();
    renderNow();
  } catch (e) {
    showMessage(`マスクを読み込めませんでした: ${e.message}`, 'error');
  }
}

async function openLocalParams(file) {
  try {
    loadParams(JSON.parse(await file.text()));
    renderNow();
  } catch (e) {
    showMessage(`設定JSONを読み込めませんでした: ${e.message}`, 'error');
  }
}

async function handleFiles(files) {
  const list = Array.from(files);
  const isJSON = (f) => /\.json$/i.test(f.name) || f.type === 'application/json';
  const images = list.filter((f) => !isJSON(f) && (f.type.startsWith('image/') || /\.(png|jpe?g|webp|bmp)$/i.test(f.name)));
  const masks = images.filter((f) => /mask/i.test(f.name));
  const mains = images.filter((f) => !masks.includes(f));
  if (mains.length) await openLocalImage(mains[0]);
  if (masks.length) await openLocalMask(masks[0]);
  for (const f of list.filter(isJSON).slice(0, 1)) await openLocalParams(f);
}

function wireFiles() {
  const pick = (id, handler) => $(id).addEventListener('change', async (e) => {
    const file = e.target.files[0];
    e.target.value = '';
    if (file) await handler(file);
  });
  pick('fileImage', openLocalImage);
  pick('fileMask', openLocalMask);
  pick('fileParams', openLocalParams);
  $('btnOpenImage').addEventListener('click', () => $('fileImage').click());
  $('btnOpenMask').addEventListener('click', () => $('fileMask').click());
  window.addEventListener('dragover', (e) => { e.preventDefault(); document.body.classList.add('dropping'); });
  window.addEventListener('dragleave', (e) => { if (!e.relatedTarget) document.body.classList.remove('dropping'); });
  window.addEventListener('drop', (e) => {
    e.preventDefault();
    document.body.classList.remove('dropping');
    if (e.dataTransfer && e.dataTransfer.files.length) handleFiles(e.dataTransfer.files);
  });
  $('btnUndo').addEventListener('click', undo);
  $('btnRedo').addEventListener('click', redo);
  $('btnPrev').addEventListener('click', () => openIndex(state.index - 1));
  $('btnNext').addEventListener('click', () => openIndex(state.index + 1));
  $('selImage').addEventListener('change', (e) => {
    openIndex(state.images.findIndex((i) => i.name === e.target.value));
  });
}

// ---- 書き出し ----

/** いま描かれている絵を PNG にする。途中で描き直しが入ったら null（上下で違う絵が混ざるため使えない） */
async function encodeCurrent() {
  const { W, H } = renderer;
  const seq = renderSeq;
  const blob = await encodeGrayPNG(W, H, (y0, rows) => renderer.readPixels(0, y0, W, rows));
  return seq === renderSeq ? blob : null;
}

/** PNG をサーバへ保存する（表示中の画像の書き出しとして） */
async function uploadExport(name, blob) {
  const r = await fetch(apiPath('export', name), {
    method: 'POST', body: blob,
    headers: { 'Content-Type': 'image/png', ...(state.rev ? { 'X-Inkdeus-Rev': state.rev } : {}) },
  });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  const j = await r.json();
  const item = state.images.find((i) => i.name === name);
  if (item) { item.hasExport = true; item.exportFresh = true; }
  return { path: j.path, bytes: blob.size };
}

async function exportNow() {
  if (!renderer || !renderer.W) {
    showMessage('画像が読み込まれていません', 'error');
    return null;
  }
  setBusy(true);
  await new Promise((resolve) => setTimeout(resolve, 0));
  try {
    state.solo = null;                          // ソロ表示のまま書き出さない
    let blob = null;
    for (let attempt = 0; attempt < 3 && !blob; attempt++) {
      renderNow();
      setBusy(true);
      blob = await encodeCurrent();
    }
    if (!blob) throw new Error('書き出しの途中で設定が変わり続けています。操作を止めてからもう一度試してください');
    let result;
    if (state.server && state.remote) {
      const snap = JSON.stringify(state.params);
      await flushSave();                        // 保存先の設定ファイルと書き出した絵をそろえる
      result = await uploadExport(state.name, blob);
      state.lastExport = `保存しました: ${result.path}`;
      if (snap === JSON.stringify(state.params) && !autoExport.timer) {
        autoExport.dirty = false;
        setExportState('書き出し済み');
      }
    } else {
      const filename = `${stemOf(state.name || 'image')}_inkdeus.png`;
      download(blob, filename);
      result = { download: filename, bytes: blob.size };
      state.lastExport = `ダウンロードしました: ${filename}`;
    }
    rebuild();
    return result;
  } catch (e) {
    showMessage(`書き出しに失敗しました: ${e.message}`, 'error');
    throw e;
  } finally {
    setBusy(false);
  }
}

// ---- 自動書き出し（設定が変わって落ち着いたら PNG を保存する。漫画の構成の画面がそれを拾う） ----

const autoExport = { timer: null, running: null, again: false, dirty: false, count: 0 };

function setExportState(text) {
  $('exportState').textContent = text;
}

function autoExportReady() {
  return canAutoSave() && !batch.running && renderer && renderer.W > 0;
}

/** 設定が変わったときに呼ぶ。最後の変更から一定時間後に書き出す */
function touchAutoExport() {
  if (!autoExportReady()) return;
  autoExport.dirty = true;
  const item = state.images.find((i) => i.name === state.name);
  if (item) item.exportFresh = false;
  if (!state.autoExport) {
    setExportState('未反映の変更あり');
    return;
  }
  setExportState(autoExport.running ? '書き出し中…' : '未反映の変更あり');
  clearTimeout(autoExport.timer);
  autoExport.timer = setTimeout(runAutoExport, AUTO_EXPORT_DELAY_MS);
}

/** スライダーを動かしている間は、待ち時間を数え直す（動かしている最中に書き出さない） */
function deferAutoExport() {
  if (!autoExport.timer) return;
  clearTimeout(autoExport.timer);
  autoExport.timer = setTimeout(runAutoExport, AUTO_EXPORT_DELAY_MS);
}

function runAutoExport() {
  autoExport.timer = null;
  if (!state.autoExport || !autoExportReady()) return Promise.resolve();
  if (autoExport.running) {
    autoExport.again = true;                    // 書き出し中に来た変更は、終わってから1回だけやり直す（溜めない）
    return autoExport.running;
  }
  if (state.solo || renderPending) {
    // ソロ表示の間は書き出さない（その層だけの絵になってしまう）。戻るのを待つ
    autoExport.timer = setTimeout(runAutoExport, 500);
    return Promise.resolve();
  }
  const name = state.name;
  setExportState('書き出し中…');
  autoExport.running = (async () => {
    try {
      await flushSave();
      const snap = JSON.stringify(state.params);
      const blob = await encodeCurrent();
      if (!blob || state.name !== name) {
        autoExport.again = state.name === name;  // 途中で絵が変わった。保存せずにやり直す
        return;
      }
      await uploadExport(name, blob);
      autoExport.count += 1;
      if (snap === JSON.stringify(state.params) && !autoExport.timer && !autoExport.again) {
        autoExport.dirty = false;
        setExportState('書き出し済み');
      }
    } catch (e) {
      console.warn('自動の書き出しに失敗しました', e);
      setExportState('書き出しに失敗');
    } finally {
      autoExport.running = null;
      if (autoExport.again) {
        autoExport.again = false;
        if (!autoExport.timer) autoExport.timer = setTimeout(runAutoExport, 0);
      }
    }
  })();
  return autoExport.running;
}

/** 待っている自動書き出しがあれば、いますぐ済ませる（別の画像へ移る前に呼ぶ） */
async function flushAutoExport() {
  if (batch.running) return;
  if (autoExport.timer && state.autoExport && !state.solo) {
    clearTimeout(autoExport.timer);
    autoExport.timer = null;
    await runAutoExport();
  }
  if (autoExport.running) await autoExport.running;
  clearTimeout(autoExport.timer);
  autoExport.timer = null;
  autoExport.again = false;
}

function setAutoExport(on) {
  state.autoExport = !!on;
  try { localStorage.setItem(AUTO_EXPORT_KEY, on ? '1' : '0'); } catch { /* 覚えられなくても動く */ }
  if (!on) {
    clearTimeout(autoExport.timer);
    autoExport.timer = null;
    setExportState(autoExport.dirty ? '未反映の変更あり' : '');
  } else if (autoExport.dirty) {
    touchAutoExport();
  }
}

// ---- 全画像の書き出し ----

const batch = { running: false, abort: false, total: 0, done: 0, exported: 0, skipped: 0, failed: 0,
  current: null, startedAt: 0, force: false, errors: [] };
let batchTicker = null;

function formatSpan(seconds) {
  const s = Math.max(0, Math.round(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

function batchReport(extra = {}) {
  const elapsed = batch.startedAt ? (performance.now() - batch.startedAt) / 1000 : 0;
  const left = batch.total - batch.done;
  // 残りは「実際に書き出した画像の平均」から見積もる（飛ばした画像はほぼ時間がかからない）
  const remaining = batch.exported > 0 && batch.running ? (elapsed / batch.exported) * left : null;
  return { running: batch.running, total: batch.total, done: batch.done, exported: batch.exported,
    skipped: batch.skipped, failed: batch.failed, current: batch.current, aborted: batch.abort, force: batch.force,
    elapsed: Math.round(elapsed * 10) / 10, remaining: remaining === null ? null : Math.round(remaining),
    errors: batch.errors.slice(0, 20), ...extra };
}

function showBatch(extra) {
  const r = batchReport(extra);
  $('batchBar').hidden = !batch.running;
  document.body.classList.toggle('batching', batch.running);
  if (batch.running) {
    $('batchText').textContent = `全画像を書き出し中 ${r.done} / ${r.total}`
      + `（書き出し ${r.exported}・飛ばし ${r.skipped}・失敗 ${r.failed}）`
      + ` 経過 ${formatSpan(r.elapsed)}${r.remaining === null ? '' : ` / 残り 約 ${formatSpan(r.remaining)}`}`
      + (r.current ? ` — ${r.current}` : '');
  }
  return r;
}

function postBatch(extra) {
  const r = showBatch(extra);
  return fetch('api/batch', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(r),
  }).catch(() => {});
}

function abortExportAll() {
  if (batch.running) batch.abort = true;
}

/**
 * プロジェクトの全画像を順に開いて書き出す。各画像は保存済みの設定で、設定の無い画像は
 * 「始めたときに画面に出ていた設定」で書き出す（その設定をその画像の設定として保存する）。
 * 書き出し済みで、設定・元画像がその後変わっていない画像は飛ばす（force で作り直す）
 */
async function exportAll({ force = false } = {}) {
  if (batch.running) return batchReport();
  if (!state.server) {
    showMessage('全画像の書き出しはサーバありのときだけ使えます', 'error');
    return null;
  }
  await flushSave();
  await flushAutoExport();
  await refreshImages();
  const base = JSON.stringify(state.params);
  const startName = state.remote ? state.name : null;
  Object.assign(batch, { running: true, abort: false, total: state.images.length, done: 0, exported: 0, skipped: 0,
    failed: 0, current: null, startedAt: performance.now(), force: !!force, errors: [] });
  state.lastBatch = '';
  rebuild();
  await postBatch({ started: true });
  batchTicker = setInterval(() => showBatch(), 1000);
  try {
    for (let i = 0; i < state.images.length && !batch.abort; i++) {
      const item = state.images[i];
      batch.current = item.name;
      if (!force && item.exportFresh) {
        batch.skipped += 1;
      } else {
        try {
          const hadParams = item.hasParams;
          if (!hadParams) state.params = JSON.parse(base);     // 設定の無い画像には、始めたときの設定を使う
          await openIndex(i);
          if (state.name !== item.name || !state.remote) throw new Error('画像を開けませんでした');
          if (!hadParams) await saveNow();
          state.solo = null;
          renderNow();
          const blob = await encodeCurrent();
          if (!blob) throw new Error('書き出しの途中で描き直しが入りました');
          await uploadExport(item.name, blob);
          batch.exported += 1;
        } catch (e) {
          batch.failed += 1;
          batch.errors.push(`${item.name}: ${e.message}`);
        }
      }
      batch.done += 1;
      await postBatch();
      await new Promise((resolve) => setTimeout(resolve, 0));  // 画面の更新と中止ボタンに順番を回す
    }
  } finally {
    clearInterval(batchTicker);
    batch.running = false;
    batch.current = null;
    const r = batchReport();
    state.lastBatch = `${batch.abort ? '中止しました' : '終わりました'}: 書き出し ${r.exported} / 飛ばし ${r.skipped}`
      + ` / 失敗 ${r.failed}（全 ${r.total} 枚・${formatSpan(r.elapsed)}）`
      + (r.errors.length ? `\n${r.errors.join('\n')}` : '');
    await postBatch();
    // 始める前に開いていた画像へ戻る
    const back = startName ? state.images.findIndex((x) => x.name === startName) : -1;
    if (back >= 0 && state.name !== startName) await openIndex(back);
    autoExport.dirty = false;
    rebuild();
    showMessage(`全画像の書き出し: ${state.lastBatch}`, batch.failed ? 'error' : 'info');
  }
  return batchReport();
}

// ---- 起動 ----

async function init() {
  try {
    renderer = new Renderer($('gl'));
  } catch (e) {
    showMessage(e.message, 'error');
    $('dropHint').hidden = true;
    throw e;
  }
  const [config, list] = await Promise.all([getJSON('api/config'), getJSON('api/images')]);
  state.server = !!(list && Array.isArray(list.images));
  state.images = state.server ? list.images : [];
  state.project = state.server && config && config.project ? config.project : null;
  if (state.project) {
    $('projectName').textContent = state.project.name;
    $('projectName').title = `プロジェクト: ${state.project.path}`;
    $('projectName').hidden = false;
  }
  state.params = defaultParams();
  try { state.autoExport = localStorage.getItem(AUTO_EXPORT_KEY) !== '0'; } catch { state.autoExport = true; }
  $('btnBatchAbort').addEventListener('click', abortExportAll);
  resetHistory();
  $('modeBadge').textContent = state.server ? 'サーバ' : 'サーバなし';
  $('linkManga').hidden = !state.server;          // 漫画の構成（/manga/）は inkdeus のサーバが配信する

  wireViewer();
  wireFiles();
  rebuild();
  resizeCanvas();

  connectEvents();
  if (state.images.length) {
    await refreshImages();
    // /?image=<画像名> で、その画像を開いた状態で始める（漫画の構成の画面の「調整」から来る）
    const wanted = new URLSearchParams(location.search).get('image');
    const index = wanted ? state.images.findIndex((i) => i.name === wanted) : 0;
    if (wanted && index < 0) showMessage(`指定の画像が見つかりません: ${wanted}`, 'error');
    await openIndex(Math.max(0, index));
  } else if (state.server) {
    showMessage('入力フォルダに画像がありません', 'error');
  }
}

// テスト・自動化用の小さな入口（描画結果を数値で確かめるためのもの）
const hooks = {
  ready: null,
  state,
  info: () => ({
    server: state.server, name: state.name, width: renderer.W, height: renderer.H,
    hasMask: state.hasMask, maxTextureSize: renderer.maxTextureSize,
    unit: effectiveUnit(state.params, renderer.W),
    renderer: (() => {
      const gl = renderer.gl;
      const ext = gl.getExtension('WEBGL_debug_renderer_info');
      return ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER);
    })(),
  }),
  clientId: CLIENT_ID,
  styles: () => Object.keys(STYLES),
  schema: () => SCHEMA,
  rev: () => state.rev,
  /** 描画結果の統計をその場で計算して返す（サーバへ送る物と同じ形） */
  stats: () => computeStats(),
  lastPostedStats: () => lastStats,
  defaults: () => defaultParams(),
  getParams: () => JSON.parse(JSON.stringify(state.params)),
  /** 設定を差し替えてその場で描画する。戻り値は { ms, passes, warnings } */
  setParams: (raw) => {
    const warnings = loadParams(raw);
    const r = renderNow() || { ms: 0, passes: [] };
    return { ms: r.ms, passes: r.passes, warnings };
  },
  render: () => renderNow(),
  open: async (name) => { await openIndex(state.images.findIndex((i) => i.name === name)); return hooks.info(); },
  save: async () => { clearTimeout(saveTimer); saveTimer = null; await saveNow(); },
  readPixels: (x, y, w, h) => renderer.readPixels(x, y, w, h),
  readPixelsBase64: (x, y, w, h) => {
    const px = renderer.readPixels(x, y, w, h);
    let s = '';
    for (let i = 0; i < px.length; i += 0x8000) s += String.fromCharCode.apply(null, px.subarray(i, i + 0x8000));
    return btoa(s);
  },
  watermark: () => ({ ...effectiveWatermark(state.params), rect: renderer.wmRect }),
  exportPNG: () => exportNow(),
  /** 自動書き出しの状態（テスト・確認用） */
  autoExport: () => ({ enabled: state.autoExport, dirty: autoExport.dirty, running: !!autoExport.running,
    pending: !!autoExport.timer, count: autoExport.count, label: $('exportState').textContent }),
  setAutoExport: (on) => { setAutoExport(on); rebuild(); },
  exportAll: (opts) => exportAll(opts || {}),
  abortExportAll,
  batch: () => batchReport(),
  undo, redo,
};
window.inkdeus = hooks;
hooks.ready = init();
hooks.ready.catch((e) => console.error(e));
