// 画面全体の制御: 入出力（サーバあり/なし）、履歴、表示の拡大縮小、パネルとの橋渡し。

import { Renderer } from './renderer.js';
import { encodeGrayPNG } from './png.js';
import { buildPanel } from './panel.js';
import { createGallery } from './gallery.js';
import { createTools } from './tools.js';
import { createStyleUI, customOf } from './styles.js';
import { layerTarget, isTarget, unionRect } from './paint.js';
import { createNotices, packTiles, choose } from './safety.js';
import {
  SCHEMA, normalize, defaultParams, applyStyle, effectiveUnit, effectiveWatermark, STYLES,
  withoutIds, withoutParts, inheritIds, layerIds, regionLabel, isPartName,
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
const PAINT_SAVE_DELAY_MS = 1000;                  // 筆を置き終えてから、この時間が経ったら消しゴム・ペンを保存する
const PAINT_HISTORY_BYTES = 256 * 1024 * 1024;     // 筆の取り消し用に持つ画素の上限（超えたら古い履歴から捨てる）
const STROKE_SLOW_MS = 40;                         // 筆の最中の合成がこれより遅い環境では、合成の頻度を落とす
const RETRY_MIN_MS = 1000;                         // 保存に失敗したときの再試行の間隔（失敗のたびに倍にする）
const RETRY_MAX_MS = 30000;
const KEEPALIVE_MAX = 60000;                       // ページを閉じる間際でも送り切れる本体の大きさ（ブラウザの上限は合計 64KB）
const ZOOM_MIN = 0.02;
const ZOOM_MAX = 32;

const state = {
  server: false,        // サーバの API が使えるか
  images: [],           // サーバの画像一覧（いま開いている画像フォルダの物）
  folder: 'default',    // いま開いている画像フォルダ（'default' = 既定。プロジェクトは複数の画像フォルダを持てる）
  folders: [],          // 画像フォルダの一覧 [{name, label, counts}]（古いサーバ・1つだけのときは切り替えを出さない）
  index: -1,
  name: null,           // 表示中の画像名
  version: '',          // 表示中のバージョン（'' = 既定。同じ画像から作る、設定の違う白黒版）
  versionsOK: false,    // サーバがバージョンに対応しているか（古いサーバにつないだときは既定だけで動く）
  idsOK: false,         // サーバが層の id を知っているか（知らない古いサーバへは、id を除いて保存する）
  paintOK: false,       // サーバが消しゴム・ペンを保存できるか（できなければメモリ上だけで働く）
  paintTilesOK: false,  // サーバが「変わったタイルだけの保存」に対応しているか（筆を置いた直後に送る）
  backupOK: false,      // サーバが世代バックアップ（以前の状態に戻す）に対応しているか
  broken: null,         // 開いた画像の設定ファイルが壊れている（利用者が扱いを選ぶまで、保存しない）
  build: null,          // 読み込んだときのサーバのビルド識別（変わったら「再読み込みしてください」と知らせる）
  partsOK: true,        // 部分（髪・肌・服など）まわりの設定を保存できるか（知らない古いサーバへは、除いて保存する）
  parts: [],            // 表示中の画像にマスクのある部分名（部分マップに入っている順）
  partNames: [],        // いまの画像フォルダの部分名（部分マスクの置き場にある部分）
  highlight: null,      // 強調表示している領域（'person' | 'background' | 部分名）
  userStylesOK: false,  // サーバが自分用スタイル（名前を付けて保存したスタイル）に対応しているか
  remote: false,        // 表示中の画像がサーバの一覧のものか（ドロップした画像は false）
  params: null,
  rev: null,            // 表示中の設定に対応する設定ファイルの版（未保存の変更があるときは古い）
  hasMask: false,
  solo: null,           // { region, index } でその層だけ表示
  selLayer: null,       // { region, id } 消しゴムを当てる「選択中の層」（層の行をクリックして選ぶ。ソロとは別）
  tab: 'person',        // トーンのパネルで開いているタブ
  project: null,
  lastExport: '',
  autoExport: true,     // 変更を自動で書き出す（漫画の構成の画面へ反映するため）
  batchForce: false,    // 一括書き出しで、書き出し済みの画像も作り直す
  batchDefaultOnly: false,   // 一括書き出しで、既定のバージョンだけを書き出す
  batchAllFolders: false,    // 一括書き出しで、すべての画像フォルダを書き出す（既定は、いまのフォルダ）
  lastBatch: '',        // 直近の一括書き出しの結果
  view: { zoom: 1, cx: 0, cy: 0 },
  compare: false,
  split: 0.5,           // 比較の仕切り位置（キャンバス幅に対する割合）
  original: false,
};

let renderer = null;
let notices = null;                     // ヘッダの下の知らせの帯（更新・サーバの再起動・見つからない元画像の場所）
let gallery = null;                     // 画像一覧パネル（サーバありのときだけ）
let tools = null;                       // プレビューの上のツールバー（手のひら / 消しゴム / ペン）
let styleUI = null;                     // 自分用スタイル（一覧・保存・管理のダイアログ）
let history = [];
let hsteps = [];                        // history と同じ並び。その状態へ進むときの筆（消しゴム・ペンの1筆）の記録。筆でなければ null
let hpos = -1;
const historyStash = new Map();         // バージョン名 → { history, hsteps, hpos }（表示中の画像の、ほかのバージョンの履歴）
let saveTimer = null;
let saving = false;
let statsTimer = null;
let lastStats = null;
let renderPending = false;
let drawPending = false;
let lastRenderMs = 0;
let renderSeq = 0;                      // 描画のたびに増える（書き出しの途中で絵が変わったことを知るため）

// ---- 表示まわりの小物 ----

function showMessage(text, kind = 'info', timeout = kind === 'error' ? 0 : 9000, action = null) {
  const box = document.createElement('div');
  box.className = `msg ${kind}`;
  box.textContent = text;
  box.title = 'クリックで閉じる';
  box.addEventListener('click', () => box.remove());
  if (action) {
    // 知らせに付けるボタン（「取り消す」など）
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'msg-action';
    button.textContent = action.label;
    button.addEventListener('click', (e) => { e.stopPropagation(); box.remove(); action.run(); });
    box.append(' ', button);
  }
  $('messages').append(box);
  if (timeout) setTimeout(() => box.remove(), timeout);
  return box;
}

function setBusy(on) {
  $('busy').hidden = !on;
}

// ---- 保存の状態（ヘッダに常に出す）----
// 「保存済み」「保存中…」「未保存の変更あり」「保存に失敗（再試行中）」。変更が無いときは、最後の出来事（「外部から更新」など）

let cleanLabel = '';
const saveFail = { params: false, paint: false, delay: RETRY_MIN_MS, timer: null, notified: false };

/** 保存の状態。state: none（保存先が無い）/ failed / saving / dirty / saved */
function saveStatus() {
  if (!canAutoSave()) return { state: 'none', label: cleanLabel };
  if (saveFail.params || saveFail.paint) return { state: 'failed', label: '保存に失敗（再試行中）' };
  if (saving || paintSync.running || paintSync.sending > 0) return { state: 'saving', label: '保存中…' };
  if (saveTimer || paintSync.timer) return { state: 'dirty', label: '未保存の変更あり' };
  return { state: 'saved', label: cleanLabel };
}

function refreshSaveState() {
  const status = saveStatus();
  $('saveState').textContent = status.label;
  $('saveState').dataset.state = status.state;
}

/** 変更が無いときに出す文言を決める（「保存済み」「外部から更新」など） */
function setSaveState(text) {
  cleanLabel = text;
  refreshSaveState();
}

/** 保存していない変更がある（保存の途中・再試行中を含む）。ページを閉じる前の確認に使う */
function hasUnsaved() {
  return canAutoSave() && saveStatus().state !== 'saved';
}

/** 保存に失敗した。つながらない・サーバ側の一時的な失敗なら、変更をメモリに持ったまま間隔を広げながら再試行する */
function saveFailed(kind, text, error) {
  const retry = !error.status || error.status >= 500;          // 4xx は、やり直しても同じ結果になる
  if (!retry) {
    showMessage(`${text}: ${error.message}`, 'error');
    setSaveState('保存に失敗');
    return;
  }
  saveFail[kind] = true;
  if (!saveFail.notified) {
    saveFail.notified = true;
    showMessage(`${text}: ${error.message}\n変更はこの画面が持っています。つながり次第、自動で保存します（この画面を閉じないでください）`, 'error');
  }
  clearTimeout(saveFail.timer);
  saveFail.timer = setTimeout(retrySaves, saveFail.delay);
  saveFail.delay = Math.min(RETRY_MAX_MS, saveFail.delay * 2);
  refreshSaveState();
}

/** 保存できた（失敗していたなら、復旧を知らせる） */
function saveRecovered(kind) {
  if (!saveFail[kind]) return;
  saveFail[kind] = false;
  if (!saveFail.params && !saveFail.paint) {
    clearTimeout(saveFail.timer);
    saveFail.timer = null;
    saveFail.delay = RETRY_MIN_MS;
    if (saveFail.notified) showMessage('保存できました（サーバにつながりました）');
    saveFail.notified = false;
  }
}

function resetSaveFail() {
  clearTimeout(saveFail.timer);
  Object.assign(saveFail, { params: false, paint: false, delay: RETRY_MIN_MS, timer: null, notified: false });
}

async function retrySaves() {
  saveFail.timer = null;
  if (saveFail.params) await saveNow();
  if (saveFail.paint) await savePaint();
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

const DEFAULT_FOLDER = 'default';
const FOLDER_KEY = 'inkdeus.folder';            // 最後に開いていた画像フォルダ
const FOLDER_LAST_KEY = 'inkdeus.folderLast';   // 画像フォルダ → 最後に開いていた画像名
/** クエリ文字列（version と folder。既定の物は付けない＝これまでの URL のまま） */
function queryOf(version = '', folder = state.folder, extra = '') {
  const parts = [];
  if (version) parts.push(`version=${encodeURIComponent(version)}`);
  if (folder && folder !== DEFAULT_FOLDER) parts.push(`folder=${encodeURIComponent(folder)}`);
  if (extra) parts.push(extra);
  return parts.length ? `?${parts.join('&')}` : '';
}
const apiPath = (kind, name, version = '', folder = state.folder) => `api/${kind}/${encodeURIComponent(name)}`
  + queryOf(version, folder);
const folderOf = (ev) => (typeof ev.folder === 'string' && ev.folder ? ev.folder : DEFAULT_FOLDER);
const multiFolder = () => state.folders.length > 1;
const folderLabel = (name) => (state.folders.find((f) => f.name === name) || {}).label || name;

function readLast() {
  try { return JSON.parse(localStorage.getItem(FOLDER_LAST_KEY) || '{}') || {}; } catch { return {}; }
}
function rememberLast() {
  if (!state.remote || !state.name) return;
  try {
    const last = readLast();
    last[state.folder] = state.name;
    localStorage.setItem(FOLDER_LAST_KEY, JSON.stringify(last));
    localStorage.setItem(FOLDER_KEY, state.folder);
  } catch { /* 覚えられなくても動く */ }
}

// ---- バージョンの記録（state.images[].versions。画像単位の項目は既定のバージョンの物） ----

const VERSION_NAME_RE = /^[\p{L}\p{N}_-]{1,32}$/u;
const versionLabel = (version) => version || '既定';

/** その画像のバージョンの記録（無ければ null）。古いサーバの一覧には versions が無いので、既定の1件を補う */
function verOf(item, version = '') {
  if (!item) return null;
  if (!Array.isArray(item.versions) || !item.versions.length) {
    item.versions = [{ name: '', hasParams: !!item.hasParams, hasExport: !!item.hasExport, exportFresh: !!item.exportFresh,
      exportFile: item.exportFile, exportVersion: item.exportVersion }];
  }
  return item.versions.find((v) => v.name === version) || null;
}

/** バージョンの状態（設定あり・書き出し済み・最新か）を書き換えて、一覧の印を新しくする */
function mark(name, version, patch) {
  const item = state.images.find((i) => i.name === name);
  if (!item) return;
  const rec = verOf(item, version);
  if (rec) Object.assign(rec, patch);
  if (version === '') Object.assign(item, patch);
  if (gallery) gallery.update(name);
}

const currentItem = () => (state.remote ? state.images.find((i) => i.name === state.name) : null) || null;

// ---- 描画 ----

function drawNow() {
  drawPending = false;
  if (!renderer) return;
  const canvas = $('gl');
  renderer.draw({
    zoom: state.view.zoom, cx: state.view.cx, cy: state.view.cy,
    original: state.original,
    split: state.compare && !state.original ? state.split * canvas.width : -1,
    highlight: highlightOf(state.highlight),
  });
  $('zoomLabel').textContent = renderer.W ? `${Math.round(state.view.zoom * 100)}%` : '-';
  const handle = $('splitHandle');
  handle.hidden = !state.compare;
  handle.style.left = `${state.split * 100}%`;
}

/** 別の設定にしてある部分（その画像にマスクがあり、linked でない）か */
const partOwn = (name) => {
  const own = state.params && state.params.regions[name];
  return !!own && own.linked === false;
};

/** 強調表示する領域を、表示シェーダ向けの形にする */
function highlightOf(region) {
  if (!region || !renderer || !renderer.W || !state.params) return null;
  const code = state.parts.indexOf(region) + 1;
  if (region !== 'person' && region !== 'background' && !code) return null;
  let own = 0;
  state.parts.forEach((name, i) => { if (partOwn(name) && !renderer.dropped.includes(name)) own |= 1 << (i + 1); });
  return { kind: region === 'person' ? 1 : region === 'background' ? 2 : 3, part: code, own,
    grow: state.params.regions.background.grow * effectiveUnit(state.params, renderer.W) };
}

function setHighlight(region) {
  if (state.highlight === (region || null)) return;
  state.highlight = region || null;
  requestDraw();
}

/** 画像の座標 (x, y) がどの領域か（'person' | 'background' | 部分名）。画像の外は null */
function regionAt(x, y) {
  if (!renderer || !renderer.W || !state.params) return null;
  const found = renderer.regionAt(x, y, state.params.regions.background.grow * effectiveUnit(state.params, renderer.W));
  if (!found) return null;
  if (found.part) return found.part;
  return found.inside ? 'person' : 'background';
}

/** トーンのタブを切り替える（その画像に無い部分は選べない） */
function setTab(tab) {
  state.tab = ['person', 'background', ...state.parts].includes(tab) ? tab : 'person';
  rebuild();
}

let droppedKey = '';
/** 層の総数がこの環境の上限を超えて、人物と同じ設定で描いた部分があれば知らせる（変わったときだけ） */
function noteDropped() {
  const key = renderer ? renderer.dropped.join() : '';
  if (key === droppedKey) return;
  droppedKey = key;
  if (key) {
    showMessage(`層の数がこの環境で一度に扱える数（${renderer.maxLayers} 層）を超えたため、次の部分は人物と同じ設定で描いています: `
      + `${renderer.dropped.map(regionLabel).join('、')}。部分や層を減らしてください`, 'error');
  }
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
    noteDropped();
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
  const dpr = window.devicePixelRatio || 1;
  const barPx = 34 * dpr;
  // 上のツールバー（折り返すと高くなる）と下の表示バーの分を空ける
  const topPx = Math.max(34, $('toolbar').offsetHeight || 0) * dpr;
  const zoom = Math.min(canvas.width / renderer.W, Math.max(1, canvas.height - topPx - barPx) / renderer.H) * 0.98;
  state.view = {
    zoom: Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, zoom)),
    cx: renderer.W / 2,
    cy: renderer.H / 2 + ((barPx - topPx) / 2) / zoom,
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
  if (tools) tools.placeCursor();                      // 筆の大きさを示す円も、表示倍率に合わせる
}

function canvasPoint(e) {
  const canvas = $('gl');
  const rect = canvas.getBoundingClientRect();
  const sx = canvas.width / Math.max(1, rect.width);
  const sy = canvas.height / Math.max(1, rect.height);
  return [(e.clientX - rect.left) * sx, (e.clientY - rect.top) * sy];
}

/** ポインタの位置 → 画像の座標（px。画像の外も返す） */
function imagePoint(e) {
  const canvas = $('gl');
  const [px, py] = canvasPoint(e);
  const v = state.view;
  return [(px - canvas.width / 2) / v.zoom + v.cx, (py - canvas.height / 2) / v.zoom + v.cy];
}

/** 画像の座標 → 画面（ビューポート）の座標 */
function clientPoint(x, y) {
  const canvas = $('gl');
  const rect = canvas.getBoundingClientRect();
  const v = state.view;
  return [rect.left + ((x - v.cx) * v.zoom + canvas.width / 2) * (rect.width / Math.max(1, canvas.width)),
    rect.top + ((y - v.cy) * v.zoom + canvas.height / 2) * (rect.height / Math.max(1, canvas.height))];
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
    // Alt+クリック: その場所の領域（人物 / 背景 / 部分）のタブへ切り替える
    if (e.altKey && e.button === 0) {
      e.preventDefault();
      const region = regionAt(...imagePoint(e));
      if (region) setTab(region);
      return;
    }
    try { canvas.setPointerCapture(e.pointerId); } catch { /* 取れなくても動く */ }
    // 消しゴム・ペンの最中は、左ドラッグは筆。移動は中ボタンか、スペースを押しながらのドラッグ
    if (tools && tools.pointerDown(e)) return;
    drag = { p: canvasPoint(e), cx: state.view.cx, cy: state.view.cy };
    canvas.classList.add('dragging');
  });
  canvas.addEventListener('pointermove', (e) => {
    if (tools && tools.pointerMove(e)) return;
    if (!drag) return;
    const [px, py] = canvasPoint(e);
    state.view.cx = drag.cx - (px - drag.p[0]) / state.view.zoom;
    state.view.cy = drag.cy - (py - drag.p[1]) / state.view.zoom;
    requestDraw();
  });
  const end = () => { if (tools) tools.pointerUp(); drag = null; canvas.classList.remove('dragging'); };
  canvas.addEventListener('pointerup', end);
  canvas.addEventListener('pointercancel', end);
  canvas.addEventListener('pointerleave', () => { if (tools) tools.pointerLeave(); });

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
    || (t.tagName === 'INPUT' && ['text', 'number', 'search'].includes(t.type)));
  const plain = (e) => !typing(e.target) && !e.ctrlKey && !e.metaKey && !e.altKey;
  window.addEventListener('keydown', (e) => {
    if (e.code === 'Space' && !typing(e.target)) {
      e.preventDefault();
      // 消しゴム・ペンの最中のスペースは「一時的に手のひら」。手のひらのときは従来どおり元画像を表示
      if (!e.repeat && !(tools && tools.setSpace(true))) showOriginal(true);
      return;
    }
    if (e.code === 'KeyO' && plain(e)) {                         // O: 押している間だけ元画像（どのツールでも）
      e.preventDefault();
      if (!e.repeat) showOriginal(true);
      return;
    }
    // E 消しゴム / B ペン / H 手のひら / X 切り替え。消しゴム・ペンの最中の [ ] は太さ
    if (tools && !batch.running && tools.keyDown(e)) {
      e.preventDefault();
      return;
    }
    if ((e.key === '[' || e.key === ']') && plain(e)) {
      e.preventDefault();
      if (!e.repeat) stepImage(e.key === ']' ? 1 : -1);        // 前後の画像（絞り込み中はその中で）
      return;
    }
    const mod = e.ctrlKey || e.metaKey;
    if (!mod || typing(e.target)) return;
    const k = e.key.toLowerCase();
    if (k === 'z' && !e.shiftKey) { e.preventDefault(); undo(); }
    else if (k === 'y' || (k === 'z' && e.shiftKey)) { e.preventDefault(); redo(); }
  });
  window.addEventListener('keyup', (e) => {
    if (e.code === 'Space' && !typing(e.target)) {
      e.preventDefault();
      if (tools) tools.setSpace(false);
      showOriginal(false);
    } else if (e.code === 'KeyO') showOriginal(false);
  });
  window.addEventListener('blur', () => {
    showOriginal(false);
    if (tools) { tools.setSpace(false); tools.pointerUp(); }
  });
  // タブを離れる・閉じるときは、待ち時間を待たずに保存を済ませる（設定と、消しゴム・ペン）。
  // ページが消える間際でも届くよう、小さな本体は keepalive で送る
  document.addEventListener('visibilitychange', () => {
    if (document.hidden && !batch.running) flushSave({ keepalive: true }).catch((e) => console.warn(e));
  });
  window.addEventListener('pagehide', () => {
    if (hasUnsaved() && !batch.running) flushSave({ keepalive: true }).catch(() => {});
  });
  // 保存していない変更・保存の途中があるときは、閉じる・再読み込みの前に確かめる（その間に保存を進める）
  window.addEventListener('beforeunload', (e) => {
    if (!hasUnsaved()) return;
    flushSave({ keepalive: true }).catch(() => {});
    e.preventDefault();
    e.returnValue = '';
  });
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
  setTab: (tab) => setTab(tab),
  highlight: (region) => setHighlight(region),
  selectLayer: (region, id) => selectLayer(region, id),
  painted: (target) => !!renderer && renderer.paint.has(target),
  style: (key, region) => applyStyleTo(key, region),
  // 自分用スタイル（その領域の選択欄に出す物）。サーバが対応していなければ null（選択欄に何も足さない）
  userStyles: (region) => (state.userStylesOK && styleUI ? styleUI.listFor(region) : null),
  exportNow: () => exportNow(),
  restore: () => openRestore(),
  setAutoExport: (on) => setAutoExport(on),
  exportAll: () => exportAll({ force: state.batchForce, defaultOnly: state.batchDefaultOnly,
    allFolders: state.batchAllFolders }),
  setBatchAllFolders: (on) => { state.batchAllFolders = !!on; },
  setBatchForce: (on) => { state.batchForce = !!on; },
  setBatchDefaultOnly: (on) => { state.batchDefaultOnly = !!on; },
  saveJSON: () => download(new Blob([JSON.stringify(state.params, null, 2) + '\n'], { type: 'application/json' }),
    `${state.name ? stemOf(state.name) : 'inkdeus'}${state.version ? `@${state.version}` : ''}.inkdeus.json`),
  openJSON: () => $('fileParams').click(),
  openMask: () => $('fileMask').click(),
};

