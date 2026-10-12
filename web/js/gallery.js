// 画像一覧パネル（画面の左端）。プロジェクトの全画像をサムネイルで並べ、押した画像を開く。
// 一覧そのもの（state.images）は app.js が持ち、ここは表示と絞り込みだけを受け持つ。

import { el } from './panel.js';

const WIDTH = { def: 200, min: 160, maxRatio: 0.5 };
const THUMB_H = 120;
const KEY = 'inkdeus.gallery.';
const FILTERS = [
  ['all', 'すべて', () => true],
  ['noParams', '未設定', (i) => !i.hasParams],
  ['noExport', '未書き出し', (i) => !i.hasExport],
  ['stale', '書き出しが古い', (i) => i.hasExport && !i.exportFresh],
];

const store = {
  get(key, fallback) {
    try {
      const v = localStorage.getItem(KEY + key);
      return v === null ? fallback : JSON.parse(v);
    } catch { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem(KEY + key, JSON.stringify(value)); } catch { /* 覚えられなくても動く */ }
  },
};

const encodePath = (name) => String(name).split('/').map(encodeURIComponent).join('/');

/** 状態の印（色だけに頼らず、文字でも分かるようにする） */
function badgesOf(item) {
  const out = [];
  if (item.hasParams) out.push(['params', '設定', '設定あり（この画像の設定が保存済み）']);
  if (item.hasExport && item.exportFresh) out.push(['fresh', '書出', '書き出し済み（白黒版は最新）']);
  else if (item.hasExport) out.push(['stale', '古い', '書き出しが古い（白黒版が、設定・元画像・マスクより古い）']);
  if (!item.hasMask) out.push(['nomask', 'マスク無', 'マスクなし（人物マスクが無い画像）']);
  return out;
}

/**
 * @param {HTMLElement} root  <aside id="gallery">
 * @param {{images: () => object[], current: () => string|null, open: (name: string) => void,
 *          locked: () => boolean}} host
 */
