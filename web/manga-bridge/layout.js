// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 andam0202
//
// inkdeus の連携スクリプトの部品: コマ割りツール。
// ページを「行と列に分かれた長方形の集まり」として持ち（計算は layout-core.js）、
// 漫画エディタのコマ（isPanel の fabric.Polygon）の頂点をそこから決める。
//   - レイアウトはキャンバスの独自プロパティ ikLayout として漫画データに入る（取り消し・ページ切替・保存で往復する）
//   - コマには ikCell（葉の id）の目印が付く
//   - 境界線などの目印は、キャンバスに重ねた別の描画面に描く（漫画データ・書き出し・レイヤー一覧には出ない）
// 漫画エディタ側のコードは書き換えず、公開されているグローバル関数を呼ぶ（必要な物は下の NEEDED）。
(function () {
  'use strict';
  const bridge = window.inkdeusBridge;
  const core = window.inkdeusLayoutCore;
  if (!bridge || !bridge.x || !core || bridge.layout) return;
  /* global fabric, canvas, ModeManager, commonProperties, withoutHistory, commitHistory, flushHistory, saveInitialState,
     updateClipPath, moveSettings, setGUID, getGUID, releasePanelLink, getAbsolutePolygonPoints, setText2ImageInitPrompt,
     setPanelValue, updateLayerPanel, reserveUniqueObjectName, collectUsedObjectNames, changeDoNotSaveHistory,
     changeDoSaveHistory, showCanvasHelpText, showConfirmDialog, getCanvasGUID,
     canvasContinerScale */
  const { ui, store, el, say, run, optional, commands, waitIdle } = bridge.x;

  const NEEDED = ['fabric', 'canvas', 'ModeManager', 'commonProperties', 'withoutHistory', 'commitHistory', 'flushHistory',
    'saveInitialState', 'updateClipPath', 'moveSettings', 'setGUID', 'getGUID', 'releasePanelLink',
    'getAbsolutePolygonPoints', 'setText2ImageInitPrompt', 'setPanelValue', 'updateLayerPanel', 'reserveUniqueObjectName',
    'collectUsedObjectNames', 'changeDoNotSaveHistory', 'changeDoSaveHistory', 'showCanvasHelpText'];
  const globalEval = eval;                   // 間接の eval（グローバルの名前を引くため）
  const missing = NEEDED.filter((name) => { try { return globalEval(name) == null; } catch (e) { return true; } });

  const LAYOUT_KEY = 'ikLayout';             // キャンバスに持つレイアウト
  const CELL_KEY = 'ikCell';                 // コマに持つ葉の id
  const MODE = 'ikLayout';                   // ModeManager に入れるモード名
  const FOLLOW = { keep: '相対位置を保って動かす', none: '動かさない', cover: '覆うように入れ直す' };
  const HELP = 'コマ割りモード: 境界線をドラッグで両側のコマを伸縮（Shift でスナップ）／境界の端の丸をドラッグで斜めに'
    + '（ダブルクリックでまっすぐ）／コマをクリックで選択（Shift で 2 つ目）／Alt+クリックで左右に、Alt+Shift+クリックで上下に割る／ESC で終了';

  const st = { mode: false, sel: [], hover: null, drag: null, arm: null, busy: 0,
    follow: FOLLOW[store.get('layoutFollow', 'keep')] ? store.get('layoutFollow', 'keep') : 'keep' };
  const view = {};

  // ---- 土台 ----

  function size() { return { W: canvas.getWidth(), H: canvas.getHeight() }; }
  function pageGuid() { return optional(() => getCanvasGUID()) || null; }
  // いまのページのレイアウト。コマが 1 つも残っていない（別のページの物が残っている・全部消された）なら、無い物として扱う
  function current() {
    const L = canvas[LAYOUT_KEY];
    if (!L || typeof L !== 'object' || L.v !== 1 || !L.root) return null;
    if (!core.leaves(L).some((leaf) => panelByGuid(leaf.guid))) {
      if (!st.busy) delete canvas[LAYOUT_KEY];
      return null;
    }
    if (L.page !== pageGuid()) canvas[LAYOUT_KEY] = Object.assign({}, L, { page: pageGuid() });   // 複製されたページ
    return canvas[LAYOUT_KEY];
  }
  function need() {
    const L = current();
    if (!L) throw new Error('このページにはコマ割りのレイアウトがありません（「格子を作る」か「いまのコマを取り込む」から始めてください）');
    return L;
  }
  function setLayout(L) {
    if (L && L.root) canvas[LAYOUT_KEY] = Object.assign({}, L, { page: pageGuid() }); else delete canvas[LAYOUT_KEY];
  }
  function allPanels() { return canvas.getObjects().filter((o) => o && o.isPanel); }
  function panelByGuid(guid) { return guid ? allPanels().find((p) => p.guid === guid) || null : null; }
  function absPoints(panel) {
    if (panel.points && panel.points.length >= 3) return getAbsolutePolygonPoints(panel).map((p) => [p.x, p.y]);
    const r = panel.getBoundingRect(true);
    return [[r.left, r.top], [r.left + r.width, r.top], [r.left + r.width, r.top + r.height], [r.left, r.top + r.height]];
  }
  // コマに入っている画像など（コマの形で切り抜かれている物）
  function childrenOf(panel) {
    const guids = panel.guids || [];
    return canvas.getObjects().filter((o) => o !== panel && !o.isPanel
      && (o.relatedPoly === panel || (o.guid && guids.indexOf(o.guid) !== -1)));
  }
  function samePoints(a, b) {
    return a.length === b.length && a.every((p, i) => Math.abs(p[0] - b[i][0]) < 0.01 && Math.abs(p[1] - b[i][1]) < 0.01);
  }

  // コマの頂点を絶対座標で置き換える（拡大率・角度は素直な値へ戻し、画面に合わせ直すときの基準も更新する）
  function setPanelPoints(panel, pts) {
    panel.set({ scaleX: 1, scaleY: 1, angle: 0, skewX: 0, skewY: 0, flipX: false, flipY: false });
    panel.points = pts.map((p) => ({ x: p[0], y: p[1] }));
    panel._setPositionDimensions({});
    panel.dirty = true;
    panel.setCoords();
    saveInitialState(panel);
  }

  function attach(child, panel) {
    if (child.relatedPoly === panel) return;
    if (child.relatedPoly || child.clipPath) releasePanelLink(child);
    moveSettings(child, panel);
    setGUID(panel, child);
  }
  function reclip(child, panel) {
    updateClipPath(child, panel);
    child.dirty = true;
    child.setCoords();
    saveInitialState(child);
  }
  function centerOf(obj) { const c = obj.getCenterPoint(); return [c.x, c.y]; }
  function moveCenter(obj, x, y) { obj.setPositionByOrigin(new fabric.Point(x, y), 'center', 'center'); }

  // コマの形が変わったときの、中の画像の扱い（before・after はコマの外接矩形、base は変わる前の画像の状態）
  function follow(child, base, before, after, mode) {
    child.set({ scaleX: base.sx, scaleY: base.sy });
    moveCenter(child, base.cx, base.cy);
    const bw = Math.max(before.r - before.l, 1e-6);
    const bh = Math.max(before.b - before.t, 1e-6);
    const aw = after.r - after.l;
    const ah = after.b - after.t;
    if (mode === 'keep') {
      // 中心の相対位置と、コマに対する大きさ（面積の比の平方根 = 縦横の倍率の相乗平均。等倍率で、行って戻れば元の大きさになる）を保つ
      const k = Math.sqrt((aw / bw) * (ah / bh));
      child.set({ scaleX: base.sx * k, scaleY: base.sy * k });
      moveCenter(child, after.l + (base.cx - before.l) / bw * aw, after.t + (base.cy - before.t) / bh * ah);
    } else if (mode === 'cover') {
      cover(child, after);
    }
  }
  function cover(child, box) {
    const scale = Math.max((box.r - box.l) / child.width, (box.b - box.t) / child.height);
    child.set({ scaleX: scale, scaleY: scale });
    moveCenter(child, (box.l + box.r) / 2, (box.t + box.b) / 2);
  }
  function childState(child) {
    const c = centerOf(child);
    return { obj: child, sx: child.scaleX, sy: child.scaleY, cx: c[0], cy: c[1] };
  }
  // いまのコマと中身の状態（ドラッグの間、ここを基準に計算し直す）
  function snapshot(L) {
    const out = {};
    core.leaves(L).forEach((leaf) => {
      const panel = panelByGuid(leaf.guid);
      if (panel) out[leaf.id] = { box: core.bbox(absPoints(panel)), kids: childrenOf(panel).map(childState) };
    });
    return out;
  }

  // レイアウトどおりにコマの頂点を置く。mode = 中の画像の扱い、base = snapshot()（省略時はいまの状態）
  function applyGeometry(L, mode, base) {
    const { W, H } = size();
    const C = core.compute(L, W, H);
    core.leaves(L).forEach((leaf) => {
      const panel = panelByGuid(leaf.guid);
      if (!panel) return;
      panel[CELL_KEY] = leaf.id;
      const target = C.cells[leaf.id].pts;
      const now = absPoints(panel);
      const from = base && base[leaf.id];
      if (!from && samePoints(now, target)) return;
      const before = from ? from.box : core.bbox(now);
      const kids = from ? from.kids : childrenOf(panel).map(childState);
      if (!samePoints(now, target)) setPanelPoints(panel, target);
      const after = core.bbox(target);
      kids.forEach((k) => {
        if (canvas.getObjects().indexOf(k.obj) === -1) return;
        follow(k.obj, k, before, after, mode);
        reclip(k.obj, panel);
      });
    });
    return C;
  }

  // 読む順に並べたコマ（レイアウトのコマ → それ以外を位置の順に）
  function orderedPanels() {
    const L = current();
    const managed = L ? core.order(L).map((id) => panelByGuid(core.find(L, id).node.guid)).filter(Boolean) : [];
    const rest = allPanels().filter((p) => managed.indexOf(p) === -1).map((p) => ({ p, box: core.bbox(absPoints(p)) }));
    const rowTol = size().H * 0.05;
    const rtl = !L || L.rtl !== false;
    rest.sort((a, b) => (Math.abs(a.box.t - b.box.t) > rowTol ? a.box.t - b.box.t : (rtl ? b.box.l - a.box.l : a.box.l - b.box.l)));
    return managed.concat(rest.map((x) => x.p));
  }

  // 新しいコマを作る（漫画エディタがコマを足すときと同じ手順）
  function createPanel(pts, index, names) {
    const panel = new fabric.Polygon(pts.map((p) => ({ x: p[0], y: p[1] })), {
      scaleX: 1, scaleY: 1, stroke: 'black', strokeWidth: 2 * (canvas.width / 700), strokeUniform: true,
      objectCaching: false, isPanel: true, selectable: false });
    setText2ImageInitPrompt(panel);
    panel.name = reserveUniqueObjectName('polygon', names);
    getGUID(panel);
    if (index === undefined || index === null || index >= canvas.getObjects().length) canvas.add(panel);
    else canvas.insertAt(panel, Math.max(index, 0), false);
    setPanelValue(panel);                     // 線の太さ・色・塗りは、漫画エディタのコマの設定に合わせる
    setPanelPoints(panel, pts);               // 線の太さが変わると位置がずれるので、置き直す
    return panel;
  }

  // 一連の書き換えを、取り消し 1 回で戻る 1 つの変更として行う。fn は { layout, … } を返す
  function transact(fn) {
    let out;
    st.busy += 1;
    try {
      withoutHistory(() => { out = fn(); });
      if (out && 'layout' in out) setLayout(out.layout);
    } finally {
      st.busy -= 1;
    }
    if (st.mode) relock();
    st.sel = st.sel.filter((id) => current() && core.find(current(), id));
    canvas.requestRenderAll();
    optional(() => updateLayerPanel());
    commitHistory();
    flushHistory();
    render();
    return out;
  }

  // 葉を指す値（葉の id・コマの guid・読む順の番号）→ 葉の id
  function cellId(ref) {
    const L = need();
    if (typeof ref === 'number') {
      const id = core.order(L)[ref - 1];
      if (!id) throw new Error(`${ref} 番のコマはありません`);
      return id;
    }
    const hit = core.leaves(L).find((leaf) => leaf.id === ref || leaf.guid === ref);
    if (!hit) throw new Error('そのコマはレイアウトにありません');
    return hit.id;
  }
  function leafOf(L, id) { return core.find(L, id).node; }

  // ---- 操作 ----

  async function confirmReplace(count) {
    const message = `このページのコマ ${count} 個を新しいコマ割りに置き換えます。コマに入っている画像は、読む順に新しいコマへ引き継ぎます。`;
    const dialog = optional(() => showConfirmDialog);
    if (dialog) return !!(await dialog({ title: 'コマ割りを置き換える', message, okText: '置き換える', cancelText: 'やめる', danger: true }));
    return window.confirm(message);
  }

  // レイアウトどおりにコマを作り直す。既にあるコマの画像は、読む順に新しいコマへ引き継ぐ
  async function rebuild(L, opts) {
    const o = opts || {};
    await waitIdle();
    const existing = allPanels();
    if (existing.length && !o.force && !(await confirmReplace(existing.length))) return null;
    transact(() => {
      const groups = orderedPanels().map((p) => childrenOf(p));
      const objects = canvas.getObjects();
      const baseIndex = existing.length ? Math.min.apply(null, existing.map((p) => objects.indexOf(p))) : 0;
      existing.forEach((p) => {
        childrenOf(p).forEach((child) => releasePanelLink(child));      // 先に紐づけを外してからコマを消す
        canvas.remove(p);
      });
      const { W, H } = size();
      const C = core.compute(L, W, H);
      const names = collectUsedObjectNames();
      const made = {};
      let layout = L;
      core.leaves(L).forEach((leaf, i) => {
        const panel = createPanel(C.cells[leaf.id].pts, baseIndex + i, names);
        panel[CELL_KEY] = leaf.id;
        made[leaf.id] = panel;
        layout = core.assign(layout, leaf.id, panel.guid);
      });
      const ids = core.order(L);
      groups.forEach((group, i) => {
        const id = ids[Math.min(i, ids.length - 1)];              // 余った画像は最後のコマへ
        group.forEach((child) => {
          attach(child, made[id]);
          cover(child, core.bbox(C.cells[id].pts));
          reclip(child, made[id]);
        });
      });
      return { layout };
    });
    st.sel = [];
    render();
    return info();
  }

  function makeGrid(counts, opts) {
    const o = opts || {};
    const old = current();
    return rebuild(core.grid(counts, { gutter: o.gutter || (old && old.gutter), margin: o.margin || (old && old.margin),
      rtl: o.rtl === undefined ? !old || old.rtl !== false : o.rtl }), o);
  }

  // 木と設定をまとめて置き換える。葉がいまのコマをちょうど指していれば形だけを変え、そうでなければ作り直す
  async function setAll(input, opts) {
    const L = core.check(input);
    if (!L.root) throw new Error('レイアウトにコマがありません');
    const old = current();
    const leaves = core.leaves(L);
    const keeps = old && leaves.every((leaf) => leaf.guid && panelByGuid(leaf.guid))
      && core.leaves(old).every((leaf) => leaves.some((x) => x.guid === leaf.guid));
    if (!keeps) return rebuild(L, opts);
    await waitIdle();
    transact(() => { applyGeometry(L, st.follow); return { layout: L }; });
    return info();
  }

  function change(next, mode) {
    transact(() => { applyGeometry(next, mode === undefined ? st.follow : mode); return { layout: next }; });
    return info();
  }
  function setGutter(v, h) {
    const gutter = {};
    if (v !== undefined && v !== null) gutter.v = v;
    if (h !== undefined && h !== null) gutter.h = h;
    return change(core.setGutter(need(), gutter));
  }
  function setMargin(margin) { return change(core.setMargin(need(), margin || {})); }
  function setBleed(ref, side, on) { return change(core.setBleed(need(), cellId(ref), side, on !== false)); }
  function setRtl(on) { const L = core.clone(need()); L.rtl = !!on; return change(L); }
  // 短辺に対する割合で境界を動かす（画面のドラッグと同じ計算）
  function moveBoundary(key, delta, opts) {
    const { W, H } = size();
    return change(core.moveBoundary(need(), key, delta * Math.min(W, H), W, H, opts));
  }
  function setTilt(key, end, delta) {
    const { W, H } = size();
    return change(core.setTilt(need(), key, end, delta * Math.min(W, H), W, H));
  }
  function resetTilt(key) { const { W, H } = size(); return change(core.resetTilt(need(), key, W, H)); }
  // 連動から外して、頂点（ページに対する割合の [x, y]）をそのまま使う
  function setFree(ref, pts) {
    const L = need();
    const id = cellId(ref);
    const { W, H } = size();
    const shape = pts || core.compute(L, W, H).cells[id].pts.map((p) => [p[0] / W, p[1] / H]);
    return change(core.setFree(L, id, shape));
  }
  function clearFree(ref) { return change(core.clearFree(need(), cellId(ref))); }

  // 割る。dir = 'h'（縦の線で左右に）| 'v'（横の線で上下に）。中の画像は、中心がある側に残る（位置と大きさはそのまま）
  function split(ref, dir, t) {
    const L = need();
    const id = cellId(ref);
    const { W, H } = size();
    const r = core.split(L, id, dir, t, W, H);
    const panel = panelByGuid(leafOf(L, id).guid);
    if (!panel) throw new Error('そのコマがページにありません');
    const out = transact(() => {
      const kids = childrenOf(panel);
      const fresh = createPanel(core.compute(r.layout, W, H).cells[r.second].pts,
        canvas.getObjects().indexOf(panel) + 1, collectUsedObjectNames());
      fresh[CELL_KEY] = r.second;
      const layout = core.assign(r.layout, r.second, fresh.guid);
      const C = applyGeometry(layout, 'none');
      kids.forEach((child) => {
        const c = centerOf(child);
        const target = core.contains(C.cells[r.second].pts, c[0], c[1]) ? fresh : panel;
        attach(child, target);
        reclip(child, target);
      });
      return { layout, first: r.first, second: r.second };
    });
    st.sel = [out.first];
    render();
    return Object.assign(info(), { first: out.first, second: out.second });
  }

  // 隣り合う 2 つを 1 つにする。中の画像は両方残る（切り抜きは新しいコマ）
  function merge(refA, refB) {
    const L = need();
    const { W, H } = size();
    const r = core.merge(L, cellId(refA), cellId(refB), W, H);
    const kept = panelByGuid(leafOf(r.layout, r.kept).guid);
    const gone = panelByGuid(r.removedGuid);
    transact(() => {
      const moved = gone ? childrenOf(gone) : [];
      moved.forEach((child) => releasePanelLink(child));
      if (gone) canvas.remove(gone);
      applyGeometry(r.layout, 'none');
      if (kept) moved.concat(childrenOf(kept)).forEach((child) => { attach(child, kept); reclip(child, kept); });
      return { layout: r.layout };
    });
    st.sel = [r.kept];
    render();
    return Object.assign(info(), { kept: r.kept });
  }

  // 消す。兄弟が比率に応じて埋める。images = 'keep'（既定。コマなしの自由な画像として残す）| 'delete'
  function remove(ref, opts) {
    const L = need();
    const id = cellId(ref);
    const panel = panelByGuid(leafOf(L, id).guid);
    const next = core.remove(L, id);
    transact(() => {
      if (panel) {
        childrenOf(panel).forEach((child) => {
          releasePanelLink(child);
          if (opts && opts.images === 'delete') canvas.remove(child);
          else saveInitialState(child);
        });
        canvas.remove(panel);
      }
      if (next.root) applyGeometry(next, st.follow);
      return { layout: next.root ? next : null };
    });
    return info();
  }

  // 既にあるコマ（ひな形やナイフで作った物）からレイアウトを推定して取り込む
  function importPanels() {
    const { W, H } = size();
    const tol = Math.min(W, H) * 0.015;
    const rects = [];
    let skipped = 0;
    allPanels().forEach((panel) => {
      const pts = panel.points && panel.points.length === 4 ? absPoints(panel) : null;
      const box = pts && core.bbox(pts);
      // 外接矩形がほぼ軸に平行な長方形（4 頂点がそれぞれ外接矩形の角の近くにある）だけを対象にする
      const corners = box && [[box.l, box.t], [box.r, box.t], [box.r, box.b], [box.l, box.b]];
      const ok = corners && corners.every((c) => pts.some((p) => Math.abs(p[0] - c[0]) <= tol && Math.abs(p[1] - c[1]) <= tol));
      if (ok) rects.push(Object.assign({ guid: getGUID(panel) }, box)); else skipped += 1;
    });
    if (!rects.length) throw new Error('取り込めるコマ（長方形のコマ）がありません');
    const old = current();
    const L = core.infer(rects, W, H, { rtl: !old || old.rtl !== false });
    if (!L) throw new Error('行と列に分けられない配置なので、取り込めません');
    transact(() => {
      allPanels().forEach((panel) => { delete panel[CELL_KEY]; });
      applyGeometry(L, st.follow);
      return { layout: L };
    });
    say(`コマ ${rects.length} 個を取り込みました` + (skipped ? `（長方形でないコマ ${skipped} 個は対象外）` : ''));
    return Object.assign(info(), { imported: rects.length, skipped });
  }

  // ---- 上流への差分（inkdeus manga install が当てる）と組になる機能。差分が当たっていなければ使えない ----

  const PATCH = { template: '0002-template-to-current-page', knife: '0003-knife-keeps-image-place' };
  function patched(name) { return (window.__inkdeusPatches || []).indexOf(name) !== -1; }

  // ひな形（SVG）の中のコマの形を、ページの画素の頂点にして返す
  function templateShapes(svg) {
    return new Promise((resolve, reject) => {
      fabric.loadSVGFromString(svg, (objects, options) => {
        const { W, H } = size();
        const sw = options && options.width;
        const sh = options && options.height;
        if (!objects || !objects.length || !sw || !sh) { reject(new Error('ひな形を読めません')); return; }
        resolve(objects.map((obj) => {
          let pts;
          if (obj.type === 'polygon' && obj.points && obj.points.length >= 3) {
            pts = getAbsolutePolygonPoints(obj).map((q) => [q.x, q.y]);
          } else {
            const r = obj.getBoundingRect(true, true);
            pts = [[r.left, r.top], [r.left + r.width, r.top], [r.left + r.width, r.top + r.height], [r.left, r.top + r.height]];
          }
          return pts.map((q) => [q[0] / sw * W, q[1] / sh * H]);
        }));
      });
    });
  }

  // ひな形を、新しいページを作らずに、いまのページのコマと置き換える（中の画像は読む順に引き継ぐ）。
  // 行と列に分けられる長方形のひな形はレイアウトとして取り込み、そうでない物は形のまま置く
  async function applyTemplate(svg, opts) {
    const o = opts || {};
    const shapes = await templateShapes(svg);
    const { W, H } = size();
    const tol = Math.min(W, H) * 0.015;
    const rects = shapes.map((pts, i) => {
      const box = core.bbox(pts);
      const corners = [[box.l, box.t], [box.r, box.t], [box.r, box.b], [box.l, box.b]];
      const ok = pts.length === 4 && corners.every((c) => pts.some((q) => Math.abs(q[0] - c[0]) <= tol && Math.abs(q[1] - c[1]) <= tol));
      return ok ? Object.assign({ guid: 't' + i }, box) : null;
    });
    const old = current();
    const inferred = rects.every(Boolean) ? core.infer(rects, W, H, { rtl: !old || old.rtl !== false }) : null;
    if (inferred) {
      let L = inferred;
      core.leaves(inferred).forEach((leaf) => { L = core.assign(L, leaf.id, null); });
      return rebuild(L, o);
    }
    await waitIdle();
    const existing = allPanels();
    if (existing.length && !o.force && !(await confirmReplace(existing.length))) return null;
    transact(() => {
      const groups = orderedPanels().map((p) => childrenOf(p));
      const objects = canvas.getObjects();
      const baseIndex = existing.length ? Math.min.apply(null, existing.map((p) => objects.indexOf(p))) : 0;
      existing.forEach((p) => { childrenOf(p).forEach((child) => releasePanelLink(child)); canvas.remove(p); });
      const names = collectUsedObjectNames();
      const made = shapes.map((pts, i) => createPanel(pts, baseIndex + i, names));
      // 読む順（上から、右から）に並べて、画像を引き継ぐ
      const sorted = made.map((p, i) => ({ p, box: core.bbox(shapes[i]) }))
        .sort((a, b) => (Math.abs(a.box.t - b.box.t) > H * 0.05 ? a.box.t - b.box.t : b.box.l - a.box.l));
      groups.forEach((group, i) => {
        const target = sorted[Math.min(i, sorted.length - 1)];
        group.forEach((child) => { attach(child, target.p); cover(child, target.box); reclip(child, target.p); });
      });
      return { layout: null };
    });
    return info();
  }

  function setTemplateTarget(target) {
    if (target !== 'new' && target !== 'current') throw new Error('new / current のどちらかです');
    st.templateTarget = target;
    store.set('templateTarget', target);
    if (view.template) view.template.value = target;
    return target;
  }
  // true = 上流の元の挙動（大きいほうのコマの中央に入れ直す）、false = 位置と大きさを保つ
  function setKnifeRefit(on) {
    st.knifeRefit = !!on;
    window.inkdeusKnifeRefit = st.knifeRefit;
    store.set('knifeRefit', st.knifeRefit);
    if (view.knife) view.knife.value = st.knifeRefit ? 'refit' : 'keep';
    return st.knifeRefit;
  }

  function clearLayout() {
    transact(() => { allPanels().forEach((panel) => { delete panel[CELL_KEY]; }); return { layout: null }; });
    return info();
  }

  function info() {
    const { W, H } = size();
    const L = current();
    const out = { page: { width: W, height: H, guid: pageGuid() }, layout: L ? core.clone(L) : null,
      cells: [], bounds: [], mode: st.mode, selected: st.sel.slice(), follow: st.follow,
      unmanaged: allPanels().filter((p) => !L || !core.leaves(L).some((leaf) => leaf.guid === p.guid)).length };
    if (!L) return out;
    const C = core.compute(L, W, H);
    core.order(L).forEach((id, i) => {
      const cell = C.cells[id];
      const panel = panelByGuid(cell.guid);
      const box = core.bbox(cell.pts);
      out.cells.push({ number: i + 1, id, guid: cell.guid, name: panel ? panel.name || '' : null, missing: !panel,
        rect: { x: box.l / W, y: box.t / H, w: (box.r - box.l) / W, h: (box.b - box.t) / H },
        points: cell.pts.map((p) => [p[0] / W, p[1] / H]), free: cell.free, edges: cell.edges });
    });
    out.bounds = Object.values(C.bounds).map((b) => ({ key: b.key, dir: b.dir, pos: b.pos / (b.dir === 'v' ? H : W),
      a: [b.a[0] / W, b.a[1] / H], b: [b.b[0] / W, b.b[1] / H], tilt: b.tilt.slice() }));
    return out;
  }

  // ---- ドラッグ・スライダーの間の軽い描き直し（離したときに 1 回だけ確定する） ----

  const live = { on: false, base: null, shot: null, next: null };
  function liveBegin() {
    if (live.on) return;
    live.base = need();
    live.on = true;
    live.shot = snapshot(live.base);
    live.next = null;
    st.busy += 1;
    changeDoNotSaveHistory();
  }
  function liveApply(next) {
    if (!live.on) return;
    live.next = next;
    applyGeometry(next, st.follow, live.shot);
    canvas.requestRenderAll();
  }
  function liveEnd(cancel) {
    if (!live.on) return;
    live.on = false;
    if (cancel && live.next) { applyGeometry(live.base, st.follow, live.shot); live.next = null; }
    changeDoSaveHistory();
    st.busy -= 1;
    if (live.next) {
      setLayout(live.next);
      canvas.requestRenderAll();
      optional(() => updateLayerPanel());
      commitHistory();
      flushHistory();
    }
    live.shot = null;
    render();
  }

  // ---- コマ割りモード（境界線のドラッグなど） ----

  function zoom() { return optional(() => canvasContinerScale) || 1; }
  function dist(p, a, b) {
    const dx = b[0] - a[0], dy = b[1] - a[1];
    const len = dx * dx + dy * dy;
    const t = len ? Math.min(Math.max(((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / len, 0), 1) : 0;
    return Math.hypot(p[0] - (a[0] + dx * t), p[1] - (a[1] + dy * t));
  }
  function handlePos(b, end) {
    const f = end ? 0.94 : 0.06;
    return [b.a[0] + (b.b[0] - b.a[0]) * f, b.a[1] + (b.b[1] - b.a[1]) * f];
  }

  // ポインタの下にある物: 選択中のコマの頂点 → 境界の端 → 境界線 →（連動から外したコマの）辺 → コマ
  function hit(p) {
    const L = current();
    if (!L) return null;
    const { W, H } = size();
    const C = core.compute(L, W, H);
    const tol = 8 / zoom();
    const one = st.sel.length === 1 && C.cells[st.sel[0]] ? C.cells[st.sel[0]] : null;
    if (one) {
      for (let i = 0; i < one.pts.length; i++) {
        if (Math.hypot(p[0] - one.pts[i][0], p[1] - one.pts[i][1]) <= tol) return { type: 'vertex', id: one.id, index: i };
      }
    }
    const bounds = Object.values(C.bounds);
    for (const b of bounds) {
      for (const end of [0, 1]) {
        const h = handlePos(b, end);
        if (Math.hypot(p[0] - h[0], p[1] - h[1]) <= tol) return { type: 'tilt', key: b.key, end, dir: b.dir };
      }
    }
    if (one && one.free) {
      for (let i = 0; i < one.pts.length; i++) {
        if (dist(p, one.pts[i], one.pts[(i + 1) % one.pts.length]) <= tol * 0.75) return { type: 'edge', id: one.id, index: i };
      }
    }
    let best = null;
    bounds.forEach((b) => {
      const d = dist(p, b.a, b.b);
      if (d <= Math.max(tol * 0.75, b.gutter / 2) && (!best || d < best.d)) best = { type: 'bound', key: b.key, dir: b.dir, d };
    });
    if (best) return best;
    const cell = Object.values(C.cells).find((c) => core.contains(c.pts, p[0], p[1]));
    return cell ? { type: 'cell', id: cell.id, rect: cell.rect } : null;
  }

  function pointer(opt) { const p = canvas.getPointer(opt.e); return [p.x, p.y]; }
  function cursorFor(h) {
    if (!h) return 'default';
    if (h.type === 'bound') return h.dir === 'v' ? 'row-resize' : 'col-resize';
    if (h.type === 'tilt') return h.dir === 'v' ? 'ns-resize' : 'ew-resize';
    if (h.type === 'vertex' || h.type === 'edge') return 'move';
    return st.arm ? 'crosshair' : 'pointer';
  }
  function setCursor(name) {
    canvas.defaultCursor = name;
    canvas.setCursor(name);
  }

  function splitAt(h, p, dir) {
    const span = dir === 'h' ? h.rect.r - h.rect.l : h.rect.b - h.rect.t;
    const t = span > 0 ? (dir === 'h' ? p[0] - h.rect.l : p[1] - h.rect.t) / span : 0.5;
    return split(h.id, dir, t);
  }

  function onDown(opt) {
    if (!st.mode || (opt.e && opt.e.button)) return;
    const p = pointer(opt);
    st.drag = { start: p, hit: hit(p), moved: false };
  }
  function onMove(opt) {
    if (!st.mode) return;
    const p = pointer(opt);
    const d = st.drag;
    if (!d) {
      const h = hit(p);
      const key = h ? [h.type, h.key || h.id, h.end, h.index].join('|') : '';
      if (key !== (st.hover && st.hover.sig)) { st.hover = h ? Object.assign({ sig: key }, h) : null; draw(); }
      setCursor(cursorFor(h));
      return;
    }
    const h = d.hit;
    if (!h || h.type === 'cell') return;
    const dx = p[0] - d.start[0];
    const dy = p[1] - d.start[1];
    if (!d.moved && Math.hypot(dx, dy) < 3 / zoom()) return;
    const { W, H } = size();
    run(() => {
      if (!d.moved) { d.moved = true; liveBegin(); }
      const base = live.base;
      if (h.type === 'bound') {
        liveApply(core.moveBoundary(base, h.key, h.dir === 'v' ? dy : dx, W, H, { snap: !!(opt.e && opt.e.shiftKey) }));
      } else if (h.type === 'tilt') {
        liveApply(core.setTilt(base, h.key, h.end, h.dir === 'v' ? dy : dx, W, H));
      } else {
        // 頂点・辺のドラッグ: そのコマを連動から外して、形を直接変える
        const pts = core.compute(base, W, H).cells[h.id].pts.map((q) => [q[0], q[1]]);
        const moving = h.type === 'vertex' ? [h.index] : [h.index, (h.index + 1) % pts.length];
        moving.forEach((i) => { pts[i] = [Math.min(Math.max(pts[i][0] + dx, 0), W), Math.min(Math.max(pts[i][1] + dy, 0), H)]; });
        liveApply(core.setFree(base, h.id, pts.map((q) => [q[0] / W, q[1] / H])));
      }
      draw();
    });
  }
  function onUp(opt) {
    if (!st.mode || !st.drag) return;
    const d = st.drag;
    st.drag = null;
    if (d.moved) { liveEnd(false); return; }
    const h = d.hit;
    const e = opt.e || {};
    run(() => {
      if (h && h.type === 'cell') {
        if (st.arm) { const dir = st.arm; st.arm = null; splitAt(h, d.start, dir); return; }
        if (e.altKey) { splitAt(h, d.start, e.shiftKey ? 'v' : 'h'); return; }
        if (e.shiftKey && st.sel.length === 1 && st.sel[0] !== h.id) st.sel = [st.sel[0], h.id];
        else st.sel = [h.id];
      } else if (!h) {
        st.sel = [];
        st.arm = null;
      }
      render();
    });
  }
  function onDblClick(opt) {
    if (!st.mode) return;
    const h = hit(pointer(opt));
    if (h && (h.type === 'tilt' || h.type === 'bound')) run(() => resetTilt(h.key));
  }

  // モードの間は、キャンバス上の物を掴めないようにする（保存される selectable には触らない）
  function relock() { ModeManager.lock.apply({ evented: false }); }

  function enter() {
    if (st.mode) return true;
    ModeManager.clearAll();                    // ほかのモードを抜ける
    canvas.discardActiveObject();
    ModeManager._current = MODE;
    relock();
    canvas.selection = false;
    ModeManager.button.activeClear();
    showCanvasHelpText(HELP, 'ESC');
    st.mode = true;
    st.sel = [];
    st.hover = null;
    canvas.on('mouse:down', onDown);
    canvas.on('mouse:move', onMove);
    canvas.on('mouse:up', onUp);
    canvas.on('mouse:dblclick', onDblClick);
    canvas.requestRenderAll();
    render();
    return true;
  }
  // 後始末（ロックの解除・案内文・ボタンの消灯は、このあと ModeManager.clearAll が行う）
  function leave() {
    if (!st.mode) return;
    if (live.on) liveEnd(true);
    st.mode = false;
    st.drag = null;
    st.hover = null;
    st.arm = null;
    st.sel = [];
    canvas.off('mouse:down', onDown);
    canvas.off('mouse:move', onMove);
    canvas.off('mouse:up', onUp);
    canvas.off('mouse:dblclick', onDblClick);
    render();
  }
  function exit() {
    if (st.mode) ModeManager.clearAll();
    return false;
  }

  // ---- 目印の描画（キャンバスに重ねた別の描画面） ----

  function overlay() {
    const wrapper = canvas.wrapperEl;
    if (!wrapper) return null;
    if (!view.overlay || view.overlay.parentNode !== wrapper) {
      view.overlay = el('canvas', { class: 'ikl-overlay', 'aria-hidden': 'true' });
      wrapper.appendChild(view.overlay);
    }
    const { W, H } = size();
    if (view.overlay.width !== Math.round(W) || view.overlay.height !== Math.round(H)) {
      view.overlay.width = Math.round(W);
      view.overlay.height = Math.round(H);
    }
    return view.overlay;
  }
  function draw() {
    const node = overlay();
    if (!node) return;
    const ctx = node.getContext('2d');
    ctx.clearRect(0, 0, node.width, node.height);
    node.hidden = !st.mode;
    const L = current();
    if (!st.mode || !L) return;
    const { W, H } = size();
    const C = core.compute(L, W, H);
    const z = zoom();
    const accent = '#ff9532';
    const on = (h, type, key, end) => h && h.type === type && h.key === key && (end === undefined || h.end === end);
    const active = st.drag && st.drag.hit && st.drag.hit.type !== 'cell' ? st.drag.hit : st.hover;
    core.order(L).forEach((id, i) => {
      const cell = C.cells[id];
      const box = core.bbox(cell.pts);
      const selected = st.sel.indexOf(id) !== -1;
      ctx.beginPath();
      cell.pts.forEach((p, k) => (k ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1])));
      ctx.closePath();
      if (!panelByGuid(cell.guid)) {             // コマが消されて空いている場所
        ctx.setLineDash([3 / z, 4 / z]);
        ctx.lineWidth = 1 / z;
        ctx.strokeStyle = 'rgba(120,120,120,0.9)';
        ctx.stroke();
        ctx.setLineDash([]);
      }
      if (selected) { ctx.fillStyle = 'rgba(255,149,50,0.14)'; ctx.fill(); ctx.lineWidth = 2.5 / z; ctx.strokeStyle = accent; ctx.stroke(); }
      const r = 11 / z;
      ctx.beginPath();
      ctx.arc((box.l + box.r) / 2, (box.t + box.b) / 2, r, 0, Math.PI * 2);
      ctx.fillStyle = selected ? accent : 'rgba(31,33,38,0.72)';
      ctx.fill();
      ctx.fillStyle = '#fff';
      ctx.font = `${Math.round(12 / z)}px system-ui, sans-serif`;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(String(i + 1) + (cell.free ? '*' : ''), (box.l + box.r) / 2, (box.t + box.b) / 2);
      if (selected && st.sel.length === 1) {
        cell.pts.forEach((p) => {
          ctx.fillStyle = '#fff';
          ctx.strokeStyle = accent;
          ctx.lineWidth = 1.5 / z;
          ctx.fillRect(p[0] - 4 / z, p[1] - 4 / z, 8 / z, 8 / z);
          ctx.strokeRect(p[0] - 4 / z, p[1] - 4 / z, 8 / z, 8 / z);
        });
      }
    });
    Object.values(C.bounds).forEach((b) => {
      const hot = on(active, 'bound', b.key);
      ctx.beginPath();
      ctx.moveTo(b.a[0], b.a[1]);
      ctx.lineTo(b.b[0], b.b[1]);
      ctx.setLineDash(hot ? [] : [6 / z, 5 / z]);
      ctx.lineWidth = (hot ? 3.5 : 1.2) / z;
      ctx.strokeStyle = hot ? accent : 'rgba(255,149,50,0.75)';
      ctx.stroke();
      ctx.setLineDash([]);
      [0, 1].forEach((end) => {
        const h = handlePos(b, end);
        ctx.beginPath();
        ctx.arc(h[0], h[1], (on(active, 'tilt', b.key, end) ? 6.5 : 4.5) / z, 0, Math.PI * 2);
        ctx.fillStyle = on(active, 'tilt', b.key, end) ? accent : '#fff';
        ctx.fill();
        ctx.lineWidth = 1.5 / z;
        ctx.strokeStyle = accent;
        ctx.stroke();
      });
    });
  }

  // ---- 素材パネルの「コマ割り」の欄 ----

  function pct(value) { return Math.round(value * 1000) / 10; }
  function slider(id, label, max, title) {
    const input = el('input', { type: 'range', id, min: '0', max: String(max), step: '0.1', title });
    const out = el('span', { class: 'ikb-dim ikl-value' });
    return { row: el('label', { class: 'ikl-slider' }, [el('span', { text: label }), input, out]), input, out };
  }

  function buildPanel() {
    const v = view;
    v.mode = el('button', { type: 'button', id: 'ikl-mode', text: 'コマ割りモード', 'aria-pressed': 'false',
      title: '境界線をドラッグしてコマを伸縮する・割る・結合するモード（ESC で終了）',
      onclick: () => run(() => (st.mode ? exit() : enter())) });
    v.counts = el('input', { type: 'text', id: 'ikl-counts', value: '2,1,2', placeholder: '行ごとのコマ数（例 2,1,3）',
      'aria-label': '行ごとのコマ数', title: '上の行から順に、その行のコマの数をカンマで区切って書きます（例 2,1,3 = 3 行）' });
    v.grid = el('button', { type: 'button', id: 'ikl-grid', text: '格子を作る', onclick: () => run(() => makeGrid(v.counts.value)) });
    v.presets = el('div', { class: 'ikl-presets' }, core.PRESETS.map((p) => el('button', {
      type: 'button', class: 'ikl-preset', 'data-counts': p.counts.join(','), text: p.name,
      title: `行ごとのコマ数 ${p.counts.join(',')} の格子を、いまのページに作る`,
      onclick: () => run(() => { v.counts.value = p.counts.join(','); return makeGrid(p.counts); }) })));
    v.gv = slider('ikl-gutter-v', '間隔（縦）', 10, '行と行の間隔（ページの短辺に対する %）');
    v.gh = slider('ikl-gutter-h', '間隔（横）', 10, '列と列の間隔（ページの短辺に対する %）');
    v.mv = slider('ikl-margin-v', '余白（上下）', 20, '外周の余白の上と下（ページの短辺に対する %）');
    v.mh = slider('ikl-margin-h', '余白（左右）', 20, '外周の余白の左と右（ページの短辺に対する %）');
    v.sides = {};
    const sideLabels = { top: '上', right: '右', bottom: '下', left: '左' };
    v.marginEach = el('div', { class: 'ikb-row ikl-margin-each' }, [el('span', { class: 'ikb-dim', text: '個別(%):' })]
      .concat(core.SIDES.map((side) => {
        v.sides[side] = el('input', { type: 'number', id: 'ikl-margin-' + side, min: '0', max: '40', step: '0.1',
          title: `外周の余白（${sideLabels[side]}）`, 'aria-label': `外周の余白（${sideLabels[side]}）`,
          onchange: () => run(() => setMargin({ [side]: (parseFloat(v.sides[side].value) || 0) / 100 })) });
        return el('label', {}, [el('span', { text: sideLabels[side] }), v.sides[side]]);
      })));
    v.selName = el('div', { class: 'ikb-dim ikl-sel-name' });
    v.splitH = el('button', { type: 'button', id: 'ikl-split-h', text: '縦に割る', title: '縦の線で左右に等分する（Alt+クリックで、次に押した位置で割る）',
      onclick: (e) => run(() => doSplit('h', e.altKey)) });
    v.splitV = el('button', { type: 'button', id: 'ikl-split-v', text: '横に割る', title: '横の線で上下に等分する（Alt+クリックで、次に押した位置で割る）',
      onclick: (e) => run(() => doSplit('v', e.altKey)) });
    v.merge = el('button', { type: 'button', id: 'ikl-merge', text: '結合', title: '隣り合う 2 つのコマ（Shift+クリックで 2 つ目を選ぶ）を 1 つにする',
      onclick: () => run(() => merge(st.sel[0], st.sel[1])) });
    v.removeImages = el('select', { id: 'ikl-remove-images', 'aria-label': '消すコマの中の画像', title: '消すコマの中の画像' },
      [el('option', { value: 'keep', text: '画像は残す' }), el('option', { value: 'delete', text: '画像も消す' })]);
    v.remove = el('button', { type: 'button', id: 'ikl-remove', text: '削除', title: 'コマを消して、隣のコマで埋める',
      onclick: () => run(() => remove(st.sel[0], { images: v.removeImages.value })) });
    v.unfree = el('button', { type: 'button', id: 'ikl-unfree', text: '連動に戻す', title: '自由に変形したコマを、元の長方形に戻す',
      onclick: () => run(() => clearFree(st.sel[0])) });
    v.bleed = {};
    v.bleedRow = el('div', { class: 'ikb-row ikl-bleed' }, [el('span', { class: 'ikb-dim', text: '断ち切り:' })]
      .concat(core.SIDES.map((side) => {
        v.bleed[side] = el('input', { type: 'checkbox', id: 'ikl-bleed-' + side,
          onchange: () => run(() => setBleed(st.sel[0], side, v.bleed[side].checked)) });
        return el('label', { title: `${sideLabels[side]}の辺をページの端まで伸ばす` }, [v.bleed[side], el('span', { text: sideLabels[side] })]);
      })));
    v.cell = el('div', { class: 'ikl-cell', hidden: true }, [v.selName,
      el('div', { class: 'ikb-row' }, [v.splitH, v.splitV, v.merge]),
      el('div', { class: 'ikb-row' }, [v.removeImages, v.remove, v.unfree]),
      v.bleedRow]);
    v.follow = el('select', { id: 'ikl-follow', 'aria-label': 'コマの形が変わったときの中の画像',
      onchange: () => { st.follow = v.follow.value; store.set('layoutFollow', st.follow); } },
    Object.entries(FOLLOW).map(([value, text]) => el('option', { value, text })));
    v.follow.value = st.follow;
    v.importBtn = el('button', { type: 'button', id: 'ikl-import', text: 'いまのコマを取り込む',
      title: 'ひな形やナイフで作った長方形のコマから、行と列の構成を推定して取り込む', onclick: () => run(() => importPanels()) });
    v.info = el('div', { class: 'ikb-dim ikl-info' });
    // 上流への差分と組になる設定（差分が当たっているときだけ出す）
    v.template = el('select', { id: 'ikl-template-target', 'aria-label': '漫画エディタのコマのひな形の適用先',
      onchange: () => run(() => setTemplateTarget(v.template.value)) },
    [el('option', { value: 'new', text: '新しいページを作る' }), el('option', { value: 'current', text: 'いまのページのコマを置き換える' })]);
    v.template.value = st.templateTarget;
    v.templateRow = el('label', { class: 'ikl-option', hidden: !patched(PATCH.template),
      title: '漫画エディタのサイドバーにあるコマのひな形を押したときの動き' }, [el('span', { class: 'ikb-dim', text: 'ひな形を押したら:' }), v.template]);
    v.knife = el('select', { id: 'ikl-knife', 'aria-label': 'ナイフで切ったときの中の画像',
      onchange: () => run(() => setKnifeRefit(v.knife.value === 'refit')) },
    [el('option', { value: 'keep', text: '位置と大きさを保つ' }), el('option', { value: 'refit', text: '中央に入れ直す（元の挙動）' })]);
    v.knife.value = st.knifeRefit ? 'refit' : 'keep';
    v.knifeRow = el('label', { class: 'ikl-option', hidden: !patched(PATCH.knife),
      title: '漫画エディタのナイフでコマを切ったときの、中の画像の扱い' }, [el('span', { class: 'ikb-dim', text: 'ナイフで切ったら:' }), v.knife]);
    v.section = el('details', { class: 'ikb-section ikl', id: 'ikl' }, [
      el('summary', { text: 'コマ割り' }),
      el('div', { class: 'ikl-body' }, [
        el('div', { class: 'ikb-row' }, [v.counts, v.grid]),
        v.presets,
        el('div', { class: 'ikb-row' }, [v.mode, v.importBtn]),
        v.cell,
        v.gv.row, v.gh.row, v.mv.row, v.mh.row, v.marginEach,
        el('label', { class: 'ikb-row ikl-follow' }, [el('span', { class: 'ikb-dim', text: '中の画像:' }), v.follow]),
        v.templateRow, v.knifeRow,
        v.info,
      ]),
    ]);
    v.section.open = store.get('layoutOpen', false) === true;
    v.section.addEventListener('toggle', () => store.set('layoutOpen', v.section.open));
    (ui.fileSection || ui.body.lastChild).before(v.section);

    const wire = (s, build) => {
      s.input.addEventListener('input', () => run(() => {
        if (!current()) return;
        liveBegin();
        liveApply(build(live.base, parseFloat(s.input.value) / 100));
        s.out.textContent = s.input.value + '%';
      }));
      s.input.addEventListener('change', () => liveEnd(false));
    };
    wire(v.gv, (L, x) => core.setGutter(L, { v: x }));
    wire(v.gh, (L, x) => core.setGutter(L, { h: x }));
    wire(v.mv, (L, x) => core.setMargin(L, { top: x, bottom: x }));
    wire(v.mh, (L, x) => core.setMargin(L, { left: x, right: x }));
  }

  function doSplit(dir, armed) {
    if (armed) {
      if (!st.mode) enter();
      st.arm = dir;
      say(dir === 'h' ? '左右に割る位置で、コマをクリックしてください' : '上下に割る位置で、コマをクリックしてください');
      return null;
    }
    if (st.sel.length !== 1) throw new Error('割るコマを 1 つ選んでください（コマ割りモードでコマをクリック）');
    return split(st.sel[0], dir);
  }

  function renderPanel() {
    const v = view;
    if (!v.section) return;
    const L = current();
    v.mode.setAttribute('aria-pressed', String(st.mode));
    v.mode.classList.toggle('ikb-primary', st.mode);
    v.mode.textContent = st.mode ? 'コマ割りモードを終了' : 'コマ割りモード';
    v.mode.disabled = !L && !st.mode;
    [v.gv, v.gh, v.mv, v.mh].forEach((s) => { s.input.disabled = !L; });
    Object.values(v.sides).forEach((input) => { input.disabled = !L; });
    if (L && !live.on) {
      const set = (s, value) => { s.input.value = String(pct(value)); s.out.textContent = pct(value) + '%'; };
      set(v.gv, L.gutter.v);
      set(v.gh, L.gutter.h);
      set(v.mv, L.margin.top);
      set(v.mh, L.margin.left);
      core.SIDES.forEach((side) => { if (document.activeElement !== v.sides[side]) v.sides[side].value = String(pct(L.margin[side])); });
    }
    const { W, H } = size();
    const C = L ? core.compute(L, W, H) : null;
    const ids = L ? core.order(L) : [];
    const sel = st.sel.filter((id) => C && C.cells[id]);
    v.cell.hidden = !sel.length;
    if (sel.length) {
      const first = C.cells[sel[0]];
      v.selName.textContent = '選択中: コマ ' + sel.map((id) => ids.indexOf(id) + 1).join(' と ')
        + (first.free && sel.length === 1 ? '（連動から外れています）' : '');
      v.splitH.disabled = v.splitV.disabled = sel.length !== 1;
      v.merge.disabled = sel.length !== 2;
      v.remove.disabled = v.removeImages.disabled = sel.length !== 1;
      v.unfree.hidden = !(sel.length === 1 && first.free);
      v.bleedRow.hidden = sel.length !== 1 || !core.SIDES.some((side) => first.edges[side]);
      const leaf = core.find(L, sel[0]).node;
      core.SIDES.forEach((side) => {
        v.bleed[side].parentNode.hidden = !first.edges[side];
        v.bleed[side].checked = !!(leaf.bleed && leaf.bleed[side]);
      });
    }
    const outside = allPanels().length - (L ? core.leaves(L).filter((leaf) => panelByGuid(leaf.guid)).length : 0);
    v.info.textContent = L ? `コマ ${ids.length} 個` + (outside > 0 ? `（レイアウトの外のコマ ${outside} 個）` : '')
      : (allPanels().length ? 'このページのコマは、まだレイアウトに入っていません（「いまのコマを取り込む」）' : 'このページにコマはありません');
  }

  function render() { renderPanel(); draw(); }

  // ---- 漫画エディタの動きに合わせる ----

  function onRestored() {
    // 取り消し・やり直し・ページの切り替えのあと（オブジェクトが作り直されている）
    const L = current();
    st.sel = st.sel.filter((id) => L && core.find(L, id));
    st.hover = null;
    if (st.mode) relock();
    render();
  }

  function hook() {
    [LAYOUT_KEY, CELL_KEY].forEach((key) => { if (commonProperties.indexOf(key) === -1) commonProperties.push(key); });
    // 履歴・ページの読み込み（loadFromJSON）: レイアウトの無い状態へ戻るときは、いまのレイアウトを残さない
    const load = fabric.Canvas.prototype.loadFromJSON;
    fabric.Canvas.prototype.loadFromJSON = function (json, callback, reviver) {
      const mine = this === canvas;
      if (mine) delete this[LAYOUT_KEY];
      return load.call(this, json, function () {
        const result = callback ? callback.apply(this, arguments) : undefined;
        if (mine) { try { onRestored(); } catch (e) { console.error('[inkdeus]', e); } }
        return result;
      }, reviver);
    };
    // モードの解除（ESC・サイドバーの切り替え・ほかのモードへの切り替えは、すべて clearAll を通る）
    const clearAll = ModeManager.clearAll;
    ModeManager.clearAll = function () {
      leave();
      return clearAll.apply(this, arguments);
    };
    // 「モード解除」のボタンは、置き換える前の clearAll を直接呼ぶ
    const button = document.getElementById('clearMode');
    if (button) button.addEventListener('click', () => leave());
    // 素材パネルにフォーカスがある間は、キー操作が漫画エディタへ届かない（パネルが止めている）。ESC はここで拾う
    ui.root.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && st.mode && !(e.target.closest && e.target.closest('dialog'))) exit();
    });
    canvas.on('after:render', () => { if (st.mode) draw(); });
    // 漫画エディタ側の操作（Delete キー・ナイフなど）でコマが消されたときは、その場所を空けたままにする
    // （葉は残る。コマ割りモードでその場所を選んで「削除」すると、隣のコマが埋める）
    canvas.on('object:modified', (e) => {
      const panel = e.target;
      if (st.busy || !panel || !panel.isPanel || !panel[CELL_KEY]) return;
      const L = current();
      if (!L || !core.find(L, panel[CELL_KEY])) return;
      // 手で動かした・頂点を編集したコマは、その形のまま連動から外す
      const { W, H } = size();
      setLayout(core.setFree(L, panel[CELL_KEY], absPoints(panel).map((p) => [p[0] / W, p[1] / H])));
      render();
    });
  }

  // ---- 起動 ----

  if (missing.length) {
    // 対応していない版（必要な関数が無い）。欄だけ出して、理由を書く
    const note = el('details', { class: 'ikb-section ikl', id: 'ikl' }, [el('summary', { text: 'コマ割り' }),
      el('div', { class: 'ikb-dim', text: `対応していない版のため使えません（必要な関数: ${missing.join(', ')}）` })]);
    (ui.fileSection || ui.body.lastChild).before(note);
    bridge.layout = { supported: false, missing };
    return;
  }

  st.templateTarget = store.get('templateTarget', 'new') === 'current' ? 'current' : 'new';
  st.knifeRefit = store.get('knifeRefit', false) === true;
  window.inkdeusKnifeRefit = st.knifeRefit;
  // ひな形の差分（0002）が呼ぶ入口: true を返すと、漫画エディタは新しいページを作らない
  window.inkdeusTemplateHook = async (item) => {
    if (st.templateTarget !== 'current' || !item || !item.svg) return false;
    await run(() => applyTemplate(item.svg));
    return true;
  };
  hook();
  buildPanel();
  render();
  setInterval(renderPanel, 1000);              // ページの切り替え・コマの増減に追いつく

  bridge.layout = {
    supported: true, core,
    get: info,
    grid: makeGrid, set: setAll, clear: clearLayout, importPanels,
    gutter: setGutter, margin: setMargin, bleed: setBleed, rtl: setRtl,
    moveBoundary, tilt: setTilt, resetTilt, free: setFree, unfree: clearFree,
    split, merge, remove,
    applyTemplate, setTemplateTarget, setKnifeRefit,
    options: () => ({ templateTarget: st.templateTarget, knifeRefit: st.knifeRefit,
      patches: { template: patched(PATCH.template), knife: patched(PATCH.knife) } }),
    enter, exit, isActive: () => st.mode,
    select(refs) { st.sel = (Array.isArray(refs) ? refs : [refs]).filter((r) => r != null).map(cellId).slice(0, 2); render(); return st.sel.slice(); },
    setFollow(mode) { if (!FOLLOW[mode]) throw new Error('keep / none / cover のどれかです'); st.follow = mode; view.follow.value = mode; store.set('layoutFollow', mode); return mode; },
    // 画面上の位置（テスト・自動操作用）: 境界線の中点と端のつまみ、コマの中心を、ブラウザの座標で返す
    screen() {
      const L = current();
      if (!L) return { bounds: {}, cells: {} };
      const { W, H } = size();
      const C = core.compute(L, W, H);
      const rect = canvas.upperCanvasEl.getBoundingClientRect();
      const at = (p) => ({ x: rect.left + p[0] / W * rect.width, y: rect.top + p[1] / H * rect.height });
      const out = { bounds: {}, cells: {}, scale: rect.width / W };
      Object.values(C.bounds).forEach((b) => {
        out.bounds[b.key] = { dir: b.dir, mid: at([(b.a[0] + b.b[0]) / 2, (b.a[1] + b.b[1]) / 2]),
          ends: [at(handlePos(b, 0)), at(handlePos(b, 1))] };
      });
      Object.values(C.cells).forEach((c) => {
        const box = core.bbox(c.pts);
        out.cells[c.id] = { center: at([(box.l + box.r) / 2, (box.t + box.b) / 2]), points: c.pts.map(at),
          rect: { l: at([c.rect.l, c.rect.t]).x, t: at([c.rect.l, c.rect.t]).y, r: at([c.rect.r, c.rect.b]).x, b: at([c.rect.r, c.rect.b]).y } };
      });
      return out;
    },
  };

  // 外部（inkdeus ctl manga layout …）からの指示
  commands.layout = async (args) => {
    const a = args || {};
    if (a.action === 'get') return info();
    if (a.action === 'grid') return makeGrid(a.counts, { force: true });
    if (a.action === 'set') return setAll(a.layout, { force: true });
    if (a.action === 'gutter') return setGutter(a.v, a.h);
    if (a.action === 'margin') return setMargin(a.margin);
    throw new Error('layout の指示は get / grid / set / gutter / margin のどれかです');
  };
})();