/**
 * スタイルを領域に当てる（組み込みも自分用も同じ経路。取り消せる）。
 * key は組み込みのスタイルのキー、または @user:<名前> / @project:<名前>（自分用）、@save / @manage（ダイアログを開く）
 */
function applyStyleTo(key, region) {
  if (key === '@save' || key === '@manage') {
    if (styleUI && state.userStylesOK) (key === '@save' ? styleUI.openSave : styleUI.openManage)(region);
    return false;
  }
  const mine = /^@(user|project):(.*)$/.exec(key);
  let custom = null;
  if (mine) {
    const row = styleUI ? styleUI.find(mine[1], mine[2]) : null;
    if (!row) {
      showMessage(`スタイルが見つかりません: ${mine[2]}`, 'error');
      return false;
    }
    custom = customOf(row);
  }
  state.params = applyStyle(state.params, key, region, custom);
  state.solo = null;
  commit(); rebuild(); requestRender();
  return true;
}

/** いまの設定の、その領域の中身（自分用スタイルとして保存する形）。人物と同じ設定で描いている背景・部分は、人物の物 */
function regionData(region, withLevels) {
  const regions = state.params.regions;
  const own = regions[region] || regions.person;
  const tone = own.linked === true ? regions.person : own;
  const data = { solid: { ...tone.solid }, layers: tone.layers.map(({ id, ...rest }) => rest) };
  if (withLevels) data.levels = { ...tone.levels };
  let target = 'any';
  if (region === 'background') {
    data.mode = own.mode;
    if (own.mode !== 'keep') target = 'background';     // 「白」「線だけ」は背景にしか意味が無い
  }
  return { data, target };
}