export function createGallery(root, host) {
  const view = {
    open: store.get('open', true) !== false,
    width: store.get('width', null),
    kind: store.get('kind', 'source') === 'inked' ? 'inked' : 'source',
    q: '',
    filter: 'all',
  };
  const cells = new Map();                      // 画像名 → サムネイルの要素

  const search = el('input', { type: 'search', class: 'gal-search', placeholder: '名前で絞り込み', 'aria-label': '名前で絞り込み' });
  const kind = el('select', { class: 'gal-kind', title: 'サムネイルの種類', 'aria-label': 'サムネイルの種類' },
    el('option', { value: 'source' }, '元画像'), el('option', { value: 'inked' }, '白黒版'));
  kind.value = view.kind;
  const chips = el('div', { class: 'gal-chips', role: 'group', 'aria-label': '状態で絞り込み' });
  const count = el('div', { class: 'gal-count' });
  const grid = el('div', { class: 'gal-grid' });
  const body = el('div', { class: 'gal-body' },
    el('div', { class: 'gal-row' }, search, kind), chips, count, grid);
  const resize = el('div', { class: 'gal-resize', role: 'separator', 'aria-orientation': 'vertical',
    title: 'ドラッグで幅を変える（ダブルクリックで既定の幅）' });
  const toggle = el('button', { type: 'button', class: 'gal-toggle', title: '画像の一覧を開く／閉じる' }, el('span', {}, '画像'));
  root.replaceChildren(body, resize, toggle);
  root.hidden = false;

  // 画面内に入ったサムネイルだけ読み込む（数百枚あっても、開いた直後に全部は取りに行かない）
  const loader = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      const img = entry.target;
      if (img.dataset.src && img.getAttribute('src') !== img.dataset.src) img.src = img.dataset.src;
    }
  }, { root: grid, rootMargin: '300px 0px' });

  // ---- 幅と開閉 ----

  function clampWidth(px) {
    return Math.round(Math.min(Math.max(px, WIDTH.min), Math.max(WIDTH.min, window.innerWidth * WIDTH.maxRatio)));
  }
  function applyLayout() {
    root.classList.toggle('open', view.open);
    toggle.setAttribute('aria-expanded', String(view.open));
    root.style.setProperty('--gal-width', `${view.width === null ? WIDTH.def : view.width}px`);
  }
  function setOpen(open) {
    view.open = !!open;
    store.set('open', view.open);
    applyLayout();
    if (view.open) showCurrent();
  }
  function setWidth(px, save = true) {
    view.width = px === null || !Number.isFinite(px) ? null : clampWidth(px);
    if (save) store.set('width', view.width);
    applyLayout();
  }
  toggle.addEventListener('click', () => setOpen(!view.open));
  let dragging = false;
  resize.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    dragging = true;
    resize.setPointerCapture(e.pointerId);
    document.body.classList.add('gal-resizing');
    e.preventDefault();
  });
  resize.addEventListener('pointermove', (e) => {
    if (dragging) setWidth(e.clientX - root.getBoundingClientRect().left, false);
  });
  const endDrag = () => {
    if (!dragging) return;
    dragging = false;
    document.body.classList.remove('gal-resizing');
    store.set('width', view.width);
  };
  resize.addEventListener('pointerup', endDrag);
  resize.addEventListener('pointercancel', endDrag);
  resize.addEventListener('dblclick', () => setWidth(null));
  window.addEventListener('resize', () => {
    if (view.width !== null && view.width > clampWidth(view.width)) setWidth(view.width, false);
  });

  // ---- 中身 ----

  function thumbOf(item) {
    if (view.kind === 'inked' && item.hasExport && item.exportFile) {
      // 白黒版は書き出しのたびに中身が替わるので、更新時刻を付けて古い物を使い回させない
      return { src: `api/assets/inked/thumb/${encodePath(item.exportFile)}?h=${THUMB_H}&v=${item.exportVersion || 0}`,
        missing: false };
    }
    return { src: `api/assets/source/thumb/${encodePath(item.name)}?h=${THUMB_H}`, missing: view.kind === 'inked' };
  }

  function paint(cell, item) {
    const thumb = thumbOf(item);
    const img = cell.querySelector('img');
    if (img.dataset.src !== thumb.src) {
      img.dataset.src = thumb.src;
      if (img.getAttribute('src')) img.src = thumb.src;       // もう表示している物はその場で差し替える
    }
    cell.classList.toggle('missing', thumb.missing);           // 白黒版がまだ無い（元画像を薄く出している）
    cell.querySelector('.gal-badges').replaceChildren(...badgesOf(item).map(([key, text, title]) => (
      el('span', { class: 'gal-badge', 'data-kind': key, title }, text))));
    cell.title = `${item.name}（${item.width}×${item.height}）${thumb.missing ? ' — 白黒版はまだありません' : ''}`;
  }

  function makeCell(item) {
    const img = el('img', { alt: '', decoding: 'async', draggable: 'false' });
    const cell = el('div', { class: 'gal-item', role: 'button', tabindex: '0', 'data-name': item.name },
      el('div', { class: 'gal-thumb' }, img, el('div', { class: 'gal-badges' })),
      el('span', { class: 'gal-name' }, item.name));
    const open = () => { if (!host.locked()) host.open(item.name); };
    cell.addEventListener('click', open);
    cell.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); }
    });
    paint(cell, item);
    loader.observe(img);
    return cell;
  }

  function matches(item) {
    const needle = view.q.trim().toLowerCase();
    if (needle && !item.name.toLowerCase().includes(needle)) return false;
    return FILTERS.find(([key]) => key === view.filter)[2](item);
  }

  /** 絞り込みに合う画像名（一覧の順）。前後の移動に使う */
  function visibleNames() {
    return host.images().filter(matches).map((i) => i.name);
  }

  function applyFilter() {
    const images = host.images();
    let shown = 0;
    for (const item of images) {
      const cell = cells.get(item.name);
      if (!cell) continue;
      const ok = matches(item);
      cell.hidden = !ok;
      if (ok) shown += 1;
    }
    chips.replaceChildren(...FILTERS.map(([key, label, test]) => el('button', {
      type: 'button', class: `gal-chip${key === view.filter ? ' on' : ''}`, 'data-filter': key,
      'aria-pressed': String(key === view.filter),
      onclick: () => { view.filter = key; applyFilter(); showCurrent(); },
    }, `${label} ${images.filter(test).length}`)));
    count.textContent = `${images.length} 枚中 ${shown} 枚`;
  }

  function showCurrent() {
    const name = host.current();
    for (const [key, cell] of cells) cell.classList.toggle('current', key === name);
    const cell = name ? cells.get(name) : null;
    if (cell && !cell.hidden && view.open) cell.scrollIntoView({ block: 'nearest' });
  }

  /** 一覧を作り直す（画像の増減があったとき） */
  function refresh() {
    loader.disconnect();
    cells.clear();
    for (const item of host.images()) cells.set(item.name, makeCell(item));
    grid.replaceChildren(...cells.values());
    applyFilter();
    showCurrent();
  }

  /** 1枚分の状態（印・サムネイル）を更新する */
  function update(name) {
    const item = host.images().find((i) => i.name === name);
    const cell = cells.get(name);
    if (!item || !cell) return;
    paint(cell, item);
    applyFilter();
  }

  let timer = 0;
  search.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(() => { view.q = search.value; applyFilter(); showCurrent(); }, 120);
  });
  kind.addEventListener('change', () => {
    view.kind = kind.value === 'inked' ? 'inked' : 'source';
    store.set('kind', view.kind);
    for (const item of host.images()) {
      const cell = cells.get(item.name);
      if (cell) paint(cell, item);
    }
  });

  applyLayout();
  refresh();
  return { refresh, update, setCurrent: showCurrent, visibleNames, setOpen, setWidth,
    state: () => ({ open: view.open, width: view.width === null ? WIDTH.def : view.width, kind: view.kind,
      filter: view.filter, q: view.q }) };
}
