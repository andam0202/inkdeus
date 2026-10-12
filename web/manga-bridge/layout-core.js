// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 andam0202
//
// コマ割りの計算（画面にも漫画エディタにも触らない純粋な関数だけ）。
//
// レイアウト = ページを「行と列に分かれた長方形の集まり」として持つ入れ子の木と、全体の設定。
//   {
//     v: 1, seq: <id の通し番号>, rtl: true（右綴じ = 右から左へ読む）,
//     gutter: { v: 行と行の間隔, h: 列と列の間隔 },             … ページの短辺に対する割合
//     margin: { top, right, bottom, left },                       … ページの短辺に対する割合
//     root: 節 | 葉 | null
//   }
//   節 = { id, dir: 'v'（子を上から下へ並べる = 行）| 'h'（子を左から右へ並べる = 列）,
//          ratios: [子の比率…], children: [節 | 葉…],
//          tilt: [境界ごとの [端0のずれ, 端1のずれ] | null …]（短辺に対する割合。斜めの境界） }
//   葉 = { id, guid: 対応するコマの guid, bleed: { top, right, bottom, left }（端まで伸ばす辺）,
//          free: true のとき連動から外れていて、pts（[[x/幅, y/高さ]…]）の形をそのまま使う }
// どの関数も引数のレイアウトを書き換えず、新しいレイアウトを返す。
(function (root) {
  'use strict';

  const MIN_CELL = 0.04;                    // コマの最小の幅・高さ（短辺に対する割合）
  const LIMITS = { gutter: [0, 0.2], margin: [0, 0.4], rows: 12, cols: 12 };
  const DEFAULTS = { gutter: { v: 0.03, h: 0.015 }, margin: { top: 0.05, right: 0.05, bottom: 0.05, left: 0.05 } };
  const SIDES = ['top', 'right', 'bottom', 'left'];

  function fail(message) { throw new Error(message); }
  function clone(value) { return JSON.parse(JSON.stringify(value)); }
  function isLeaf(node) { return !!node && !node.children; }
  function clamp(v, lo, hi) { return Math.min(Math.max(v, lo), hi); }
  function num(value, lo, hi, label) {
    if (typeof value !== 'number' || !isFinite(value)) fail(`${label}は数値で指定してください`);
    if (value < lo || value > hi) fail(`${label}は ${lo}〜${hi} の範囲で指定してください`);
    return value;
  }

  function create(opts) {
    const o = opts || {};
    const L = { v: 1, seq: 0, rtl: o.rtl !== false, gutter: Object.assign({}, DEFAULTS.gutter),
      margin: Object.assign({}, DEFAULTS.margin), root: null };
    return setMargin(setGutter(L, o.gutter || {}), o.margin || {});
  }
  function newId(L, prefix) { L.seq += 1; return prefix + L.seq; }
  function newLeaf(L, guid) { return { id: newId(L, 'c'), guid: guid || null }; }

  // "2,1,3" → [2, 1, 3]（行ごとのコマ数）
  function parseCounts(text) {
    const counts = Array.isArray(text) ? text.slice() : String(text == null ? '' : text).split(/[,\s、]+/).filter(Boolean)
      .map((part) => (/^\d+$/.test(part) ? parseInt(part, 10) : NaN));
    if (!counts.length || counts.length > LIMITS.rows) fail(`行数は 1〜${LIMITS.rows} です（例: 2,1,3）`);
    counts.forEach((n) => {
      if (!Number.isInteger(n) || n < 1 || n > LIMITS.cols) fail(`行ごとのコマ数は 1〜${LIMITS.cols} の整数で書いてください（例: 2,1,3）`);
    });
    return counts;
  }

  // 格子: 行数と、行ごとのコマ数
  function grid(counts, opts) {
    const L = create(opts);
    const rows = parseCounts(counts).map((n) => {
      if (n === 1) return newLeaf(L);
      const node = { id: newId(L, 'n'), dir: 'h', ratios: [], children: [] };
      for (let i = 0; i < n; i++) { node.children.push(newLeaf(L)); node.ratios.push(1 / n); }
      return node;
    });
    L.root = rows.length === 1 ? rows[0]
      : { id: newId(L, 'n'), dir: 'v', ratios: rows.map(() => 1 / rows.length), children: rows };
    return L;
  }

  function walk(node, fn, parent, index) {
    if (!node) return;
    fn(node, parent || null, index === undefined ? -1 : index);
    if (node.children) node.children.forEach((child, i) => walk(child, fn, node, i));
  }
  function find(L, id) {
    let found = null;
    walk(L.root, (node, parent, index) => { if (node.id === id) found = { node, parent, index }; });
    return found;
  }
  function leaves(L) {
    const out = [];
    walk(L.root, (node) => { if (isLeaf(node)) out.push(node); });
    return out;
  }
  // 読む順（上から下。右綴じなら右から左）の葉の id
  function order(L) {
    const out = [];
    (function visit(node) {
      if (!node) return;
      if (isLeaf(node)) { out.push(node.id); return; }
      const kids = node.dir === 'h' && L.rtl !== false ? node.children.slice().reverse() : node.children;
      kids.forEach(visit);
    })(L.root);
    return out;
  }

  function normalizeRatios(node) {
    const sum = node.ratios.reduce((a, b) => a + b, 0) || 1;
    node.ratios = node.ratios.map((r) => r / sum);
  }
  function boundKey(node, index) { return node.id + ':' + index; }
  function tiltOf(node, index) {
    const t = node.tilt && node.tilt[index];
    return t ? [t[0] || 0, t[1] || 0] : [0, 0];
  }

  // レイアウト → 各コマの頂点（絶対座標。左上・右上・右下・左下の順）と境界線
  function compute(L, W, H) {
    const S = Math.min(W, H);
    const cells = {};
    const bounds = {};
    const m = L.margin;
    const page = { l: 0, t: 0, r: W, b: H };
    (function place(node, rect, sides) {
      if (!node) return;
      if (isLeaf(node)) { cells[node.id] = { id: node.id, leaf: node, rect, sides }; return; }
      const n = node.children.length;
      const vertical = node.dir === 'v';
      const g = (vertical ? L.gutter.v : L.gutter.h) * S;
      const extent = vertical ? rect.b - rect.t : rect.r - rect.l;
      const avail = Math.max(extent - g * (n - 1), n * 1e-6);
      const sum = node.ratios.reduce((a, b) => a + b, 0) || 1;
      let at = vertical ? rect.t : rect.l;
      node.children.forEach((child, k) => {
        const size = avail * node.ratios[k] / sum;
        const before = k === 0 ? null : boundKey(node, k - 1);
        const after = k === n - 1 ? null : boundKey(node, k);
        const childRect = vertical ? { l: rect.l, r: rect.r, t: at, b: at + size } : { t: rect.t, b: rect.b, l: at, r: at + size };
        const childSides = vertical
          ? { top: before || sides.top, bottom: after || sides.bottom, left: sides.left, right: sides.right }
          : { left: before || sides.left, right: after || sides.right, top: sides.top, bottom: sides.bottom };
        if (after) {
          const tilt = tiltOf(node, k);
          const pos = at + size + g / 2;
          const lo = vertical ? rect.l : rect.t;
          const hi = vertical ? rect.r : rect.b;
          bounds[after] = { key: after, node: node.id, index: k, dir: node.dir, pos, lo, hi, tilt, gutter: g,
            sizes: [size, avail * node.ratios[k + 1] / sum],
            a: vertical ? [lo, pos + tilt[0] * S] : [pos + tilt[0] * S, lo],
            b: vertical ? [hi, pos + tilt[1] * S] : [pos + tilt[1] * S, hi] };
        }
        place(child, childRect, childSides);
        at += size + g;
      });
    })(L.root, { l: m.left * S, t: m.top * S, r: W - m.right * S, b: H - m.bottom * S }, { top: null, right: null, bottom: null, left: null });

    // 斜めの境界: 境界に接している辺の両端を、境界の傾きに沿ってずらす
    const shift = (key, at) => {
      if (!key) return 0;
      const b = bounds[key];
      const f = b.hi > b.lo ? (at - b.lo) / (b.hi - b.lo) : 0;
      return S * (b.tilt[0] + (b.tilt[1] - b.tilt[0]) * f);
    };
    Object.values(cells).forEach((cell) => {
      const leaf = cell.leaf;
      const r = Object.assign({}, cell.rect);
      const edges = {};
      SIDES.forEach((side) => { edges[side] = cell.sides[side] === null; });
      const bleed = leaf.bleed || {};
      if (edges.top && bleed.top) r.t = page.t;
      if (edges.bottom && bleed.bottom) r.b = page.b;
      if (edges.left && bleed.left) r.l = page.l;
      if (edges.right && bleed.right) r.r = page.r;
      const corner = (x, y, h, v) => [x + shift(cell.sides[h], y), y + shift(cell.sides[v], x)];
      let pts = [corner(r.l, r.t, 'left', 'top'), corner(r.r, r.t, 'right', 'top'),
        corner(r.r, r.b, 'right', 'bottom'), corner(r.l, r.b, 'left', 'bottom')];
      const free = !!(leaf.free && Array.isArray(leaf.pts) && leaf.pts.length >= 3);
      if (free) pts = leaf.pts.map((p) => [p[0] * W, p[1] * H]);
      cell.grid = r;
      cell.edges = edges;
      cell.free = free;
      cell.pts = pts;
      cell.guid = leaf.guid || null;
      delete cell.leaf;
    });
    return { cells, bounds, S, W, H };
  }

  function bbox(pts) {
    const xs = pts.map((p) => p[0]);
    const ys = pts.map((p) => p[1]);
    const l = Math.min.apply(null, xs);
    const t = Math.min.apply(null, ys);
    return { l, t, r: Math.max.apply(null, xs), b: Math.max.apply(null, ys) };
  }
  function area(pts) {
    let sum = 0;
    for (let i = 0; i < pts.length; i++) {
      const a = pts[i];
      const b = pts[(i + 1) % pts.length];
      sum += a[0] * b[1] - b[0] * a[1];
    }
    return Math.abs(sum) / 2;
  }
  function contains(pts, x, y) {
    let inside = false;
    for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) {
      const xi = pts[i][0], yi = pts[i][1], xj = pts[j][0], yj = pts[j][1];
      if (((yi > y) !== (yj > y)) && (x < ((xj - xi) * (y - yi)) / (yj - yi) + xi)) inside = !inside;
    }
    return inside;
  }

  // ---- 設定 ----

  function setGutter(L, gutter) {
    const out = clone(L);
    ['v', 'h'].forEach((k) => {
      if (gutter && gutter[k] !== undefined) out.gutter[k] = num(gutter[k], LIMITS.gutter[0], LIMITS.gutter[1], 'コマの間隔');
    });
    return out;
  }
  function setMargin(L, margin) {
    const out = clone(L);
    SIDES.forEach((k) => {
      if (margin && margin[k] !== undefined) out.margin[k] = num(margin[k], LIMITS.margin[0], LIMITS.margin[1], '外周の余白');
    });
    return out;
  }
  function edit(L, id, fn) {
    const out = clone(L);
    const hit = find(out, id);
    if (!hit || !isLeaf(hit.node)) fail('そのコマはレイアウトにありません');
    fn(hit.node, hit, out);
    return out;
  }
  function setBleed(L, id, side, on) {
    if (SIDES.indexOf(side) === -1) fail('辺は top / right / bottom / left のどれかです');
    return edit(L, id, (leaf) => {
      leaf.bleed = Object.assign({}, leaf.bleed);
      if (on) leaf.bleed[side] = true; else delete leaf.bleed[side];
      if (!Object.keys(leaf.bleed).length) delete leaf.bleed;
    });
  }
  // 連動から外して、その形（[[x/幅, y/高さ]…]）のままにする
  function setFree(L, id, pts) {
    if (!Array.isArray(pts) || pts.length < 3 || !pts.every((p) => Array.isArray(p) && isFinite(p[0]) && isFinite(p[1]))) {
      fail('自由変形の頂点は 3 つ以上の [x, y] で指定してください');
    }
    return edit(L, id, (leaf) => { leaf.free = true; leaf.pts = pts.map((p) => [p[0], p[1]]); });
  }
  function clearFree(L, id) {
    return edit(L, id, (leaf) => { delete leaf.free; delete leaf.pts; });
  }
  function assign(L, id, guid) {
    return edit(L, id, (leaf) => { leaf.guid = guid || null; });
  }

  // ---- 境界 ----

  function boundOf(out, key, W, H) {
    const b = compute(out, W, H).bounds[key];
    if (!b) fail('その境界はレイアウトにありません');
    return { b, node: find(out, b.node).node };
  }
  // スナップの候補: ページの 1/2・1/3・1/4 と、同じ向きのほかの境界の位置
  function snapTargets(L, W, H, key) {
    const C = compute(L, W, H);
    const me = C.bounds[key];
    if (!me) return [];
    const full = me.dir === 'v' ? H : W;
    const out = [1 / 2, 1 / 3, 2 / 3, 1 / 4, 3 / 4].map((f) => full * f);
    Object.values(C.bounds).forEach((b) => { if (b.key !== key && b.dir === me.dir) out.push(b.pos); });
    return out;
  }
  // 境界を delta（画素）だけ動かす。両側のコマが伸縮し、間隔は変わらない。最小の大きさは下回らない
  function moveBoundary(L, key, delta, W, H, opts) {
    const o = opts || {};
    const out = clone(L);
    const { b, node } = boundOf(out, key, W, H);
    const S = Math.min(W, H);
    const min = MIN_CELL * S;
    let target = b.pos + delta;
    if (o.snap) {
      const tol = o.tolerance === undefined ? S * 0.02 : o.tolerance;
      let best = null;
      snapTargets(L, W, H, key).forEach((p) => {
        if (Math.abs(p - target) <= tol && (best === null || Math.abs(p - target) < Math.abs(best - target))) best = p;
      });
      if (best !== null) target = best;
    }
    const lo = -Math.max(b.sizes[0] - min, 0);
    const hi = Math.max(b.sizes[1] - min, 0);
    const d = clamp(target - b.pos, lo, hi);
    const total = b.sizes[0] + b.sizes[1];
    const pair = node.ratios[b.index] + node.ratios[b.index + 1];
    if (total > 0) {
      node.ratios[b.index] = pair * (b.sizes[0] + d) / total;
      node.ratios[b.index + 1] = pair * (b.sizes[1] - d) / total;
    }
    normalizeRatios(node);
    return out;
  }
  // 境界の一端（end = 0: 左／上、1: 右／下）だけを delta（画素）動かして斜めにする
  function setTilt(L, key, end, delta, W, H) {
    if (end !== 0 && end !== 1) fail('端は 0 か 1 で指定してください');
    const out = clone(L);
    const { b, node } = boundOf(out, key, W, H);
    const S = Math.min(W, H);
    const min = MIN_CELL * S;
    const tilt = tiltOf(node, b.index);
    const value = clamp(tilt[end] * S + delta, -Math.max(b.sizes[0] - min, 0), Math.max(b.sizes[1] - min, 0)) / S;
    tilt[end] = Math.abs(value) < 1e-9 ? 0 : value;
    node.tilt = node.children.slice(0, -1).map((_c, i) => (i === b.index ? tilt : (node.tilt && node.tilt[i]) || null));
    if (node.tilt.every((t) => !t || (!t[0] && !t[1]))) delete node.tilt;
    return out;
  }
  function resetTilt(L, key, W, H) {
    const out = clone(L);
    const { b, node } = boundOf(out, key, W || 1, H || 1);
    if (node.tilt) {
      node.tilt[b.index] = null;
      if (node.tilt.every((t) => !t || (!t[0] && !t[1]))) delete node.tilt;
    }
    return out;
  }

  // ---- 分割・結合・削除 ----

  function tiltInsert(node, index) {
    if (node.tilt) node.tilt.splice(index, 0, null);
  }
  function tiltRemove(node, index) {
    if (!node.tilt) return;
    node.tilt.splice(index, 1);
    if (node.tilt.every((t) => !t || (!t[0] && !t[1]))) delete node.tilt;
  }
  // 子が 1 つになった節をたたむ
  function collapse(L, node) {
    if (!node || isLeaf(node) || node.children.length !== 1) return;
    const hit = find(L, node.id);
    const child = node.children[0];
    if (hit.parent) hit.parent.children[hit.index] = child; else L.root = child;
  }
  // 節の子の大きさ（画素）
  function childSizes(L, node, W, H) {
    const C = compute(L, W, H);
    const size = (child) => {
      let l = Infinity, t = Infinity, r = -Infinity, b = -Infinity;
      walk(child, (n) => {
        if (!isLeaf(n)) return;
        const g = C.cells[n.id].rect;
        l = Math.min(l, g.l); t = Math.min(t, g.t); r = Math.max(r, g.r); b = Math.max(b, g.b);
      });
      return node.dir === 'v' ? b - t : r - l;
    };
    return node.children.map(size);
  }

  // コマを割る。dir = 'h'（縦の線で左右に割る）| 'v'（横の線で上下に割る）、t = 割る位置（0〜1。既定は等分）
  function split(L, id, dir, t, W, H) {
    if (dir !== 'h' && dir !== 'v') fail("割る向きは 'h'（左右）か 'v'（上下）です");
    const out = clone(L);
    const hit = find(out, id);
    if (!hit || !isLeaf(hit.node)) fail('そのコマはレイアウトにありません');
    const S = Math.min(W, H);
    const g = (dir === 'v' ? out.gutter.v : out.gutter.h) * S;
    const rect = compute(out, W, H).cells[id].rect;
    const extent = dir === 'v' ? rect.b - rect.t : rect.r - rect.l;
    const min = MIN_CELL * S;
    if (extent - g < min * 2) fail('コマが小さすぎて割れません');
    const at = clamp((t === undefined || t === null ? 0.5 : t) * extent, min + g / 2, extent - min - g / 2);
    const sizes = [at - g / 2, extent - at - g / 2];
    const leaf = hit.node;
    delete leaf.free;
    delete leaf.pts;
    const fresh = newLeaf(out);
    if (leaf.bleed) fresh.bleed = Object.assign({}, leaf.bleed);
    if (hit.parent && hit.parent.dir === dir) {
      // 同じ向きに並んでいる節の中なら、兄弟として足す（ほかの兄弟の大きさは変えない）
      const parent = hit.parent;
      const px = childSizes(out, parent, W, H);
      px.splice(hit.index, 1, sizes[0], sizes[1]);
      parent.children.splice(hit.index + 1, 0, fresh);
      parent.ratios = px;
      tiltInsert(parent, hit.index);
      normalizeRatios(parent);
    } else {
      const node = { id: newId(out, 'n'), dir, ratios: sizes, children: [leaf, fresh] };
      normalizeRatios(node);
      if (hit.parent) hit.parent.children[hit.index] = node; else out.root = node;
    }
    return { layout: out, first: leaf.id, second: fresh.id };
  }

  // 隣り合う 2 つのコマ（同じ行・列に並ぶ兄弟）を 1 つにする。残るのは左／上の側
  function merge(L, a, b, W, H) {
    const out = clone(L);
    const ha = find(out, a);
    const hb = find(out, b);
    if (!ha || !hb || !isLeaf(ha.node) || !isLeaf(hb.node)) fail('そのコマはレイアウトにありません');
    if (a === b || !ha.parent || ha.parent !== hb.parent || Math.abs(ha.index - hb.index) !== 1) {
      fail('結合できるのは、同じ行（または列）で隣り合う 2 つのコマです');
    }
    const parent = ha.parent;
    const i = Math.min(ha.index, hb.index);
    const px = childSizes(out, parent, W, H);
    const g = (parent.dir === 'v' ? out.gutter.v : out.gutter.h) * Math.min(W, H);
    px.splice(i, 2, px[i] + px[i + 1] + g);
    const kept = parent.children[i];
    const removed = parent.children[i + 1];
    parent.children.splice(i + 1, 1);
    parent.ratios = px;
    tiltRemove(parent, i);
    normalizeRatios(parent);
    delete kept.free;
    delete kept.pts;
    if (removed.bleed) kept.bleed = Object.assign({}, removed.bleed, kept.bleed);
    collapse(out, parent);
    return { layout: out, kept: kept.id, removed: removed.id, removedGuid: removed.guid || null };
  }

  // コマを消す。兄弟が比率に応じて埋める
  function remove(L, id) {
    const out = clone(L);
    const hit = find(out, id);
    if (!hit || !isLeaf(hit.node)) fail('そのコマはレイアウトにありません');
    if (!hit.parent) { out.root = null; return out; }
    const parent = hit.parent;
    parent.children.splice(hit.index, 1);
    parent.ratios.splice(hit.index, 1);
    tiltRemove(parent, Math.min(hit.index, parent.children.length - 1));
    normalizeRatios(parent);
    collapse(out, parent);
    return out;
  }

  // ---- 検査（外から渡されたレイアウトを確かめて整える） ----

  function check(input) {
    if (!input || typeof input !== 'object' || Array.isArray(input)) fail('レイアウトは JSON オブジェクトで指定してください');
    let L = create({ rtl: input.rtl !== false });
    L = setMargin(setGutter(L, input.gutter || {}), input.margin || {});
    const ids = new Set();
    let max = 0;
    const idOf = (node, prefix) => {
      let id = typeof node.id === 'string' && /^[a-z]\d{1,6}$/.test(node.id) && !ids.has(node.id) ? node.id : null;
      if (id) max = Math.max(max, parseInt(id.slice(1), 10));
      return id || null;
    };
    const pending = [];
    const build = (node, depth) => {
      if (!node || typeof node !== 'object' || Array.isArray(node)) fail('木の節は JSON オブジェクトで指定してください');
      if (depth > 8) fail('木が深すぎます');
      if (!node.children) {
        const leaf = { id: idOf(node, 'c'), guid: typeof node.guid === 'string' && node.guid ? node.guid : null };
        if (leaf.id) ids.add(leaf.id); else pending.push([leaf, 'c']);
        if (node.bleed && typeof node.bleed === 'object') {
          const bleed = {};
          SIDES.forEach((s) => { if (node.bleed[s] === true) bleed[s] = true; });
          if (Object.keys(bleed).length) leaf.bleed = bleed;
        }
        if (node.free === true && Array.isArray(node.pts) && node.pts.length >= 3
          && node.pts.every((p) => Array.isArray(p) && isFinite(p[0]) && isFinite(p[1]))) {
          leaf.free = true;
          leaf.pts = node.pts.map((p) => [Number(p[0]), Number(p[1])]);
        }
        return leaf;
      }
      if (node.dir !== 'v' && node.dir !== 'h') fail("節の dir は 'v'（行）か 'h'（列）です");
      if (!Array.isArray(node.children) || node.children.length < 1 || node.children.length > LIMITS.cols) {
        fail(`節の子は 1〜${LIMITS.cols} 個です`);
      }
      const n = node.children.length;
      let ratios = Array.isArray(node.ratios) ? node.ratios.slice() : node.children.map(() => 1);
      if (ratios.length !== n || !ratios.every((r) => typeof r === 'number' && isFinite(r) && r > 0)) {
        fail('ratios は子と同じ数の正の数で指定してください');
      }
      const made = { id: idOf(node, 'n'), dir: node.dir, ratios, children: [] };
      if (made.id) ids.add(made.id); else pending.push([made, 'n']);
      made.children = node.children.map((c) => build(c, depth + 1));
      normalizeRatios(made);
      if (Array.isArray(node.tilt)) {
        const tilt = [];
        for (let i = 0; i < n - 1; i++) {
          const t = node.tilt[i];
          const ok = Array.isArray(t) && t.length === 2 && t.every((x) => typeof x === 'number' && isFinite(x) && Math.abs(x) <= 1);
          tilt.push(ok && (t[0] || t[1]) ? [t[0], t[1]] : null);
        }
        if (tilt.some(Boolean)) made.tilt = tilt;
      }
      return made;
    };
    L.root = input.root == null ? null : build(input.root, 0);
    L.seq = max;
    pending.forEach(([node, prefix]) => { node.id = newId(L, prefix); });
    if (leaves(L).length > LIMITS.rows * LIMITS.cols) fail('コマが多すぎます');
    // 子が 1 つの節はたたむ
    let again = true;
    while (again) {
      again = false;
      walk(L.root, (node) => { if (!again && !isLeaf(node) && node.children.length === 1) { collapse(L, node); again = true; } });
    }
    const guids = leaves(L).map((x) => x.guid).filter(Boolean);
    if (new Set(guids).size !== guids.length) fail('同じコマ（guid）が 2 回出てきます');
    return L;
  }

  // ---- 取り込み（既にあるコマの外接矩形から、行と列の木を推定する） ----
  // rects = [{ l, t, r, b, guid }…]（画素）。行・列に分けられない配置なら null
  function infer(rects, W, H, opts) {
    const S = Math.min(W, H);
    const eps = S * 0.004;
    if (!rects.length) return null;
    const L = create(opts);
    const gaps = { v: [], h: [] };
    const groupBy = (items, lo, hi) => {
      const sorted = items.slice().sort((a, b) => a[lo] - b[lo]);
      const groups = [];
      let end = -Infinity;
      sorted.forEach((item) => {
        if (!groups.length || item[lo] >= end - eps) groups.push([item]);
        else groups[groups.length - 1].push(item);
        end = Math.max(end, item[hi]);
      });
      return groups;
    };
    const extent = (items) => ({ l: Math.min.apply(null, items.map((i) => i.l)), t: Math.min.apply(null, items.map((i) => i.t)),
      r: Math.max.apply(null, items.map((i) => i.r)), b: Math.max.apply(null, items.map((i) => i.b)) });
    const build = (items) => {
      if (items.length === 1) return newLeaf(L, items[0].guid);
      for (const dir of ['v', 'h']) {
        const lo = dir === 'v' ? 't' : 'l';
        const hi = dir === 'v' ? 'b' : 'r';
        const groups = groupBy(items, lo, hi);
        if (groups.length < 2) continue;
        const boxes = groups.map(extent);
        for (let i = 0; i < boxes.length - 1; i++) gaps[dir].push(Math.max(boxes[i + 1][lo] - boxes[i][hi], 0));
        const children = groups.map(build);
        if (children.some((c) => !c)) return null;
        const node = { id: newId(L, 'n'), dir, ratios: boxes.map((b) => Math.max(b[hi] - b[lo], 1e-6)), children };
        normalizeRatios(node);
        return node;
      }
      return null;
    };
    const rootNode = build(rects);
    if (!rootNode) return null;
    L.root = rootNode;
    const all = extent(rects);
    const median = (list, fallback) => {
      if (!list.length) return fallback;
      const s = list.slice().sort((a, b) => a - b);
      return s[Math.floor(s.length / 2)] / S;
    };
    L.gutter = { v: clamp(median(gaps.v, DEFAULTS.gutter.v), LIMITS.gutter[0], LIMITS.gutter[1]),
      h: clamp(median(gaps.h, DEFAULTS.gutter.h), LIMITS.gutter[0], LIMITS.gutter[1]) };
    L.margin = { top: clamp(all.t / S, 0, LIMITS.margin[1]), left: clamp(all.l / S, 0, LIMITS.margin[1]),
      right: clamp((W - all.r) / S, 0, LIMITS.margin[1]), bottom: clamp((H - all.b) / S, 0, LIMITS.margin[1]) };
    return L;
  }

  // よく使う形（行ごとのコマ数）
  const PRESETS = [
    { name: '4コマ縦', counts: [1, 1, 1, 1] }, { name: '2×2', counts: [2, 2] }, { name: '3段(2,1,2)', counts: [2, 1, 2] },
    { name: '3段(1,2,1)', counts: [1, 2, 1] }, { name: '3段(2,2,2)', counts: [2, 2, 2] }, { name: '3段(1,2,2)', counts: [1, 2, 2] },
    { name: '4段(2,1,2,1)', counts: [2, 1, 2, 1] }, { name: '3段(3,2,1)', counts: [3, 2, 1] },
  ];

  const api = { MIN_CELL, LIMITS, DEFAULTS, SIDES, PRESETS, create, grid, parseCounts, compute, leaves, order, find, bbox, area,
    contains, setGutter, setMargin, setBleed, setFree, clearFree, assign, snapTargets, moveBoundary, setTilt, resetTilt,
    split, merge, remove, check, infer, clone };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.inkdeusLayoutCore = api;
})(typeof window !== 'undefined' ? window : globalThis);