function rebuild() {
  buildPanel($('panel'), ctx);
  $('btnUndo').disabled = hpos <= 0;
  $('btnRedo').disabled = hpos >= history.length - 1;
  usedKey = renderer ? renderer.paint.targets().join() : '';
  if (tools) tools.refresh();
}

function resetHistory() {
  history = [JSON.stringify(state.params)];
  hsteps = [null];
  hpos = 0;
}

const historyBytes = () => hsteps.reduce((n, step) => n + (step ? step.bytes : 0), 0);

/** 履歴が上限（件数・筆の画素の量）を超えたら、古い物から捨てる */
function trimHistory() {
  while (history.length > 1 && (history.length > HISTORY_MAX || historyBytes() > PAINT_HISTORY_BYTES)) {
    history.shift();
    hsteps.shift();
    hsteps[0] = null;                 // 先頭になった状態より前へは戻れない
    hpos -= 1;
  }
}

/** いまの状態を履歴の末尾に積む（step は、その状態へ進むときの筆の記録） */
function pushHistory(snap, step) {
  history.length = hpos + 1;
  hsteps.length = hpos + 1;
  history.push(snap);
  hsteps.push(step);
  hpos = history.length - 1;
  trimHistory();
  $('btnUndo').disabled = hpos <= 0;
  $('btnRedo').disabled = true;
}

/** いまの設定を履歴に積む。save=false は「すでにファイルと同じ内容」のとき（外部からの更新） */
function commit(save = true) {
  const snap = JSON.stringify(state.params);
  if (snap === history[hpos]) return;
  pushHistory(snap, null);
  if (save) scheduleSave();
  touchAutoExport();
  touchPaint();                       // 層が消えた・戻った・入れ替わったら、その層の消しゴムの保存も合わせる
}

/** 取り消し・やり直し。スライダーなどの設定の変更と、消しゴム・ペンの1筆が、行った順に戻る */
function stepHistory(delta) {
  const next = hpos + delta;
  if (next < 0 || next >= history.length || (tools && tools.stroking())) return;
  // 筆の記録は「その状態へ進むとき」の物。戻るときは今の状態の記録を、進むときは次の状態の記録を当てる
  const step = hsteps[delta < 0 ? hpos : next];
  if (step) {
    const usedBefore = renderer.paint.has(step.target);
    const revBefore = renderer.paint.rev(step.target);
    renderer.paint.swap(step);
    sendTiles(step, usedBefore, revBefore);          // 筆の取り消し・やり直しも、変わった所だけをすぐ送る
  }
  const before = history[hpos];
  hpos = next;
  state.params = JSON.parse(history[hpos]);
  state.solo = null;
  rebuild();
  requestRender();
  if (history[hpos] !== before) scheduleSave();
  touchPaint();
  touchAutoExport();
}
const undo = () => stepHistory(-1);
const redo = () => stepHistory(1);

/** 層の id だけが違う設定を「同じ」と見なす比較用の文字列 */
const sig = (params) => JSON.stringify(withoutIds(params));

/**
 * 任意の JSON を設定として読み込む（警告があれば表示する）。
 * fresh でなければ、id の書かれていない層は同じ位置にあった層の id を引き継ぐ。
 * 戻り値の filledIds は「id の無い層に新しい id を振った」（保存し直す必要がある）
 */
function loadParams(raw, { fresh = false, save = true } = {}) {
  const { params, warnings, filledIds } = normalize(fresh ? raw : inheritIds(raw, state.params), state.partNames);
  state.params = params;
  state.solo = null;
  if (warnings.length) showMessage(`設定の読み込みで気になる点がありました:\n・${warnings.join('\n・')}`, 'info', 20000);
  if (fresh) resetHistory(); else commit(save);
  rebuild();
  return Object.assign(warnings, { filledIds });
}

function canAutoSave() {
  // 壊れた設定ファイルを開いている間は、利用者が扱いを選ぶまで何も保存しない（上書きしない）
  return state.server && state.remote && !!state.name && !state.broken;
}

function scheduleSave() {
  if (!canAutoSave()) return;
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => { saveTimer = null; saveNow(); }, SAVE_DELAY_MS);
  refreshSaveState();
}

/** サーバへ保存する形の設定（サーバが知らない項目を除く） */
function forServer(params) {
  let out = params;
  if (!state.partsOK) out = withoutParts(out);
  if (!state.idsOK) out = withoutIds(out);
  return out;
}

/** 設定を保存する。idsOnly は「層の id を補っただけ」の保存（見た目は変わらないので、白黒版を「古い」にしない） */
async function saveNow({ idsOnly = false, keepalive = false } = {}) {
  if (!canAutoSave()) return true;
  const name = state.name;
  const version = state.version;
  // 層の id・部分の項目を知らない古いサーバは、それらのある設定を外部操作（ctl）で受け付けなくなるので、除いて保存する
  const body = JSON.stringify(forServer(state.params));
  saving = true;
  refreshSaveState();
  try {
    const r = await fetch(apiPath('params', name, version), {
      method: 'PUT', body,
      headers: { 'Content-Type': 'application/json', 'X-Inkdeus-Client': CLIENT_ID,
        ...(idsOnly ? { 'X-Inkdeus-Ids-Only': '1' } : {}) },
      keepalive: keepalive && body.length <= KEEPALIVE_MAX,
    });
    if (!r.ok) throw Object.assign(new Error(`HTTP ${r.status}`), { status: r.status });
    const j = await r.json();
    mark(name, version, { hasParams: true });
    if (name === state.name && version === state.version) state.rev = j.rev;
    saveRecovered('params');
    if (!saveTimer) setSaveState('保存済み');
    return true;
  } catch (e) {
    saveFailed('params', '設定を保存できませんでした', e);
    return false;
  } finally {
    saving = false;
    refreshSaveState();
    scheduleStats();
  }
}

/**
 * 待っている保存（設定と、消しゴム・ペン）を、いますぐ済ませる。
 * 戻り値は「保存していない変更が残っていない」（サーバにつながらないときは false）
 */
async function flushSave({ keepalive = false } = {}) {
  let ok = true;
  if (saveTimer || saveFail.params) {
    clearTimeout(saveTimer);
    saveTimer = null;
    ok = await saveNow({ keepalive });
  }
  if (!(await flushPaint({ keepalive }))) ok = false;
  return ok;
}

// ---- 消しゴム・ペン（人が画面で描く物）: 筆・履歴・保存 ----

const paintSync = {
  timer: null, running: null, again: false, loading: false,
  onServer: new Set(),                // サーバに保存してある対象
  saved: new Map(),                   // 対象 → 保存してある版（renderer.paint.rev）
  // 筆を置いた直後の「変わったタイルだけの保存」
  chain: Promise.resolve(),           // 送る順番を守るための列
  sending: 0,                         // 送っている最中の数
  sent: new Map(),                    // 対象 → 送った分が届けばサーバがなるはずの版
  tileSaves: 0,                       // タイルで保存できた回数（確認用）
};
let usedKey = '';                     // パネルに出している「消しあり」の印の元（変わったらパネルを作り直す）
let strokeDirty = null;               // 筆の最中に変わった範囲（まだ合成し直していない分）
let strokePending = false;
let lastStrokeMs = 0;

/** 消しゴム・ペンをサーバへ保存できるか（できなければメモリ上だけで働く。書き出しには反映される） */
const paintPersistent = () => state.paintOK && canAutoSave();

/** その対象が、いまの設定から参照されているか（消された層・入れ替わった層のマスクは保存しない） */
function paintReferenced(target) {
  return !target.startsWith('layer-') || layerIds(state.params).has(target.slice(6));
}

/** 描いた物があって、いまの設定で効いている対象（消した層のマスクは、取り消しに備えて持っているが数えない） */
const paintTargets = () => (renderer ? renderer.paint.targets().filter(paintReferenced) : []);

const paintURL = (name, target, version, folder, extra = '') => `api/paint/${encodeURIComponent(name)}`
  + (target ? `/${encodeURIComponent(target)}` : '') + queryOf(version, folder, extra);

function resetPaintSync() {
  clearTimeout(paintSync.timer);
  paintSync.timer = null;
  paintSync.again = false;
  paintSync.onServer.clear();
  paintSync.saved.clear();
  paintSync.sent.clear();
  strokeDirty = null;
}

/** 表示中の画像・バージョンの消しゴム・ペンを読み込む（別の画像の物は引き継がない） */
async function loadPaint() {
  resetPaintSync();
  if (!paintPersistent()) return;
  const name = state.name;
  const version = state.version;
  const folder = state.folder;
  const list = await getJSON(paintURL(name, '', version, folder));
  for (const row of (list && Array.isArray(list.targets) ? list.targets : [])) {
    // 設定に無い層のマスク（外部の変更で層が入れ替わった残りなど）は、効かないので読まない
    if (!isTarget(row.target) || !paintReferenced(row.target)) continue;
    try {
      const r = await fetch(paintURL(name, row.target, version, folder, `v=${row.stamp || 0}`), { cache: 'no-store' });
      if (r.status === 422) {
        // 保存してある絵が壊れている（0 バイトなど）。上書きはされない（次に保存するとき、.broken-<時刻> として残る）
        throw new Error('保存してある絵が壊れています。出力パネルの「以前の状態に戻す…」で戻せます');
      }
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const bitmap = await createImageBitmap(await r.blob(), BITMAP_OPTS);
      try {
        paintSync.saved.set(row.target, renderer.paint.load(row.target, bitmap));
      } finally {
        bitmap.close();
      }
      paintSync.onServer.add(row.target);
    } catch (e) {
      showMessage(`消しゴム・ペンを読み込めませんでした（${row.target}）: ${e.message}`, 'error');
    }
  }
}

/** 消しゴム・ペンが変わったときに呼ぶ。少し待ってから、変わった対象だけ保存する */
function touchPaint() {
  if (!paintPersistent() || !renderer) return;
  if (!renderer.paint.info.size && !paintSync.onServer.size) return;
  clearTimeout(paintSync.timer);
  paintSync.timer = setTimeout(() => { paintSync.timer = null; savePaint(); }, PAINT_SAVE_DELAY_MS);
  refreshSaveState();
}

/**
 * 1 筆（または筆の取り消し・やり直し）で変わったタイルだけを、いますぐサーバへ送る。
 * 全体の PNG を作って保存するのは 1 秒ほど後だが、それを待たずに、ポインタを離した時点で変更がサーバへ渡る
 * （送る本体は連長圧縮で数 KB。ページを閉じる間際でも届くよう keepalive で送る）。
 * サーバの絵がこの筆の直前の状態と同じだと分かっているときだけ使い、そうでなければ後の全体の保存に任せる
 */
