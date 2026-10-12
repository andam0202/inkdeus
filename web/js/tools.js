// プレビューの上のツールバー（手のひら / 消しゴム / ペン）と、プレビュー上での筆の操作。
// 描く物そのもの（GPU 上のマスクとペンの絵）は paint.js、履歴・保存とのつなぎは app.js が受け持つ。

import { el } from './panel.js';

const KEY = 'inkdeus.tools';
const SIZE_STEP = 1.2;               // [ ] で太さを変える割合
const MIN_PRESSURE = 0.05;

const RANGES = {
  eraser: { size: { min: 1, max: 500, step: 1, def: 40 }, hardness: { min: 0, max: 1, step: 0.05, def: 0.8 },
    strength: { min: 0.05, max: 1, step: 0.05, def: 1 } },
  pen: { size: { min: 0.5, max: 100, step: 0.5, def: 3 }, hardness: { min: 0, max: 1, step: 0.05, def: 1 } },
};
const ERASE_TARGETS = [['layer', '選択中の層'], ['solid', 'ベタ'], ['lines', '線画'], ['all', 'すべて']];

const clamp = (v, r) => Math.min(r.max, Math.max(r.min, v));
const snap = (v, r) => Number(clamp(Math.round(v / r.step) * r.step, r).toFixed(3));
const typing = (t) => t && (t.tagName === 'TEXTAREA' || t.tagName === 'SELECT'
  || (t.tagName === 'INPUT' && ['text', 'number', 'search'].includes(t.type)));

function loadSettings() {
  const s = {
    eraser: { size: RANGES.eraser.size.def, hardness: RANGES.eraser.hardness.def, strength: RANGES.eraser.strength.def },
    pen: { size: RANGES.pen.size.def, hardness: RANGES.pen.hardness.def, pressure: true },
  };
  try {
    const saved = JSON.parse(localStorage.getItem(KEY) || '{}') || {};
    for (const tool of ['eraser', 'pen']) {
      for (const [k, r] of Object.entries(RANGES[tool])) {
        const v = saved[tool] && saved[tool][k];
        if (typeof v === 'number' && Number.isFinite(v)) s[tool][k] = snap(v, r);
      }
    }
    if (saved.pen && typeof saved.pen.pressure === 'boolean') s.pen.pressure = saved.pen.pressure;
  } catch { /* 覚えていなくても動く */ }
  return s;
}

/**
 * @param {HTMLElement} root    ツールバーを入れる要素（#toolbar）
 * @param {HTMLElement} cursor  筆の大きさを示す円（#brushCursor）
 * @param {object} host         app.js が渡す橋渡し（下の呼び出しを参照）
 */
