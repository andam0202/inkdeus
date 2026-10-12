// 右側の設定パネル。schema.js の定義からコントロールを組み立てる。
// 値の変更は ctx.live()（再描画だけ）と ctx.commit()（履歴に積む・保存する）で通知する。

import {
  FIELDS, LAYER_FIELDS, SECTION_LABELS, PRESETS, MAX_LAYERS, UNIT_RANGE,
  effectiveUnit, newLayer, WATERMARK_FALLBACK_TEXT,
} from './schema.js';

const openState = new Map();      // 折りたたみの開閉を作り直しの間も覚えておく

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === false || v === null || v === undefined) continue;
    if (k === 'class') node.className = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else if (v === true) node.setAttribute(k, '');
    else node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return node;
}

function decimals(step) {
  return step >= 1 ? 0 : step >= 0.1 ? 1 : step >= 0.01 ? 2 : 3;
}

function fold(key, summary, body, cls, defaultOpen) {
  const d = el('details', { class: cls });
  d.open = openState.has(key) ? openState.get(key) : defaultOpen;
  d.addEventListener('toggle', () => openState.set(key, d.open));
  d.append(summary, body);
  return d;
}

function rangeRow(ctx, f, obj, key, def) {
  const fmt = (v) => Number(v).toFixed(decimals(f.step));
  const label = el('label', { title: 'ダブルクリックで既定値に戻す' }, f.label + (f.px ? '（基準px）' : ''));
  const slider = el('input', { type: 'range', min: f.min, max: f.max, step: f.step });
  const number = el('input', { type: 'number', min: f.min, max: f.max, step: f.step });
  slider.value = obj[key];
  number.value = fmt(obj[key]);
  const apply = (raw, commit) => {
    let v = Number(raw);
    if (!Number.isFinite(v)) v = obj[key];
    v = Math.min(f.max, Math.max(f.min, v));
    obj[key] = v;
    slider.value = v;
    number.value = fmt(v);
    ctx.live();
    if (commit) ctx.commit();
  };
  // スライダーは動かしている間は再描画だけ、離した時点で1履歴
  slider.addEventListener('input', () => {
    obj[key] = Number(slider.value);
    number.value = fmt(obj[key]);
    ctx.live();
  });
  slider.addEventListener('change', () => ctx.commit());
  number.addEventListener('change', () => apply(number.value, true));
  const reset = (e) => { e.preventDefault(); apply(def, true); };
  label.addEventListener('dblclick', reset);
  slider.addEventListener('dblclick', reset);
  return el('div', { class: 'row', 'data-key': key }, label, slider, number);
}

function boolRow(ctx, f, obj, key, onChange) {
  const box = el('input', { type: 'checkbox' });
  box.checked = !!obj[key];
  box.addEventListener('change', () => {
    obj[key] = box.checked;
    ctx.live();
    ctx.commit();
    if (onChange) onChange();
  });
  return el('label', { class: 'check', 'data-key': key }, box, f.label);
}

function selectRow(ctx, f, obj, key) {
  const sel = el('select', { class: 'wide' }, f.options.map(([value, label]) => el('option', { value }, label)));
  sel.value = obj[key];
  sel.addEventListener('change', () => {
    obj[key] = sel.value;
    ctx.live();
    ctx.commit();
  });
  return el('div', { class: 'row', 'data-key': key }, el('label', {}, f.label), sel);
}

function textRow(ctx, f, obj, key, fallback) {
  const input = el('input', { type: 'text', class: 'wide', maxlength: f.maxLength });
  input.value = obj[key];
  input.addEventListener('input', () => { obj[key] = input.value; ctx.live(); });
  input.addEventListener('change', () => {
    if (fallback && !input.value.trim()) {      // 空にはできない
      input.value = fallback;
      obj[key] = fallback;
      ctx.live();
    }
    ctx.commit();
  });
  return el('div', { class: 'row', 'data-key': key }, el('label', {}, f.label), input);
}

function fieldRow(ctx, f, obj, key, owner) {
  const def = owner && 'ownerDef' in f ? f.ownerDef : f.def;
  if (f.type === 'range') return rangeRow(ctx, f, obj, key, def);
  if (f.type === 'bool') return boolRow(ctx, f, obj, key);
  if (f.type === 'select') return selectRow(ctx, f, obj, key);
  return textRow(ctx, f, obj, key);
}

function section(key, body, defaultOpen = true) {
  return fold(`section:${key}`, el('summary', {}, SECTION_LABELS[key]), el('div', { class: 'body' }, body),
    'section', defaultOpen);
}