function sendTiles(step, usedBefore, revBefore) {
  if (!step || !state.paintTilesOK || !paintPersistent() || !paintReferenced(step.target)) return;
  const { paint } = renderer;
  const target = step.target;
  const name = state.name;
  const version = state.version;
  const folder = state.folder;
  // 設定がまだ保存されていない画像は、先に設定を保存する必要がある（全体の保存がそれを行う）
  if (!(verOf(state.images.find((i) => i.name === name), version) || {}).hasParams) return;
  const known = paintSync.sent.has(target) ? paintSync.sent.get(target)
    : (paintSync.onServer.has(target) ? paintSync.saved.get(target) : (usedBefore ? null : revBefore));
  if (known !== revBefore) return;
  let body;
  const rev = paint.rev(target);
  try {
    const size = paint.dims(target);
    body = packTiles(size.w, size.h, size.channels, paint.readTiles(target, step.tiles));
  } catch (e) {
    console.warn('タイルを読めませんでした', e);
    return;
  }
  const here = () => state.remote && state.name === name && state.version === version && state.folder === folder;
  paintSync.sent.set(target, rev);
  paintSync.sending += 1;
  refreshSaveState();
  const request = fetch(paintURL(name, target, version, folder), {
    method: 'PATCH', body, headers: { 'X-Inkdeus-Client': CLIENT_ID, 'Content-Type': 'application/octet-stream' },
    keepalive: body.length <= KEEPALIVE_MAX,
  });
  request.catch(() => {});
  paintSync.chain = paintSync.chain.then(async () => {
    try {
      const r = await request;
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const j = await r.json();
      if (!here()) return;
      paintSync.saved.set(target, rev);
      if (j.deleted) paintSync.onServer.delete(target); else paintSync.onServer.add(target);
      paintSync.tileSaves += 1;
      mark(name, version, { exportFresh: false });            // いまの白黒版は、もう最新ではない
    } catch (e) {
      // 届かなかった・サーバの絵と合わなかった。この対象は、後の全体の保存でそろえる
      if (here()) { paintSync.sent.delete(target); touchPaint(); }
    } finally {
      paintSync.sending -= 1;
      if (here() && !saveTimer && !paintSync.timer && !paintSync.running && !paintSync.sending) setSaveState('保存済み');
      else refreshSaveState();
    }
  });
}

/** 変わった対象を保存し、無くなった対象（空になった・層が消えた）をサーバからも消す */
function savePaint({ keepalive = false } = {}) {
  if (!paintPersistent()) return Promise.resolve();
  if (paintSync.running) {
    paintSync.again = true;
    return paintSync.running;
  }
  if (tools && tools.stroking()) {      // 筆の最中には保存しない
    touchPaint();
    return Promise.resolve();
  }
  const name = state.name;
  const version = state.version;
  const folder = state.folder;
  const here = () => state.remote && state.name === name && state.version === version && state.folder === folder;
  const headers = { 'X-Inkdeus-Client': CLIENT_ID };
  const { paint } = renderer;
  paintSync.running = (async () => {
    let changed = false;
    try {
      await paintSync.chain;                // 先に送ったタイルの保存が済んでから（順番を入れ替えない）
      // 設定がまだ保存されていない画像なら、先に設定を保存する（層の消しゴムは、設定に書かれた層の id と結び付くため）
      const wanted = [...paint.info.keys()].some((t) => paint.has(t));
      if (wanted && !(verOf(state.images.find((i) => i.name === name), version) || {}).hasParams) {
        clearTimeout(saveTimer);
        saveTimer = null;
        await saveNow();
        if (!here()) return;
      }
      for (let round = 0; round < 4; round++) {
        paintSync.again = false;
        for (const target of new Set([...paint.info.keys(), ...paintSync.onServer])) {
          if (!here()) return;
          if (paint.has(target) && paintReferenced(target)) {
            if (paintSync.onServer.has(target) && paintSync.saved.get(target) === paint.rev(target)) continue;
            const made = await paint.encode(target);
            if (!here()) return;
            if (made && made.stale) {           // 保存の途中で筆が入った。あとでやり直す
              paintSync.again = true;
              continue;
            }
            if (made) {
              const r = await fetch(paintURL(name, target, version, folder), {
                method: 'PUT', body: made.blob, headers: { ...headers, 'Content-Type': 'image/png' },
                keepalive: keepalive && made.blob.size <= KEEPALIVE_MAX });
              if (!r.ok) throw Object.assign(new Error(`HTTP ${r.status}`), { status: r.status });
              paintSync.saved.set(target, made.rev);
              paintSync.sent.delete(target);
              paintSync.onServer.add(target);
              changed = true;
              continue;
            }
          }
          if (paintSync.onServer.has(target)) {
            const r = await fetch(paintURL(name, target, version, folder), { method: 'DELETE', headers, keepalive });
            if (!r.ok) throw Object.assign(new Error(`HTTP ${r.status}`), { status: r.status });
            paintSync.onServer.delete(target);
            paintSync.saved.delete(target);
            paintSync.sent.delete(target);
            changed = true;
          }
        }
        if (!paintSync.again) break;
      }
      if (changed && here()) mark(name, version, { exportFresh: false });      // いまの白黒版は、もう最新ではない
      if (here()) saveRecovered('paint');
      if (!saveTimer && !paintSync.timer && here()) setSaveState('保存済み');
      if (usedKey !== paint.targets().join()) rebuild();      // 「戻す」で空になった対象の印を消す
    } catch (e) {
      if (here()) saveFailed('paint', '消しゴム・ペンを保存できませんでした', e);
    } finally {
      paintSync.running = null;
      refreshSaveState();
    }
  })();
  refreshSaveState();
  return paintSync.running;
}

/** 待っている消しゴム・ペンの保存を済ませる。戻り値は「保存していない物が残っていない」 */
async function flushPaint({ keepalive = false } = {}) {
  if (paintSync.timer || saveFail.paint) {
    clearTimeout(paintSync.timer);
    paintSync.timer = null;
    await savePaint({ keepalive });
  }
  if (paintSync.running) await paintSync.running;
  await paintSync.chain;
  return !saveFail.paint;
}

/** 消しゴムの対象（'layer' | 'solid' | 'lines' | 'all'）から、実際のマスクの名前と表示名を決める */
function eraseTargetOf(kind) {
  if (kind === 'lines') return { target: 'lines', label: '線画' };
  if (kind === 'all') return { target: 'all', label: 'トーン・ベタ・線画をまとめて' };
  if (kind === 'solid') {
    // ベタは人物・背景・（別の設定にした）部分それぞれにマスクがある。開いているタブの側を消す（マスクの無い画像は全体が人物）
    let region = state.hasMask && state.tab === 'background' ? 'background' : 'person';
    if (state.parts.includes(state.tab) && partOwn(state.tab)) region = state.tab;
    const tabbed = state.hasMask || state.parts.some(partOwn);
    return { target: `solid-${region}`, label: tabbed ? `${regionLabel(region)}のベタ（タブで切り替え）` : 'ベタ' };
  }
  const sel = state.selLayer;
  const owner = sel && state.params ? state.params.regions[sel.region] : null;
  const layers = owner && Array.isArray(owner.layers) ? owner.layers : [];
  const index = sel ? layers.findIndex((l) => l.id === sel.id) : -1;
  if (index < 0) return { error: '消す層を、右のパネルの層の行をクリックして選んでください' };
  return { target: layerTarget(sel.id), label: `${regionLabel(sel.region)}の層${index + 1}`, region: sel.region, index };
}

/** 消しゴムを当てる層を選ぶ（層の行のクリック。パネルは作り直さず、強調だけ付け替える） */
function selectLayer(region, id) {
  state.selLayer = id ? { region, id } : null;
  for (const card of document.querySelectorAll('#panel .layer')) {
    card.classList.toggle('selected', !!id && card.dataset.id === id);
  }
  if (tools) tools.refresh();
}

function beginStroke(target, brush) {
  if (!renderer || !renderer.W || batch.running) return false;
  if (brush.mode === 'restore' && !renderer.paint.has(target)) return false;     // 消した所が無い（戻す物が無い）
  try {
    renderer.paint.beginStroke(target, brush);
    return true;
  } catch (e) {
    showMessage(e.message, 'error');
    return false;
  }
}

/** 筆の最中の画面の更新: 変わった範囲だけを合成し直す（前段のパスは作り直さない） */
function strokeRender() {
  strokePending = false;
  if (!strokeDirty || !renderer) return;
  const rect = strokeDirty;
  strokeDirty = null;
  try {
    renderSeq += 1;
    lastStrokeMs = renderer.render(state.params, { solo: state.solo, rect }).ms;
  } catch (e) {
    showMessage(`描画に失敗しました: ${e.message}`, 'error');
  }
  drawNow();
}

function stampStroke(x, y, radius) {
  strokeDirty = unionRect(strokeDirty, renderer.paint.stamp(x, y, radius));
  if (strokePending || !strokeDirty) return;
  strokePending = true;
  // 合成が重い環境では頻度を落とす（筆そのものは GPU 上で即座に打たれている）
  if (lastStrokeMs > STROKE_SLOW_MS) setTimeout(() => requestAnimationFrame(strokeRender), Math.min(250, lastStrokeMs * 2));
  else requestAnimationFrame(strokeRender);
}

/** 1筆を終える: 履歴に1件として積み、正規の合成をして、保存と自動書き出しを予約する */
function endStroke() {
  const active = renderer.paint.stroke;
  const usedBefore = active ? renderer.paint.has(active.target) : false;
  const revBefore = active ? renderer.paint.rev(active.target) : 0;
  const step = renderer.paint.endStroke();
  strokeDirty = null;
  // ポインタを離したら、まず変わった所だけをサーバへ送る（描画や履歴より先に。保存の取りこぼしの窓をなくす）
  sendTiles(step, usedBefore, revBefore);
  if (step) pushHistory(JSON.stringify(state.params), step);
  renderNow();
  if (!step) return;
  touchPaint();
  touchAutoExport();
  if (usedKey !== renderer.paint.targets().join()) rebuild();
}

/** 対象に描いた物を全部消す（取り消せる） */
function clearTarget(target) {
  if (!renderer || !renderer.W || batch.running || (tools && tools.stroking())) return false;
  const step = renderer.paint.clear(target);
  if (step) pushHistory(JSON.stringify(state.params), step);
  rebuild();
  renderNow();
  touchPaint();
  touchAutoExport();
  return !!step;
}

/** どこかで（ctl・別のタブ）消しゴム・ペンが変わった。開いている物なら読み直す */
async function onPaintEvent(ev) {
  if (folderOf(ev) !== state.folder) return;
  const version = typeof ev.version === 'string' ? ev.version : '';
  if (ev.source === CLIENT_ID) return;                 // この画面自身の保存（印は保存したときに付けてある）
  mark(ev.name, version, { exportFresh: false });
  if (!state.remote || ev.name !== state.name) return;
  if (version !== state.version) {
    historyStash.delete(version);     // そのバージョンの筆の履歴は、もう当てはまらない
    return;
  }
  if (batch.running) return;
  if (tools) tools.pointerUp();
  paintSync.loading = true;
  try {
    if (paintSync.running) await paintSync.running.catch(() => {});
    renderer.paint.clearAll();
    await loadPaint();
  } finally {
    paintSync.loading = false;
  }
  resetHistory();                     // 筆の履歴は、読み直した絵には当てはまらない
  rebuild();
  renderNow();
  touchAutoExport();
  showMessage('消しゴム・ペンが外部から変更されました（取り消しの履歴は消えます）');
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
  stats.paint.targets = stats.paint.targets.filter(paintReferenced);
  return { name: state.name, version: state.version, rev: state.rev, renderMs: Math.round(lastRenderMs * 10) / 10, ...stats };
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
    await fetch(apiPath('stats', name, state.version), {
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
    body: JSON.stringify({ current: state.remote ? state.name : null, version: state.remote ? state.version : '',
      folder: state.folder, client: CLIENT_ID }),
  }).catch(() => {});
}