export function createTools(root, cursor, host) {
  const settings = loadSettings();
  const view = {
    tool: 'hand',                    // 'hand' | 'eraser' | 'pen'
    eraseTarget: 'layer',
    eraseMode: 'erase',              // 'erase' | 'restore'
    penColor: 'black',               // 'black' | 'white'
    space: false,                    // スペースを押している間は、一時的に手のひら
    stroking: false,
    pointer: null,                   // 最後のポインタ位置（clientX, clientY）
  };
  const save = () => {
    try { localStorage.setItem(KEY, JSON.stringify(settings)); } catch { /* 覚えられなくても動く */ }
  };

  // ---- 部品 ----

  const toolButtons = {};
  const toolButton = (key, text, title) => {
    toolButtons[key] = el('button', { type: 'button', id: `tool${key[0].toUpperCase()}${key.slice(1)}`, title,
      'aria-pressed': 'false', onclick: () => setTool(key) }, text);
    return toolButtons[key];
  };
  const sliders = {};
  /** 太さ・硬さ・強さの行（スライダーと数値欄） */
  const slider = (tool, key, label, title) => {
    const r = RANGES[tool][key];
    const range = el('input', { type: 'range', min: r.min, max: r.max, step: r.step, 'aria-label': label });
    const number = el('input', { type: 'number', min: r.min, max: r.max, step: r.step, 'aria-label': label });
    const show = () => { range.value = settings[tool][key]; number.value = settings[tool][key]; };
    const apply = (raw) => {
      const v = Number(raw);
      if (Number.isFinite(v)) settings[tool][key] = snap(v, r);
      show();
      save();
      placeCursor();
    };
    range.addEventListener('input', () => apply(range.value));
    number.addEventListener('change', () => apply(number.value));
    show();
    sliders[`${tool}.${key}`] = { show };
    return el('label', { class: 'tool-slider', title, id: `${tool}${key[0].toUpperCase()}${key.slice(1)}` },
      el('span', {}, label), range, number);
  };

  const targetSel = el('select', { id: 'eraseTarget', title: '消しゴムを当てる対象' },
    ERASE_TARGETS.map(([value, label]) => el('option', { value }, label)));
  targetSel.addEventListener('change', () => { view.eraseTarget = targetSel.value; refresh(); });
  const targetLabel = el('span', { id: 'eraseTargetLabel', class: 'tool-note' });
  const modeErase = el('button', { type: 'button', id: 'eraseModeErase', title: '消す (X で切り替え)',
    onclick: () => { view.eraseMode = 'erase'; refresh(); } }, '消す');
  const modeRestore = el('button', { type: 'button', id: 'eraseModeRestore', title: '消した所を戻す (X で切り替え)',
    onclick: () => { view.eraseMode = 'restore'; refresh(); } }, '戻す');
  const eraseClear = el('button', { type: 'button', id: 'eraseClear', title: 'この対象の消しゴムを全部取り消す（取り消し Ctrl+Z で戻せます）',
    'aria-label': 'この対象の消しゴムを全部取り消す',
    onclick: () => {
      const found = host.eraseTarget(view.eraseTarget);
      if (found.error) host.message(found.error, 'error');
      else host.clearTarget(found.target);
      refresh();
    } }, '全部取り消す');
  const eraserBox = el('span', { class: 'tool-opts', id: 'eraserOpts', hidden: true },
    targetSel, targetLabel,
    el('span', { class: 'tool-group', role: 'group', 'aria-label': 'モード' }, modeErase, modeRestore),
    slider('eraser', 'size', '太さ', '消しゴムの太さ（基準px）。[ ] で変える'),
    slider('eraser', 'hardness', '硬さ', '1 で縁がくっきり、下げるほど縁がなだらか'),
    slider('eraser', 'strength', '強さ', '消す量の上限（0.5 なら半分まで薄くする）'),
    eraseClear);

  const penBlack = el('button', { type: 'button', id: 'penBlack', title: '黒インク (X で切り替え)',
    onclick: () => { view.penColor = 'black'; refresh(); } }, '黒');
  const penWhite = el('button', { type: 'button', id: 'penWhite', title: '白（修正液） (X で切り替え)',
    onclick: () => { view.penColor = 'white'; refresh(); } }, '白');
  const pressure = el('input', { type: 'checkbox', id: 'penPressure' });
  pressure.checked = settings.pen.pressure;
  pressure.addEventListener('change', () => { settings.pen.pressure = pressure.checked; save(); });
  const penClear = el('button', { type: 'button', id: 'penClear', title: 'ペンで描いた物を全部消す（取り消し Ctrl+Z で戻せます）',
    'aria-label': 'ペンで描いた物を全部消す',
    onclick: () => { host.clearTarget('pen'); refresh(); } }, '全部消す');
  const penBox = el('span', { class: 'tool-opts', id: 'penOpts', hidden: true },
    el('span', { class: 'tool-group', role: 'group', 'aria-label': '色' }, penBlack, penWhite),
    slider('pen', 'size', '太さ', 'ペンの太さ（基準px）。[ ] で変える'),
    slider('pen', 'hardness', '硬さ', '1 で縁がくっきり、下げるほど縁がなだらか'),
    el('label', { class: 'check', title: 'ペンタブレットの筆圧で太さを変える（筆圧の無いマウスは一定）' }, pressure, '筆圧で太さ'),
    penClear);

  const penMark = el('span', { id: 'penMark', class: 'tool-mark', hidden: true, title: 'この絵にはペンで描いた物があります' }, 'ペンあり');
  const eraseMark = el('span', { id: 'eraseMark', class: 'tool-mark', hidden: true, title: 'この絵には消しゴムで消した所があります' }, '消しあり');
  const note = el('span', { id: 'paintNote', class: 'tool-note warn', hidden: true,
    title: '消しゴム・ペンは保存されません。書き出しには反映されます' }, '保存されません（この画面を閉じると消えます）');

  root.replaceChildren(
    el('span', { class: 'tool-group', role: 'group', 'aria-label': 'ツール' },
      toolButton('hand', '手のひら', '移動 (H)。ホイールで拡大縮小'),
      toolButton('eraser', '消しゴム', '消しゴム (E)。移動はスペースを押しながらドラッグ、または中ボタン'),
      toolButton('pen', 'ペン', 'ペン (B)。移動はスペースを押しながらドラッグ、または中ボタン')),
    eraserBox, penBox, eraseMark, penMark, note);

  // ---- 状態の表示 ----

  function refresh() {
    for (const [key, button] of Object.entries(toolButtons)) {
      button.classList.toggle('on', key === view.tool);
      button.setAttribute('aria-pressed', String(key === view.tool));
    }
    eraserBox.hidden = view.tool !== 'eraser';
    penBox.hidden = view.tool !== 'pen';
    targetSel.value = view.eraseTarget;
    modeErase.classList.toggle('on', view.eraseMode === 'erase');
    modeRestore.classList.toggle('on', view.eraseMode === 'restore');
    penBlack.classList.toggle('on', view.penColor === 'black');
    penWhite.classList.toggle('on', view.penColor === 'white');
    const found = host.eraseTarget(view.eraseTarget);
    targetLabel.textContent = found.error ? '（層の行をクリックして選ぶ）' : found.label;
    targetLabel.classList.toggle('warn', !!found.error);
    eraseClear.disabled = !!found.error || !host.used(found.target);
    penClear.disabled = !host.used('pen');
    penMark.hidden = !host.used('pen');
    eraseMark.hidden = !host.usedTargets().some((t) => t !== 'pen');
    note.hidden = host.persistent() || !host.usedTargets().length;
    root.dataset.tool = view.tool;
    host.canvas.classList.toggle('painting', active());
    placeCursor();
  }

  /** 筆で描ける状態か（ツールが消しゴム・ペンで、スペースで一時的に手のひらにしていない） */
  const active = () => view.tool !== 'hand' && !view.space;

  function setTool(tool) {
    if (!['hand', 'eraser', 'pen'].includes(tool) || view.stroking) return;
    view.tool = tool;
    refresh();
  }

  function toggleMode() {
    if (view.tool === 'eraser') view.eraseMode = view.eraseMode === 'erase' ? 'restore' : 'erase';
    else if (view.tool === 'pen') view.penColor = view.penColor === 'black' ? 'white' : 'black';
    else return false;
    refresh();
    return true;
  }

  function resize(direction) {
    const tool = view.tool;
    if (tool === 'hand') return false;
    const r = RANGES[tool].size;
    const now = settings[tool].size;
    let next = snap(direction > 0 ? now * SIZE_STEP : now / SIZE_STEP, r);
    if (next === now) next = snap(now + direction * r.step, r);       // 細い筆でも必ず1段は動く
    settings[tool].size = next;
    sliders[`${tool}.size`].show();
    save();
    placeCursor();
    return true;
  }

  // ---- 筆の大きさを示す円 ----

  const sizeOf = () => (view.tool === 'pen' ? settings.pen.size : settings.eraser.size);

  function placeCursor() {
    const show = active() && view.pointer && host.hasImage() && !host.locked();
    cursor.hidden = !show;
    if (!show) return;
    const d = Math.max(2, (sizeOf() * host.unit() * host.zoom()) / (window.devicePixelRatio || 1));
    const box = host.canvas.getBoundingClientRect();
    cursor.style.width = `${d}px`;
    cursor.style.height = `${d}px`;
    cursor.style.left = `${view.pointer[0] - box.left - d / 2}px`;
    cursor.style.top = `${view.pointer[1] - box.top - d / 2}px`;
  }

  // ---- 筆 ----

  function radiusOf(e) {
    let r = (sizeOf() * host.unit()) / 2;
    // 筆圧はペンタブレットのときだけ使う（マウスは、押している間ずっと 0.5 と報告するため一定に扱う）
    if (view.tool === 'pen' && settings.pen.pressure && e.pointerType === 'pen' && e.pressure > 0) {
      r *= Math.max(MIN_PRESSURE, Math.min(1, e.pressure));
    }
    return r;
  }

  /** プレビューでボタンが押された。筆を始めたら true（そのとき、移動のドラッグは始めない） */
  function pointerDown(e) {
    view.pointer = [e.clientX, e.clientY];
    if (!active() || e.button !== 0 || !host.hasImage() || host.locked()) return false;
    let target = 'pen';
    let brush = { mode: view.penColor, hardness: settings.pen.hardness };
    if (view.tool === 'eraser') {
      const found = host.eraseTarget(view.eraseTarget);
      if (found.error) {
        host.message(found.error, 'error');
        return true;
      }
      target = found.target;
      brush = { mode: view.eraseMode, hardness: settings.eraser.hardness, strength: settings.eraser.strength };
    }
    if (!host.beginStroke(target, brush)) return true;
    view.stroking = true;
    const [x, y] = host.imagePoint(e);
    host.stamp(x, y, radiusOf(e));
    placeCursor();
    return true;
  }

  function pointerMove(e) {
    view.pointer = [e.clientX, e.clientY];
    if (view.stroking) {
      // まとめて届いた動き（高速に動かしたとき）も1点ずつ打つ
      const events = typeof e.getCoalescedEvents === 'function' ? e.getCoalescedEvents() : [];
      for (const ev of events.length ? events : [e]) {
        const [x, y] = host.imagePoint(ev);
        host.stamp(x, y, radiusOf(ev));
      }
    }
    placeCursor();
    return view.stroking;
  }

  function pointerUp() {
    if (!view.stroking) return false;
    view.stroking = false;
    host.endStroke();
    refresh();
    return true;
  }

  function pointerLeave() {
    view.pointer = null;
    placeCursor();
  }

  /** キー操作。扱ったら true */
  function keyDown(e) {
    if (typing(e.target) || e.ctrlKey || e.metaKey || e.altKey || host.locked()) return false;
    const k = e.key.length === 1 ? e.key.toLowerCase() : e.key;
    if (k === 'e') setTool('eraser');
    else if (k === 'b') setTool('pen');
    else if (k === 'h') setTool('hand');
    else if (k === 'x') return toggleMode();
    else if (k === '[' || k === ']') return resize(k === ']' ? 1 : -1);    // 手のひらのときは false（前後の画像）
    else return false;
    return true;
  }

  /** スペースを押している間は、一時的に手のひら（ツール使用中だけ。扱ったら true） */
  function setSpace(on) {
    if (view.tool === 'hand' && !view.space) return false;
    view.space = !!on && view.tool !== 'hand';
    host.canvas.classList.toggle('painting', active());
    placeCursor();
    return true;
  }

  refresh();
  return {
    refresh, setTool, toggleMode, pointerDown, pointerMove, pointerUp, pointerLeave, keyDown, setSpace, active,
    placeCursor,
    tool: () => view.tool,
    stroking: () => view.stroking,
    setEraseTarget: (kind) => { if (ERASE_TARGETS.some(([k]) => k === kind)) { view.eraseTarget = kind; refresh(); } },
    state: () => ({ tool: view.tool, eraseTarget: view.eraseTarget, eraseMode: view.eraseMode, penColor: view.penColor,
      space: view.space, stroking: view.stroking,
      eraser: { ...settings.eraser }, pen: { ...settings.pen } }),
    /** 太さ・硬さ・強さを直接決める（テスト・自動化用） */
    set: (tool, values) => {
      for (const [k, v] of Object.entries(values || {})) {
        if (RANGES[tool] && RANGES[tool][k]) { settings[tool][k] = snap(Number(v), RANGES[tool][k]); sliders[`${tool}.${k}`].show(); }
        else if (tool === 'pen' && k === 'pressure') { settings.pen.pressure = !!v; pressure.checked = !!v; }
      }
      refresh();
    },
  };
}