// ---- 各セクション ----

function inputSection(ctx) {
  const { params } = ctx.state;
  const size = ctx.imageSize();
  const auto = params.unit === null;
  const unitNow = effectiveUnit(params, size.w);
  const autoBox = el('input', { type: 'checkbox' });
  autoBox.checked = auto;
  autoBox.addEventListener('change', () => {
    params.unit = autoBox.checked ? null : Number(unitNow.toFixed(3));
    ctx.live(); ctx.commit(); ctx.rebuild();
  });
  const unitInput = el('input', { type: 'number', min: UNIT_RANGE.min, max: UNIT_RANGE.max, step: 0.05 });
  unitInput.value = unitNow.toFixed(3);
  unitInput.disabled = auto;
  unitInput.addEventListener('change', () => {
    const v = Number(unitInput.value);
    if (Number.isFinite(v) && v > 0) params.unit = Math.min(UNIT_RANGE.max, Math.max(UNIT_RANGE.min, v));
    ctx.live(); ctx.commit(); ctx.rebuild();
  });
  return section('input', [
    el('div', { class: 'note' }, size.w ? `画像: ${size.w} × ${size.h} px` : '画像が読み込まれていません'),
    el('div', { class: 'row', 'data-key': 'unit' },
      el('label', { title: '基準px に掛ける倍率。自動 = 画像の幅 / 1280' }, '画像倍率 unit'),
      el('label', { class: 'check' }, autoBox, '自動'), unitInput),
    ...Object.entries(FIELDS.input).map(([k, f]) => fieldRow(ctx, f, params.input, k)),
  ]);
}

function simpleSection(ctx, key, defaultOpen = true) {
  const { params } = ctx.state;
  return section(key, Object.entries(FIELDS[key]).map(([k, f]) => fieldRow(ctx, f, params[key], k)), defaultOpen);
}

function layerCard(ctx, layer, index) {
  const { params, solo } = ctx.state;
  const layers = params.layers;
  const change = (fn) => (e) => {
    e.preventDefault();
    e.stopPropagation();
    fn();
    ctx.setSolo(-1, false);
    ctx.live(); ctx.commit(); ctx.rebuild();
  };
  const enabled = el('input', { type: 'checkbox', title: 'この層を表示する' });
  enabled.checked = layer.enabled;
  enabled.addEventListener('click', (e) => e.stopPropagation());
  enabled.addEventListener('change', () => { layer.enabled = enabled.checked; ctx.live(); ctx.commit(); ctx.rebuild(); });
  const soloBtn = el('button', {
    class: solo === index ? 'on' : '', title: 'ソロ表示（この層だけを見る）',
    onclick: (e) => { e.preventDefault(); e.stopPropagation(); ctx.setSolo(solo === index ? -1 : index, true); },
  }, 'ソロ');
  const summary = el('summary', {},
    enabled,
    el('span', { class: 'title' }, `層${index + 1}  <${layer.threshold.toFixed(2)}  ${layer.angle}°`),
    soloBtn,
    el('button', { title: '上へ', disabled: index === 0,
      onclick: change(() => layers.splice(index - 1, 0, layers.splice(index, 1)[0])) }, '↑'),
    el('button', { title: '下へ', disabled: index === layers.length - 1,
      onclick: change(() => layers.splice(index + 1, 0, layers.splice(index, 1)[0])) }, '↓'),
    el('button', { title: '複製', disabled: layers.length >= MAX_LAYERS,
      onclick: change(() => layers.splice(index + 1, 0, { ...layer })) }, '複製'),
    el('button', { title: '削除', onclick: change(() => layers.splice(index, 1)) }, '削除'));
  const body = el('div', { class: 'body' },
    Object.entries(LAYER_FIELDS)
      .filter(([k]) => k !== 'enabled' && k !== 'type')
      .map(([k, f]) => fieldRow(ctx, f, layer, k)));
  const cls = ['layer', layer.enabled ? '' : 'off', solo === index ? 'solo' : ''].join(' ');
  const card = fold(`layer:${index}`, summary, body, cls, index === 0);
  card.dataset.layer = index;
  return card;
}