/** 設定ファイルが外から書き換えられたときに読み直す（取り消し履歴にも1件として積む） */
async function onExternalParams(ev) {
  if (folderOf(ev) !== state.folder) return;      // 別の画像フォルダの物（一覧はいまのフォルダの分だけ持っている）
  const version = typeof ev.version === 'string' ? ev.version : '';
  // 設定が書き換わったので、いまの白黒版はもう最新ではない（層の id を補っただけの保存では、見た目は変わらない）
  mark(ev.name, version, { hasParams: ev.rev !== null, ...(ev.idsOnly ? {} : { exportFresh: false }) });
  if (ev.source === CLIENT_ID || !state.remote || ev.name !== state.name || version !== state.version) return;
  if (ev.rev === null || ev.rev === state.rev) return;
  const r = await fetch(apiPath('params', ev.name, version), { cache: 'no-store' });
  if (r.status === 422 && ev.name === state.name && version === state.version) {
    showMessage('設定ファイルが外部から書き換えられて、壊れています（0 バイト、または JSON として読めません）。'
      + 'この画面の設定はそのままです。次に保存すると、壊れたファイルは「.broken-<時刻>」として残ります', 'error');
    return;
  }
  if (!r.ok || ev.name !== state.name || version !== state.version) return;
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
  const loaded = loadParams(raw, { save: false });
  state.rev = rev;
  setSaveState('外部から更新');
  renderNow();
  if (loaded.filledIds && state.idsOK) scheduleSave();      // 手で書かれた設定に層の id が無かった。補った id を保存する
}

function connectEvents() {
  if (!state.server || typeof EventSource === 'undefined') return;
  const source = new EventSource('api/events');
  source.onmessage = (msg) => {
    let ev;
    try { ev = JSON.parse(msg.data); } catch { return; }
    if (ev.type === 'params') onExternalParams(ev).catch((e) => console.warn(e));
    else if (ev.type === 'hello') onHello(ev);
    else if (ev.type === 'exported') onExported(ev);
    else if (ev.type === 'versions') onVersions(ev).catch((e) => console.warn(e));
    else if (ev.type === 'paint') onPaintEvent(ev).catch((e) => console.warn(e));
    else if (ev.type === 'styles') { if (styleUI && ev.source !== CLIENT_ID) styleUI.refresh(); }   // 自分用スタイルが外で変わった
    else if (batch.running && (ev.type === 'open' || ev.type === 'export')) return;   // 一括書き出しの最中は受けない
    else if (ev.type === 'open') openByName(ev.name, ev.version, ev.folder).catch((e) => console.warn(e));
    else if (ev.type === 'export') openByName(ev.name, ev.version, ev.folder).then(() => exportNow()).catch((e) => console.warn(e));
    else if (ev.type === 'export-all') exportAll({ force: !!ev.force, defaultOnly: !!ev.defaultOnly, allFolders: !!ev.allFolders }).catch((e) => console.warn(e));
    else if (ev.type === 'export-all-abort') abortExportAll();
  };
}

/** どこかで（この画面・別のタブ・一括書き出し）白黒版が保存された。一覧の印とサムネイルを新しくする */
function onExported(ev) {
  if (folderOf(ev) !== state.folder) return;
  // version はバージョン名、stamp は PNG の更新時刻（古いサーバは version に更新時刻を入れていた）
  const version = typeof ev.version === 'string' ? ev.version : '';
  const stamp = ev.stamp || (typeof ev.version === 'number' ? ev.version : null);
  const rec = verOf(state.images.find((i) => i.name === ev.name), version);
  if (!rec) return;
  const patch = { hasExport: true,
    // 表示中の画像に未反映の変更が残っているなら、まだ「最新」ではない
    exportFresh: !(state.remote && ev.name === state.name && version === state.version && autoExport.dirty) };
  if (ev.file) patch.exportFile = ev.file;
  if (stamp) patch.exportVersion = stamp;
  mark(ev.name, version, patch);
}

/** 前後の画像へ（画像一覧で絞り込んでいるときは、その結果の中で動く） */
function stepImage(delta) {
  if (batch.running || !state.images.length) return;
  const names = gallery ? gallery.visibleNames() : state.images.map((i) => i.name);
  if (!names.length) return;
  const at = names.indexOf(state.name);
  const next = at < 0 ? (delta > 0 ? 0 : names.length - 1) : at + delta;
  if (next < 0 || next >= names.length) return;
  openIndex(state.images.findIndex((i) => i.name === names[next]));
}

/** 画像（とバージョン）を開く。version が null / 未指定なら「指定なし」（その画像を開いていればそのまま、別の画像からなら既定） */
async function openByName(name, version = null, folder = null) {
  if (folder && folder !== state.folder) {
    await switchFolder(folder, name, version || '');       // 別の画像フォルダの画像（ctl open --folder など）
    return;
  }
  const same = state.remote && state.name === name;
  const want = version === null || version === undefined ? (same ? state.version : '') : version;
  if (same && want === state.version) return;
  let index = state.images.findIndex((i) => i.name === name);
  if (index < 0 || !verOf(state.images[index], want)) {
    await refreshImages();          // 起動後に増えた画像・バージョンかもしれない
    index = state.images.findIndex((i) => i.name === name);
  }
  if (index >= 0) await openIndex(index, want);
}

async function refreshImages() {
  const list = await getJSON(`api/images${queryOf()}`);
  if (!list || !Array.isArray(list.images)) return;
  state.images = list.images;
  if (notices && Array.isArray(list.missingFolders)) showMissing(list.missingFolders);
  const sel = $('selImage');
  sel.replaceChildren(...state.images.map((item) => {
    const opt = document.createElement('option');
    opt.value = item.name;
    opt.textContent = `${item.name}${item.hasMask ? '  [マスク]' : ''}`;
    return opt;
  }));
  if (state.remote && state.name) sel.value = state.name;
  $('nav').hidden = state.images.length < 2;
  state.partNames = state.partsOK && Array.isArray(list.parts) ? list.parts.filter(isPartName) : [];
  state.versionsOK = state.images.some((i) => Array.isArray(i.versions));
  if (gallery) gallery.refresh();
  updateVersionUI();
}

// ---- 画像フォルダ（プロジェクトは複数の画像フォルダを持てる） ----

async function refreshFolders() {
  const data = await getJSON('api/folders');       // 古いサーバには無い（そのときは既定のフォルダだけで動く）
  state.folders = data && Array.isArray(data.folders) ? data.folders.filter((f) => !f.missing) : [];
  showProject();
  if (gallery) gallery.setFolders();
  return state.folders;
}

/** ヘッダの「<プロジェクト名> / <フォルダ名>」 */
function showProject() {
  const parts = [];
  if (state.project) parts.push(state.project.name);
  if (multiFolder()) parts.push(folderLabel(state.folder));
  $('projectName').textContent = parts.join(' / ');
  $('projectName').title = [state.project ? `プロジェクト: ${state.project.path}` : '',
    multiFolder() ? `画像フォルダ: ${folderLabel(state.folder)}` : ''].filter(Boolean).join('\n');
  $('projectName').hidden = !parts.length;
}

/**
 * 画像フォルダを切り替える。name を省くと、そのフォルダで最後に開いていた画像（無ければ先頭）を開く。
 * open=false なら一覧を読むだけ（一括書き出し用）
 */
async function switchFolder(folder, name = null, version = '', { open = true } = {}) {
  if (!state.server || folder === state.folder) {
    if (open && name) await openByName(name, version);
    return;
  }
  if (!(await flushSave())) {
    showMessage('保存できていない変更があるため、移動をやめました（サーバにつながり次第、自動で保存されます）', 'error');
    return;
  }
  await flushAutoExport();
  await refreshFolders();
  if (!state.folders.some((f) => f.name === folder)) {
    showMessage(`画像フォルダ「${folder}」が見つかりません`, 'error');
    return;
  }
  rememberLast();
  state.folder = folder;
  state.name = null;                   // いま出ている絵は前のフォルダの物（同じ名前の画像があっても別物として開く）
  state.remote = false;
  state.index = -1;
  historyStash.clear();
  showProject();
  await refreshImages();
  if (gallery) gallery.setFolders();
  if (!open) return;
  if (!state.images.length) {
    showMessage(`画像フォルダ「${folderLabel(folder)}」に画像がありません`, 'error');
    postState();
    return;
  }
  const last = readLast()[folder];
  let index = state.images.findIndex((i) => i.name === (name || last));
  if (name && index < 0) showMessage(`指定の画像が見つかりません: ${name}`, 'error');
  if (index < 0) index = 0;
  // 設定の引き継ぎはフォルダをまたがない。そのフォルダで最後に開いていた画像に設定があれば、それを引き継ぎ元にする
  let seed = defaultParams();
  const from = state.images.find((i) => i.name === last);
  if (from && from.hasParams && from.name !== state.images[index].name) {
    const saved = await getJSON(apiPath('params', from.name));
    if (saved) seed = normalize(saved).params;
  }
  state.params = seed;
  await openIndex(index, name ? version : '');
}

// ---- バージョン（同じ画像から作る、設定の違う白黒版） ----

function updateVersionUI() {
  const item = currentItem();
  const box = $('verBox');
  box.hidden = !(state.server && state.versionsOK && item);
  if (box.hidden) return;
  verOf(item);
  const option = (value, text, disabled = false) => {
    const opt = document.createElement('option');
    opt.value = value;
    opt.textContent = text;
    opt.disabled = disabled;
    return opt;
  };
  const actions = document.createElement('optgroup');
  actions.label = '操作';
  // 操作の値は @ で始める（バージョン名には @ を使えないので、名前とぶつからない）
  actions.append(option('@new', '＋ いまの設定を複製して新しいバージョン…'),
    option('@rename', '名前を変える…', !state.version), option('@delete', '削除…', !state.version));
  const sel = $('selVersion');
  sel.replaceChildren(...item.versions.map((v) => option(v.name, versionLabel(v.name))), actions);
  sel.value = state.version;
}

