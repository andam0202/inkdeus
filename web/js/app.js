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
// このブラウザ画面の ID。自分の保存で出たイベントを「外部からの変更」と取り違えないために使う
const CLIENT_ID = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
const HISTORY_MAX = 200;
const ZOOM_MIN = 0.02;
const ZOOM_MAX = 32;

const state = {
  server: false,        // サーバの API が使えるか
  owner: false,         // 所有者モード（透かしを外せる）
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
    result = renderer.render(state.params, { solo: state.solo, owner: state.owner });
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
  live: () => requestRender(),
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
}
const undo = () => stepHistory(-1);
const redo = () => stepHistory(1);

/** 任意の JSON を設定として読み込む（警告があれば表示する） */
function loadParams(raw, { fresh = false, save = true } = {}) {
  const { params, warnings } = normalize(raw, { owner: state.owner });
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
  const stats = renderer.stats(state.params, { solo: state.solo, owner: state.owner });
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
    else if (ev.type === 'open') openByName(ev.name);
    else if (ev.type === 'export') openByName(ev.name).then(() => exportNow()).catch((e) => console.warn(e));
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
      if (JSON.stringify(state.params) !== JSON.stringify(defaultParams({ owner: state.owner }))) await saveNow();
    }
    postState();
    renderNow();
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

async function exportNow() {
  if (!renderer || !renderer.W) {
    showMessage('画像が読み込まれていません', 'error');
    return null;
  }
  setBusy(true);
  await new Promise((resolve) => setTimeout(resolve, 0));
  try {
    state.solo = null;                          // ソロ表示のまま書き出さない
    renderNow();
    setBusy(true);
    const { W, H } = renderer;
    const blob = await encodeGrayPNG(W, H, (y0, rows) => renderer.readPixels(0, y0, W, rows));
    let result;
    if (state.server && state.remote) {
      const r = await fetch(apiPath('export', state.name), {
        method: 'POST', body: blob,
        headers: { 'Content-Type': 'image/png', ...(state.rev ? { 'X-Inkdeus-Rev': state.rev } : {}) },
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const j = await r.json();
      result = { path: j.path, bytes: blob.size };
      state.lastExport = `保存しました: ${j.path}`;
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
  state.owner = !!(state.server && config && config.owner === true);
  state.images = state.server ? list.images : [];
  state.project = state.server && config && config.project ? config.project : null;
  if (state.project) {
    $('projectName').textContent = state.project.name;
    $('projectName').title = `プロジェクト: ${state.project.path}`;
    $('projectName').hidden = false;
  }
  state.params = defaultParams({ owner: state.owner });
  resetHistory();
  $('modeBadge').textContent = state.server ? (state.owner ? 'サーバ・所有者モード' : 'サーバ') : 'サーバなし';

  wireViewer();
  wireFiles();
  rebuild();
  resizeCanvas();

  connectEvents();
  if (state.images.length) {
    await refreshImages();
    await openIndex(0);
  } else if (state.server) {
    showMessage('入力フォルダに画像がありません', 'error');
  }
}

// テスト・自動化用の小さな入口（描画結果を数値で確かめるためのもの）
const hooks = {
  ready: null,
  state,
  info: () => ({
    server: state.server, owner: state.owner, name: state.name, width: renderer.W, height: renderer.H,
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
  defaults: () => defaultParams({ owner: state.owner }),
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
  watermark: () => ({ ...effectiveWatermark(state.params, state.owner), rect: renderer.wmRect }),
  exportPNG: () => exportNow(),
  undo, redo,
};
window.inkdeus = hooks;
hooks.ready = init();
hooks.ready.catch((e) => console.error(e));