function layersSection(ctx) {
  const { params, solo } = ctx.state;
  const preset = el('select', { id: 'selPreset' },
    el('option', { value: '' }, 'プリセットを選ぶ…'),
    Object.entries(PRESETS).map(([value, p]) => el('option', { value }, p.label)));
  preset.addEventListener('change', () => { if (preset.value) ctx.preset(preset.value); });
  const add = el('button', {
    id: 'btnAddLayer', disabled: params.layers.length >= MAX_LAYERS,
    onclick: () => {
      // 新しい層は、いちばん暗い層よりさらに暗い所に置く
      const last = params.layers[params.layers.length - 1];
      params.layers.push(newLayer(last ? {
        threshold: Math.max(0.05, Number((last.threshold * 0.75).toFixed(3))),
        angle: (last.angle + 45) % 180, spacing: last.spacing, width: last.width,
      } : { threshold: 0.6 }));
      ctx.setSolo(-1, false);
      ctx.live(); ctx.commit(); ctx.rebuild();
    },
  }, '層を追加');
  return section('layers', [
    el('div', { class: 'buttons' }, preset, add,
      el('span', { class: 'note' }, `${params.layers.length} / ${MAX_LAYERS} 層`)),
    el('div', { class: 'note' }, '平滑化した輝度がしきい値未満の所に線を引きます。暗い所ほど多くの層が重なります。'),
    solo >= 0 ? el('div', { class: 'note warn' }, `層${solo + 1} をソロ表示中（ベタと線画も隠しています）`) : null,
    ...params.layers.map((l, i) => layerCard(ctx, l, i)),
  ]);
}

function backgroundSection(ctx) {
  const { params, hasMask } = ctx.state;
  const rows = Object.entries(FIELDS.background).map(([k, f]) => fieldRow(ctx, f, params.background, k));
  return section('background', [
    el('div', { class: 'buttons' },
      el('button', { onclick: () => ctx.openMask() }, 'マスクを開く'),
      el('span', { class: hasMask ? 'note' : 'note warn', id: 'maskNote' },
        hasMask ? 'マスクあり（白=人物）' : 'マスクが無いため背景処理は無効です')),
    el('div', { class: hasMask ? '' : 'disabled', id: 'bgControls' }, el('div', { class: 'body' }, rows)),
  ]);
}

function watermarkSection(ctx) {
  const { params, owner } = ctx.state;
  const w = params.watermark;
  const f = FIELDS.watermark;
  const rows = [];
  if (owner) {
    rows.push(boolRow(ctx, f.enabled, w, 'enabled', () => ctx.rebuild()));
  } else {
    rows.push(el('div', { class: 'note', id: 'wmForcedNote' }, '透かしは常に入ります（所有者モードでのみ外せます）。'));
    if (w.enabled === false) {
      rows.push(el('div', { class: 'note warn', id: 'wmIgnoredNote' },
        '設定では透かしがオフですが、所有者モードではないため無視しています。'));
    }
  }
  rows.push(textRow(ctx, f.text, w, 'text', WATERMARK_FALLBACK_TEXT));
  rows.push(selectRow(ctx, f.position, w, 'position'));
  rows.push(rangeRow(ctx, f.size, w, 'size', f.size.def));
  rows.push(rangeRow(ctx, f.opacity, w, 'opacity', f.opacity.def));
  return section('watermark', rows, false);
}

function outputSection(ctx) {
  const { params, lastExport, server } = ctx.state;
  return section('output', [
    ...Object.entries(FIELDS.output).map(([k, f]) => fieldRow(ctx, f, params.output, k)),
    el('div', { class: 'buttons' },
      el('button', { class: 'primary', id: 'btnExport', onclick: () => ctx.exportNow() }, 'PNG を書き出す'),
      el('span', { class: 'note' }, server ? 'フル解像度・グレースケール' : 'フル解像度・グレースケール（ダウンロード）')),
    el('div', { class: 'note', id: 'exportInfo' }, lastExport || ''),
    el('div', { class: 'buttons' },
      el('button', { id: 'btnSaveJSON', onclick: () => ctx.saveJSON() }, '設定を保存 (JSON)'),
      el('button', { id: 'btnOpenJSON', onclick: () => ctx.openJSON() }, '設定を開く')),
    el('div', { class: 'note' }, server
      ? '設定は変更のたびに画像ごとに自動保存されます。'
      : 'サーバなしで動作中です。設定は JSON で保存・読み込みしてください。'),
  ]);
}

export function buildPanel(root, ctx) {
  const scroll = root.scrollTop;
  root.replaceChildren(
    inputSection(ctx),
    simpleSection(ctx, 'lines'),
    simpleSection(ctx, 'solid'),
    layersSection(ctx),
    backgroundSection(ctx),
    watermarkSection(ctx),
    outputSection(ctx),
  );
  root.scrollTop = scroll;
}