async function versionRequest(method, body, query = '') {
  const r = await fetch(`api/versions/${encodeURIComponent(state.name)}${queryOf('', state.folder, query)}`, {
    method, headers: { 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined,
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || (r.status === 404 ? 'そのバージョンはありません' : `HTTP ${r.status}`));
  return j;
}

function needVersions() {
  if (!state.server || !state.remote || !state.name) throw new Error('バージョンは、サーバの一覧にある画像でだけ使えます');
  if (batch.running) throw new Error('全画像の書き出しの最中です');
}

/** いまの設定を複製して新しいバージョンを作り、それを開く */
async function createVersion(version) {
  needVersions();
  if (!VERSION_NAME_RE.test(version || '')) throw new Error('バージョン名に使えるのは英数字・かな漢字・_ - だけです（1〜32 文字）');
  const item = currentItem();
  await flushSave();
  if (!(verOf(item, state.version) || {}).hasParams) await saveNow();   // 複製元がまだ保存されていなければ、先に保存する
  await versionRequest('POST', { version, copyFrom: state.version });
  await refreshImages();
  await openIndex(state.images.findIndex((i) => i.name === item.name), version);
  return version;
}

/** 表示中のバージョンが別の名前になった（この画面・別のタブ・ctl のどこからでも）ときの後始末 */
function followRename(from, to) {
  if (state.version !== from) return;
  state.version = to;
  document.title = titleOf();
  postState();
}

async function renameVersion(from, to) {
  needVersions();
  if (!from) throw new Error('既定のバージョンの名前は変えられません');
  if (!VERSION_NAME_RE.test(to || '')) throw new Error('バージョン名に使えるのは英数字・かな漢字・_ - だけです（1〜32 文字）');
  await flushSave();
  await flushAutoExport();
  await versionRequest('PATCH', { version: from, to });
  if (historyStash.has(from)) { historyStash.set(to, historyStash.get(from)); historyStash.delete(from); }
  followRename(from, to);
  await refreshImages();
  return to;
}

/** 表示中のバージョンが消える（消えた）ときに、保存・書き出しの予約を取り下げる（消した物を書き戻さないように） */
async function dropPending() {
  clearTimeout(saveTimer);
  saveTimer = null;
  clearTimeout(statsTimer);
  statsTimer = null;
  clearTimeout(autoExport.timer);
  autoExport.timer = null;
  autoExport.again = false;
  autoExport.dirty = false;
  resetPaintSync();
  resetSaveFail();
  if (paintSync.running) await paintSync.running.catch(() => {});
  await paintSync.chain.catch(() => {});
  if (autoExport.running) await autoExport.running.catch(() => {});
}

const ownDeletes = new Set();            // この画面が削除している最中のバージョン（通知が来ても、開き直しを重ねない）

async function deleteVersion(version) {
  needVersions();
  if (!version) throw new Error('既定のバージョンは削除できません');
  const name = state.name;
  if (version === state.version) await dropPending();
  ownDeletes.add(version);
  try {
    await versionRequest('DELETE', null, `version=${encodeURIComponent(version)}`);
    historyStash.delete(version);
    await refreshImages();
    if (state.name === name && version === state.version) {
      await openIndex(state.images.findIndex((i) => i.name === name), '');
    }
  } finally {
    ownDeletes.delete(version);
  }
}

/** どこかで（別のタブ・ctl）バージョンが作られた・名前が変わった・消された */
async function onVersions(ev) {
  if (folderOf(ev) !== state.folder) return;
  const mine = state.remote && ev.name === state.name;
  if (mine && ev.action === 'rename') {
    if (historyStash.has(ev.version)) { historyStash.set(ev.to, historyStash.get(ev.version)); historyStash.delete(ev.version); }
    followRename(ev.version, ev.to);
  }
  const gone = mine && ev.action === 'delete' && ev.version === state.version && !ownDeletes.has(ev.version);
  if (gone) await dropPending();
  await refreshImages();
  if (gone && state.name === ev.name && state.version === ev.version && !batch.running) {
    showMessage(`バージョン「${ev.version}」は削除されました。既定のバージョンを開きます`);
    await openIndex(state.images.findIndex((i) => i.name === ev.name), '');
  }
}

function wireVersions() {
  const ask = {
    '@new': async () => {
      const name = window.prompt('新しいバージョンの名前（英数字・かな漢字・_ - で 32 文字まで）。いまの設定を複製します', '');
      if (name !== null && name.trim()) await createVersion(name.trim());
    },
    '@rename': async () => {
      const name = window.prompt(`バージョン「${state.version}」の新しい名前`, state.version);
      if (name !== null && name.trim() && name.trim() !== state.version) await renameVersion(state.version, name.trim());
    },
    '@delete': async () => {
      if (window.confirm(`バージョン「${state.version}」を削除します（設定と書き出した PNG を消します）。よろしいですか？`)) {
        await deleteVersion(state.version);
      }
    },
  };
  $('selVersion').addEventListener('change', async () => {
    const value = $('selVersion').value;
    try {
      if (ask[value]) await ask[value]();
      else await openIndex(state.index, value);
    } catch (e) {
      showMessage(e.message, 'error');
    } finally {
      updateVersionUI();                 // 操作の項目を選んだままにしない
    }
  });
}

const titleOf = () => `${state.name}${state.version ? ` @${state.version}` : ''} — inkdeus`;

// ---- 画像・マスクの読み込み ----

async function setImageBlob(blob, name, remote) {
  const bitmap = await createImageBitmap(blob, BITMAP_OPTS);
  try {
    renderer.setImage(bitmap, bitmap.width, bitmap.height);
  } finally {
    bitmap.close();
  }
  state.name = name;
  state.version = '';
  state.remote = remote;
  state.hasMask = false;
  state.parts = [];                    // 部分マスクは画像ごと（setImage が部分マップも捨てている）
  state.highlight = null;
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

/** その画像の部分マスク（髪・肌・服など）を読んで、部分マップを作る。1 枚ずつ読んで足す（大きな画像でもメモリを食わないように） */
async function loadParts(item) {
  renderer.setParts(null);
  state.parts = [];
  if (!state.partsOK || !state.server || !Array.isArray(item.parts)) return;
  for (const part of item.parts.filter(isPartName)) {
    try {
      const r = await fetch(`api/part/${encodeURIComponent(item.name)}/${encodeURIComponent(part)}${queryOf()}`, { cache: 'no-store' });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const bitmap = await createImageBitmap(await r.blob(), BITMAP_OPTS);
      try {
        renderer.addPart(part, bitmap);
      } finally {
        bitmap.close();
      }
    } catch (e) {
      showMessage(`部分マスクを読み込めませんでした（${part}）: ${e.message}`, 'error');
    }
  }
  state.parts = renderer.parts.slice();
}

/** サーバの一覧の i 番目を開く（マスクと保存済みの設定も読む）。version でバージョンを選ぶ（'' = 既定） */
async function openIndex(i, version = '') {
  if (i < 0 || i >= state.images.length) return;
  if (!(await flushSave())) {
    // 保存できていない変更を抱えたまま別の画像を開くと、その変更が失われる
    showMessage('保存できていない変更があるため、移動をやめました（サーバにつながり次第、自動で保存されます）', 'error');
    return;
  }
  await flushAutoExport();             // 前の画像の「未反映の変更」を書き出してから離れる
  state.broken = null;
  if (notices) notices.hide('broken');
  const item = state.images[i];
  if (version && !verOf(item, version)) {
    showMessage(`${item.name} のバージョン「${version}」が見つかりません。既定のバージョンを開きます`, 'error');
    version = '';
  }
  // 同じ画像のバージョンを切り替えるだけなら、画像とマスクは読み直さない。取り消し履歴はバージョンごとに取っておく
  const sameImage = state.remote && state.name === item.name && renderer.W > 0;
  // 筆の履歴は、保存してある絵を読み直せるときだけ取っておける（メモリ上だけの絵は、離れると消える）
  if (sameImage && (paintPersistent() || !hsteps.some(Boolean))) historyStash.set(state.version, { history, hsteps, hpos });
  else if (sameImage) historyStash.delete(state.version);
  else historyStash.clear();
  setBusy(true);
  try {
    if (sameImage) {
      state.lastExport = '';
      state.solo = null;
      state.rev = null;
      setSaveState('');
    } else {
      const r = await fetch(apiPath('image', item.name), { cache: 'no-store' });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      await setImageBlob(await r.blob(), item.name, true);
      if (item.hasMask) {
        const m = await fetch(apiPath('mask', item.name), { cache: 'no-store' });
        if (m.ok) await setMaskBlob(await m.blob());
      }
      await loadParts(item);
      if (!['person', 'background', ...state.parts].includes(state.tab)) state.tab = 'person';
    }
    state.version = version;
    state.index = i;
    document.title = titleOf();
    $('selImage').value = item.name;
    // 保存済みの設定があればそれを読む。無ければ、既定のバージョンは直前の設定を引き継ぎ、名前付きは既定値から始める
    const rec = verOf(item, version);
    const saved = rec && rec.hasParams ? await fetch(apiPath('params', item.name, version), { cache: 'no-store' }) : null;
    const broken = saved && saved.status === 422 ? await saved.json().catch(() => null) : null;
    if (broken && broken.broken) {
      // 設定ファイルが壊れている（0 バイト、JSON として読めない）。既定値を表示するだけで、何も保存しない。
      // どうするかは利用者が選ぶ（最新のバックアップから戻す / 既定値で始める）
      state.broken = { name: item.name, version, backups: Array.isArray(broken.backups) ? broken.backups : [] };
      loadParams({}, { fresh: true });
      setSaveState('設定ファイルが壊れています');
    } else if (saved && saved.ok) {
      const rev = saved.headers.get('X-Inkdeus-Rev');
      const loaded = loadParams(await saved.json(), { fresh: true });
      state.rev = rev;
      // このバージョンを離れたときの履歴が、いまの設定の続きとして使えるなら戻す
      const kept = sameImage ? historyStash.get(version) : null;
      if (kept && sig(JSON.parse(kept.history[kept.hpos])) === sig(state.params)) {
        history = kept.history;
        hsteps = kept.hsteps;
        hpos = kept.hpos;
        state.params = JSON.parse(history[hpos]);
        rebuild();
      } else if (loaded.filledIds && state.idsOK) {
        await saveNow({ idsOnly: true });   // 層の id が入る前に保存された設定。補った id をその場で保存する
      }
      setSaveState('保存済み');
    } else if (version) {
      loadParams({}, { fresh: true });
      await saveNow();
    } else {
      resetHistory();
      rebuild();
      // 既定値と違う設定を引き継いだときは、サーバ（と外部の操作）にも分かるようすぐ保存する
      if (sig(state.params) !== sig(defaultParams())) await saveNow();
    }
    historyStash.delete(version);
    // 消しゴム・ペンは画像・バージョンごと（直前の設定を引き継ぐときも、これは引き継がない）
    if (sameImage) renderer.paint.clearAll();
    await loadPaint();
    rebuild();
    rememberLast();
    postState();
    renderNow();
    // 白黒版がまだ無い（または設定より古い）画像は、開いた時点で書き出しの対象にする
    if (rec && rec.exportFresh) {
      autoExport.dirty = false;
      setExportState(state.autoExport ? '書き出し済み' : '');
    } else touchAutoExport();
  } catch (e) {
    showMessage(`${item.name} を開けませんでした: ${e.message}`, 'error');
  } finally {
    setBusy(false);
    $('btnPrev').disabled = state.index <= 0;
    $('btnNext').disabled = state.index >= state.images.length - 1;
    updateVersionUI();                 // ヘッダの高さが変わりうるので、一覧の位置合わせより先に
    if (gallery) gallery.setCurrent();
  }
  if (state.broken) askBroken();
}

// ---- 壊れた設定ファイル（上書きせず、利用者に選んでもらう）----

/** 壊れた設定ファイルをどうするかを尋ねる。選ぶまでは、この画像には何も保存しない */
async function askBroken() {
  const info = state.broken;
  if (!info) return null;
  const label = `${info.name}${info.version ? ` @${info.version}` : ''}`;
  const latest = info.backups[0];
  notices.show('broken', `${label} の設定ファイルが壊れています（上書きしていません。いまは既定値を表示しています）`,
    { kind: 'error', action: { label: 'どうするか選ぶ…', run: () => askBroken() } });
  const picked = await choose({
    id: 'brokenDialog', title: '設定ファイルが壊れています',
    message: `${label} の設定ファイルは、0 バイトか、JSON として読めません。ファイルは上書きしていません。\n`
      + 'どちらを選んでも、壊れたファイルは「<名前>.broken-<時刻>」として残ります。',
    buttons: [
      ...(latest ? [{ label: `最新のバックアップから戻す（${latest.label}）`, value: 'restore', primary: true }] : []),
      { label: '既定値で始める', value: 'reset' }],
    cancel: 'あとで決める' });
  if (!picked || state.broken !== info) return null;
  return repairBroken(picked);
}

async function repairBroken(action) {
  const info = state.broken;
  if (!info) return null;
  const r = await fetch(`api/repair/${encodeURIComponent(info.name)}${queryOf(info.version)}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action }) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) {
    showMessage(`設定ファイルを直せませんでした: ${j.error || `HTTP ${r.status}`}`, 'error');
    return null;
  }
  state.broken = null;
  notices.hide('broken');
  await refreshImages();
  await openIndex(state.images.findIndex((i) => i.name === info.name), info.version);
  showMessage(action === 'restore' ? `最新のバックアップ（${j.restored ? j.restored.label : ''}）から設定を戻しました`
    : '既定値で始めました', 'info');
  return j;
}

// ---- 以前の状態に戻す（設定と消しゴム・ペンの世代バックアップ）----

const backupURL = () => `api/backups/${encodeURIComponent(state.name)}${queryOf(state.version)}`;
let lastRestore = null;               // 直近に戻したときの「取り消し先」

async function listBackups() {
  if (!state.backupOK || !canAutoSave()) return [];
  const list = await getJSON(backupURL());
  return list && Array.isArray(list.points) ? list.points : [];
}

/** その時点（一覧の at、または取り消し先の時刻）へ戻す。戻す前のいまの状態も、サーバが残す */
async function restoreBackup(at) {
  if (!state.backupOK || !canAutoSave()) throw new Error('この画像では使えません');
  if (!(await flushSave())) throw new Error('保存できていない変更があります（サーバにつながりません）');
  const name = state.name;
  const version = state.version;
  const r = await fetch(backupURL(), { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ at }) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
  // 戻した中身は、サーバからの「外部からの変更」の知らせで、この画面に読み込まれる
  lastRestore = { name, version, undo: j.undo };
  if (!j.restored.length) {
    showMessage('何も変わりませんでした（すでにその時点と同じ中身です）');
    return j;
  }
  showMessage(`以前の状態に戻しました（${j.restored.join(' / ')}）`, 'info', 30000,
    { label: '取り消す', run: () => undoRestore().catch((e) => showMessage(e.message, 'error')) });
  return j;
}

/** 直近の「以前の状態に戻す」を取り消す（戻す前の状態へ） */
async function undoRestore() {
  const last = lastRestore;
  if (!last || last.name !== state.name || last.version !== state.version) throw new Error('取り消せる復元がありません');
  const j = await restoreBackup(last.undo);
  return j;
}

async function openRestore() {
  if (!state.backupOK || !canAutoSave()) return null;
  const points = await listBackups();
  const label = `${state.name}${state.version ? ` @${state.version}` : ''}`;
  const at = await choose({
    id: 'restoreDialog', title: '以前の状態に戻す',
    message: `${label} の設定と消しゴム・ペンを、選んだ時点の変更の直前の状態に戻します。\n`
      + '戻す前のいまの状態も残るので、あとで取り消せます。',
    rows: points.map((p) => ({ value: p.at, label: p.label, note: p.what.join(' / ') })),
    empty: 'まだ、戻せる時点がありません（保存のたびに、上書きされる前の版が残っていきます）' });
  if (at === null) return null;
  try {
    return await restoreBackup(at);
  } catch (e) {
    showMessage(`戻せませんでした: ${e.message}`, 'error');
    return null;
  }
}

// ---- サーバの更新・再起動に気づく（ビルド識別）----

/** 読み込んだときのサーバのビルド識別を覚える。この画面のファイルの方が新しければ、サーバの再起動を促す */
function checkBuild(build) {
  if (!state.server) return;
  if (!build) {
    // /api/config にビルド識別が無い = この画面のファイルより古いサーバが動いている
    notices.show('server', 'この画面は、起動中のサーバより新しい版です。サーバを再起動してください', {
      detail: '再起動するまで、新しい機能の一部（バックアップ・筆の即時保存など）は使えません' });
    return;
  }
  state.build = build;
  if (build.stale) {
    notices.show('server', 'サーバの起動後に inkdeus が更新されています。サーバを再起動してください', {
      detail: 'サーバを起動し直すと、新しい機能がすべて使えるようになります' });
  }
}

/** イベントの接続のたびに届く、サーバのビルド識別。読み込んだときと違えば、再読み込みを促す */
function onHello(ev) {
  const build = ev && ev.build;
  if (!build || !state.build) return;
  if (build.boot !== state.build.boot) {
    // サーバが起動し直された。開いている画像を伝え直し、保存に失敗していた分があればすぐ試す
    state.build = { ...state.build, boot: build.boot };
    postState();
    if (saveFail.params || saveFail.paint) { clearTimeout(saveFail.timer); retrySaves(); }
  }
  if (build.id !== state.build.id) {
    notices.show('reload', 'inkdeus が更新されました。再読み込みしてください', {
      detail: '保存していない変更は、再読み込みの前に保存されます',
      action: { label: '保存して再読み込み', run: async () => {
        if (!(await flushSave())) {
          showMessage('保存できていない変更があります。サーバにつながってから再読み込みしてください', 'error');
          return;
        }
        await flushAutoExport().catch(() => {});
        location.reload();
      } } });
  } else if (!build.stale) {
    notices.hide('server');
  }
}

/** 元画像の場所が見つからない画像フォルダがあれば、付け替え方を知らせる */
function showMissing(list) {
  const gone = Array.isArray(list) ? list : [];
  if (!gone.length) {
    notices.hide('missing');
    return;
  }
  const project = state.project ? state.project.name : '<プロジェクト>';
  const where = gone.map((f) => (f.name === DEFAULT_FOLDER ? '既定のフォルダ' : `フォルダ「${f.label}」`)).join('、');
  const hint = gone.map((f) => `inkdeus relink ${project}${f.name === DEFAULT_FOLDER ? '' : ` --folder ${f.name}`} --images <dir>`).join(' / ');
  notices.show('missing', `元画像の場所が見つかりません（${where}）`, { kind: 'error', detail: `${hint} で付け替えてください` });
}

async function openLocalImage(file) {
  await flushSave();
  setBusy(true);
  try {
    await setImageBlob(file, file.name, false);
    resetPaintSync();
    historyStash.clear();
    updateVersionUI();
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

/** PNG をサーバへ保存する（表示中の画像・バージョンの書き出しとして） */
async function uploadExport(name, blob, version = '', folder = state.folder) {
  const r = await fetch(apiPath('export', name, version, folder), {
    method: 'POST', body: blob,
    headers: { 'Content-Type': 'image/png', ...(state.rev ? { 'X-Inkdeus-Rev': state.rev } : {}) },
  });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  const j = await r.json();
  if (folder === state.folder) {
    mark(name, version, { hasExport: true, exportFresh: true, ...(j.mtime ? { exportVersion: Math.round(j.mtime * 1000) } : {}) });
  }
  return { path: j.path, bytes: blob.size, version };
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
      result = await uploadExport(state.name, blob, state.version);
      state.lastExport = `保存しました: ${result.path}`;
      if (snap === JSON.stringify(state.params) && !autoExport.timer) {
        autoExport.dirty = false;
        setExportState('書き出し済み');
      }
    } else {
      const filename = `${stemOf(state.name || 'image')}_inkdeus.png`;       // サーバなしではバージョンを持たない
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
  mark(state.name, state.version, { exportFresh: false });
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
  if (state.solo || renderPending || (tools && tools.stroking())) {
    // ソロ表示の間は書き出さない（その層だけの絵になってしまう）。戻るのを待つ（筆の最中も待つ）
    autoExport.timer = setTimeout(runAutoExport, 500);
    return Promise.resolve();
  }
  const name = state.name;
  const version = state.version;
  const folder = state.folder;
  const here = () => state.name === name && state.version === version && state.folder === folder;
  setExportState('書き出し中…');
  autoExport.running = (async () => {
    try {
      await flushSave();
      const snap = JSON.stringify(state.params);
      const blob = await encodeCurrent();
      if (!blob || !here()) {
        autoExport.again = here();               // 途中で絵が変わった。保存せずにやり直す
        return;
      }
      await uploadExport(name, blob, version, folder);
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
    defaultOnly: !!batch.defaultOnly, allFolders: !!batch.allFolders,
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
 * プロジェクトの全画像・全バージョンを順に開いて書き出す（defaultOnly で既定のバージョンだけ）。
 * それぞれ保存済みの設定で、設定の無い画像（既定のバージョン）は「始めたときに画面に出ていた設定」で書き出す
 * （その設定をその画像の設定として保存する）。
 * 書き出し済みで、設定・元画像がその後変わっていない物は飛ばす（force で作り直す）
 */
async function exportAll({ force = false, defaultOnly = false, allFolders = false } = {}) {
  if (batch.running) return batchReport();
  if (!state.server) {
    showMessage('全画像の書き出しはサーバありのときだけ使えます', 'error');
    return null;
  }
  // 前回の結果を先に片付ける（準備の間に進み具合を見た相手が、前回の件数を今回の物と取り違えないように）
  Object.assign(batch, { abort: false, total: 0, done: 0, exported: 0, skipped: 0, failed: 0, errors: [] });
  await flushSave();
  await flushAutoExport();
  await refreshFolders();
  await refreshImages();
  const base = JSON.stringify(state.params);
  const startFolder = state.folder;
  const startName = state.remote ? state.name : null;
  const startVersion = state.version;
  // 対象の画像フォルダ（既定は、いまのフォルダだけ）。全体の件数を先に数える
  const folders = allFolders && multiFolder() ? state.folders.map((f) => f.name) : [state.folder];
  const targetsOf = (images) => {
    const out = [];
    images.forEach((item, i) => {
      verOf(item);
      for (const rec of item.versions) {
        if (!defaultOnly || rec.name === '') out.push({ i, item, rec });
      }
    });
    return out;
  };
  let total = 0;
  for (const f of folders) {
    const list = f === state.folder ? { images: state.images } : await getJSON(`api/images${queryOf('', f)}`);
    total += targetsOf(list && Array.isArray(list.images) ? list.images : []).length;
  }
  Object.assign(batch, { running: true, abort: false, total, done: 0, exported: 0, skipped: 0,
    failed: 0, current: null, startedAt: performance.now(), force: !!force, defaultOnly: !!defaultOnly,
    allFolders: folders.length > 1, errors: [] });
  state.lastBatch = '';
  rebuild();
  await postBatch({ started: true });
  batchTicker = setInterval(() => showBatch(), 1000);
  try {
    for (const folder of folders) {
      if (batch.abort) break;
      if (folder !== state.folder) await switchFolder(folder, null, '', { open: false });
      const targets = targetsOf(state.images);
      for (let t = 0; t < targets.length && !batch.abort; t++) {
        const { i, item, rec } = targets[t];
        const version = rec.name;
        const label = (folders.length > 1 ? `${folderLabel(folder)} / ` : '') + (version ? `${item.name} @${version}` : item.name);
        batch.current = label;
        // 設定の無い名前付きのバージョン（PNG だけが置いてある物）は、何で描くか決められないので飛ばす
        if ((!force && rec.exportFresh) || (version && !rec.hasParams)) {
          batch.skipped += 1;
        } else {
          try {
            const hadParams = rec.hasParams;
            if (!hadParams) state.params = JSON.parse(base);     // 設定の無い画像には、始めたときの設定を使う
            await openIndex(i, version);
            if (state.name !== item.name || state.version !== version || !state.remote) throw new Error('画像を開けませんでした');
            if (!hadParams) await saveNow();
            state.solo = null;
            renderNow();
            const blob = await encodeCurrent();
            if (!blob) throw new Error('書き出しの途中で描き直しが入りました');
            await uploadExport(item.name, blob, version, folder);
            batch.exported += 1;
          } catch (e) {
            batch.failed += 1;
            batch.errors.push(`${label}: ${e.message}`);
          }
        }
        batch.done += 1;
        await postBatch();
        await new Promise((resolve) => setTimeout(resolve, 0));  // 画面の更新と中止ボタンに順番を回す
      }
    }
  } finally {
    clearInterval(batchTicker);
    batch.running = false;
    batch.current = null;
    const r = batchReport();
    state.lastBatch = `${batch.abort ? '中止しました' : '終わりました'}: 書き出し ${r.exported} / 飛ばし ${r.skipped}`
      + ` / 失敗 ${r.failed}（全 ${r.total} 件・${formatSpan(r.elapsed)}）`
      + (r.errors.length ? `\n${r.errors.join('\n')}` : '');
    showBatch();
    try {
      // 始める前に開いていたフォルダ・画像・バージョンへ戻る
      if (state.folder !== startFolder) await switchFolder(startFolder, null, '', { open: false });
      const back = startName ? state.images.findIndex((x) => x.name === startName) : -1;
      if (back >= 0 && (state.name !== startName || state.version !== startVersion || !state.remote)) {
        await openIndex(back, verOf(state.images[back], startVersion) ? startVersion : '');
      }
    } finally {
      // 外部（ctl export-all --wait）へ「終わった」と知らせるのは、元の画像へ戻ってから
      // （知らせた直後の外部操作が、一括書き出しで最後に開いていた画像を相手にしてしまわないように）
      await postBatch();
    }
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
  const query = new URLSearchParams(location.search);
  notices = createNotices($('notices'));
  const [config, folderData] = await Promise.all([getJSON('api/config'), getJSON('api/folders')]);
  state.folders = folderData && Array.isArray(folderData.folders) ? folderData.folders.filter((f) => !f.missing) : [];
  // 始めに開く画像フォルダ: 直リンクの ?folder= → 最後に開いていたフォルダ → 既定
  let stored = null;
  try { stored = localStorage.getItem(FOLDER_KEY); } catch { /* 無くても動く */ }
  const known = (name) => !!name && state.folders.some((f) => f.name === name);
  const wantedFolder = query.get('folder');
  if (wantedFolder && !known(wantedFolder)) showMessage(`指定の画像フォルダが見つかりません: ${wantedFolder}`, 'error');
  state.folder = known(wantedFolder) ? wantedFolder : (multiFolder() && !query.get('image') && known(stored) ? stored : DEFAULT_FOLDER);
  const list = await getJSON(`api/images${queryOf()}`);
  state.server = !!(list && Array.isArray(list.images));
  state.images = state.server ? list.images : [];
  state.project = state.server && config && config.project ? config.project : null;
  const features = state.server && config && Array.isArray(config.features) ? config.features : [];
  state.idsOK = features.includes('layerIds');
  state.paintOK = features.includes('paint');
  state.paintTilesOK = features.includes('paintTiles');
  state.backupOK = features.includes('backup') && !(config.backup && config.backup.enabled === false);
  checkBuild(state.server && config ? config.build : null);
  showMissing(config && config.missingFolders);
  // 部分（regions.<部分名>）と solid.lineKnockout。サーバなしのときは、画面だけで使える（部分マスクはサーバから読む）
  state.partsOK = !state.server || features.includes('parts');
  state.partNames = state.partsOK && list && Array.isArray(list.parts) ? list.parts.filter(isPartName) : [];
  state.userStylesOK = features.includes('userStyles');
  styleUI = createStyleUI({
    enabled: () => state.userStylesOK,
    hasProject: () => !!state.project,
    clientId: CLIENT_ID,
    regionData: (region, withLevels) => regionData(region, withLevels),
    apply: (row, region) => applyStyleTo(`@${row.origin}:${row.name}`, region),
    changed: () => { if (history.length) rebuild(); },
    message: (text, kind) => showMessage(text, kind),
  });
  if (state.userStylesOK) await styleUI.refresh();
  showProject();
  state.params = defaultParams();
  try { state.autoExport = localStorage.getItem(AUTO_EXPORT_KEY) !== '0'; } catch { state.autoExport = true; }
  $('btnBatchAbort').addEventListener('click', abortExportAll);
  resetHistory();
  $('modeBadge').textContent = state.server ? 'サーバ' : 'サーバなし';
  $('linkManga').hidden = !state.server;          // 漫画の構成（/manga/）は inkdeus のサーバが配信する

  tools = createTools($('toolbar'), $('brushCursor'), {
    canvas: $('gl'),
    hasImage: () => !!renderer && renderer.W > 0,
    locked: () => batch.running,
    unit: () => effectiveUnit(state.params, renderer.W),
    zoom: () => state.view.zoom,
    imagePoint: (e) => imagePoint(e),
    eraseTarget: (kind) => eraseTargetOf(kind),
    used: (target) => !!target && renderer.paint.has(target),
    usedTargets: () => paintTargets(),
    persistent: () => paintPersistent(),
    beginStroke, stamp: stampStroke, endStroke, clearTarget,
    message: (text, kind) => showMessage(text, kind),
  });
  wireViewer();
  wireFiles();
  wireVersions();
  rebuild();
  resizeCanvas();

  connectEvents();
  if (state.images.length) {
    // 画像一覧パネル（サーバなしで1枚ずつ開く使い方では出さない）
    gallery = createGallery($('gallery'), {
      images: () => state.images,
      current: () => (state.remote ? state.name : null),
      open: (name) => { if (name !== state.name || !state.remote) openIndex(state.images.findIndex((i) => i.name === name)); },
      locked: () => batch.running,
      // 画像フォルダの切り替え（フォルダが1つだけなら出さない）と、そのフォルダの素材ソース名
      folders: () => state.folders,
      folder: () => state.folder,
      setFolder: (name) => switchFolder(name).catch((e) => showMessage(e.message, 'error')),
      assetSource: (kind) => (state.folder === DEFAULT_FOLDER ? kind : `${kind}@${state.folder}`),
    });
    await refreshImages();
    // /?image=<画像名>[&version=<バージョン>] で、その画像（のバージョン）を開いた状態で始める
    // （漫画の構成の画面の「調整」から来る）
    // 画像フォルダが複数あって画像の指定が無ければ、そのフォルダで最後に開いていた画像から始める
    const wanted = query.get('image') || (multiFolder() ? readLast()[state.folder] : null);
    const index = wanted ? state.images.findIndex((i) => i.name === wanted) : 0;
    if (query.get('image') && index < 0) showMessage(`指定の画像が見つかりません: ${wanted}`, 'error');
    await openIndex(Math.max(0, index), index >= 0 && query.get('image') ? (query.get('version') || '') : '');
  } else if (state.server) {
    showMessage('入力フォルダに画像がありません', 'error');
  }
}

// テスト・自動化用の小さな入口（描画結果を数値で確かめるためのもの）
const hooks = {
  ready: null,
  state,
  info: () => ({
    server: state.server, name: state.name, version: state.version, folder: state.folder, width: renderer.W, height: renderer.H,
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
  open: async (name, version = '', folder = null) => {
    if (folder && folder !== state.folder) await switchFolder(folder, name, version);
    else await openIndex(state.images.findIndex((i) => i.name === name), version);
    return hooks.info();
  },
  // 画像フォルダ
  folder: () => state.folder,
  folders: () => state.folders.map((f) => f.name),
  setFolder: async (name) => { await switchFolder(name); return hooks.info(); },
  images: () => state.images.map((i) => i.name),
  // バージョン（同じ画像から作る、設定の違う白黒版）
  version: () => state.version,
  versions: () => { const item = currentItem(); verOf(item); return item ? JSON.parse(JSON.stringify(item.versions)) : []; },
  openVersion: async (version) => { await openIndex(state.index, version); return state.version; },
  createVersion, renameVersion, deleteVersion,
  historyInfo: () => ({ length: history.length, pos: hpos }),
  // 自分用スタイル（名前を付けて保存したスタイル）
  userStyles: {
    ok: () => state.userStylesOK,
    list: () => (styleUI ? styleUI.list() : []),
    refresh: () => styleUI.refresh(),
    /** いまの設定の、その領域の中身を保存する。opts = { name, description, scope, includeLevels } */
    save: (opts, region = 'person') => styleUI.save(opts, region),
    remove: (origin, name) => styleUI.remove(origin, name),
    rename: (origin, name, to) => styleUI.rename(origin, name, to),
    overwrite: (origin, name, region = 'person') => styleUI.overwrite(origin, name, region),
    /** スタイルを領域に当てる（選択欄で選ぶのと同じ経路）。当てたら true */
    apply: (origin, name, region = 'person') => { const ok = applyStyleTo(`@${origin}:${name}`, region); renderNow(); return ok; },
  },
  // 部分（髪・肌・服など）と領域
  parts: () => state.parts.slice(),
  partsInfo: () => ({ ok: state.partsOK, parts: state.parts.slice(), names: state.partNames.slice(), tab: state.tab,
    dropped: renderer.dropped.slice(), maxLayers: renderer.maxLayers,
    maxUniformVectors: renderer.maxUniformVectors, maxTextureUnits: renderer.maxTextureUnits }),
  /** 一度に扱える層の総数の上限を決め直す（上限を超えたときの扱いを確かめるための物。普通は環境から決まる） */
  limitLayers: (n) => { renderer.maxLayers = n; renderNow(); return renderer.dropped.slice(); },
  tab: () => state.tab,
  setTab: (tab) => { setTab(tab); return state.tab; },
  regionAt: (x, y) => regionAt(x, y),
  highlight: (region) => { setHighlight(region); return state.highlight; },
  /** 画面のキャンバスに出ている色（画像の座標で指す。[r, g, b, a]）。強調表示の確認用 */
  displayPixel: (x, y) => {
    drawNow();
    const canvas = $('gl');
    const v = state.view;
    return renderer.readCanvas((x - v.cx) * v.zoom + canvas.width / 2, (y - v.cy) * v.zoom + canvas.height / 2);
  },
  // 消しゴム・ペン
  paint: {
    state: () => ({ ...tools.state(), selected: state.selLayer, targets: paintTargets(),
      persistent: paintPersistent(), maskSize: [renderer.paint.mw, renderer.paint.mh],
      saving: !!paintSync.timer || !!paintSync.running, loading: paintSync.loading,
      onServer: [...paintSync.onServer].sort(),
      historyBytes: historyBytes(), steps: hsteps.filter(Boolean).length }),
    setTool: (tool) => tools.setTool(tool),
    set: (tool, values) => tools.set(tool, values),
    setEraseTarget: (kind) => tools.setEraseTarget(kind),
    toggle: () => tools.toggleMode(),
    /** region の index 番目の層を「選択中の層」にする */
    select: (region, index) => selectLayer(region, state.params.regions[region].layers[index].id),
    target: (kind) => eraseTargetOf(kind),
    used: (target) => renderer.paint.has(target),
    clear: (target) => clearTarget(target),
    flush: () => flushPaint(),
    /** 画像の座標 → 画面の座標（ポインタ操作を合成するため） */
    clientPoint: (x, y) => clientPoint(x, y),
    /** 筆を1本、座標の並び [[x, y, 半径(画像px)], …] で直接描く（履歴・保存は画面の操作と同じ道を通る） */
    stroke: (target, brush, points) => {
      if (!beginStroke(target, brush)) return false;
      for (const [x, y, r] of points) stampStroke(x, y, r);
      endStroke();
      return true;
    },
  },
  save: async () => { clearTimeout(saveTimer); saveTimer = null; await saveNow(); },
  /** 保存の状態（state: none / failed / saving / dirty / saved と、ヘッダに出している文言） */
  saveStatus: () => ({ ...saveStatus(), unsaved: hasUnsaved(), failing: saveFail.params || saveFail.paint,
    retryMs: saveFail.delay, tileSaves: paintSync.tileSaves, tiles: state.paintTilesOK }),
  flushSave: (opts) => flushSave(opts || {}),
  /** 以前の状態に戻す（世代バックアップ） */
  backups: {
    ok: () => state.backupOK,
    list: () => listBackups(),
    restore: (at) => restoreBackup(at),
    undo: () => undoRestore(),
    open: () => openRestore(),
  },
  broken: () => (state.broken ? JSON.parse(JSON.stringify(state.broken)) : null),
  repair: (action) => repairBroken(action),
  notices: () => Object.fromEntries(notices.keys().map((k) => [k, notices.text(k)])),
  build: () => state.build,
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
  gallery: () => (gallery ? { ...gallery.state(), visible: gallery.visibleNames() } : null),
  batch: () => batchReport(),
  // 取り消し・やり直し（結果をすぐ読めるよう、その場で描画まで済ませる）
  undo: () => { undo(); renderNow(); },
  redo: () => { redo(); renderNow(); },
};
window.inkdeus = hooks;
hooks.ready = init();
hooks.ready.catch((e) => console.error(e));
