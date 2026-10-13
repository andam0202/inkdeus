// 右側の設定パネル。schema.js の定義からコントロールを組み立てる。
// 値の変更は ctx.live()（再描画だけ）と ctx.commit()（履歴に積む・保存する）で通知する。

import {
  SECTIONS, LEVEL_FIELDS, SOLID_FIELDS, BACKGROUND_FIELDS, LAYER_FIELDS, STYLES, MAX_LAYERS, UNIT_RANGE,
  effectiveUnit, newLayer, newId, layerIds, WATERMARK_FALLBACK_TEXT, regionLabel, newPart,
} from './schema.js';

const SECTION_LABELS = { input: '入力', lines: '線画', tone: 'トーン（ベタ・層）', watermark: '透かし', output: '出力' };

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

function fieldRow(ctx, f, obj, key) {
  const def = f.def;
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
    ...Object.entries(SECTIONS.input).map(([k, f]) => fieldRow(ctx, f, params.input, k)),
  ]);
}

function simpleSection(ctx, key, defaultOpen = true) {
  const { params } = ctx.state;
  return section(key, [
    ...Object.entries(SECTIONS[key]).map(([k, f]) => fieldRow(ctx, f, params[key], k)),
    key === 'lines' ? erasedMark(ctx, 'lines', '線画には、消しゴムで消した所があります') : null,
  ], defaultOpen);
}

/** 消しゴムで消した所があることを示す小さな印 */
function erasedMark(ctx, target, title) {
  return ctx.painted(target) ? el('span', { class: 'erased', 'data-erased': target, title }, '消しあり') : null;
}

function layerCard(ctx, region, layers, layer, index) {
  const { solo, selLayer } = ctx.state;
  const isSolo = !!solo && solo.region === region && solo.index === index;
  const isSelected = !!selLayer && selLayer.id === layer.id;
  const change = (fn) => (e) => {
    e.preventDefault();
    e.stopPropagation();
    fn();
    ctx.setSolo(null, false);
    ctx.live(); ctx.commit(); ctx.rebuild();
  };
  const enabled = el('input', { type: 'checkbox', title: 'この層を表示する' });
  enabled.checked = layer.enabled;
  enabled.addEventListener('click', (e) => e.stopPropagation());
  enabled.addEventListener('change', () => { layer.enabled = enabled.checked; ctx.live(); ctx.commit(); ctx.rebuild(); });
  const soloBtn = el('button', {
    class: isSolo ? 'on' : '', title: 'ソロ表示（この層だけを見る）',
    onclick: (e) => { e.preventDefault(); e.stopPropagation(); ctx.setSolo(isSolo ? null : { region, index }, true); },
  }, 'ソロ');
  const summary = el('summary', {},
    enabled,
    el('span', { class: 'title' }, `層${index + 1}  <${layer.threshold.toFixed(2)}  ${layer.angle}°`),
    erasedMark(ctx, `layer-${layer.id}`, 'この層には、消しゴムで消した所があります'),
    soloBtn,
    el('button', { title: '上へ', disabled: index === 0,
      onclick: change(() => layers.splice(index - 1, 0, layers.splice(index, 1)[0])) }, '↑'),
    el('button', { title: '下へ', disabled: index === layers.length - 1,
      onclick: change(() => layers.splice(index + 1, 0, layers.splice(index, 1)[0])) }, '↓'),
    el('button', { title: '複製', disabled: layers.length >= MAX_LAYERS,
      onclick: change(() => layers.splice(index + 1, 0, { ...layer, id: newId(layerIds(ctx.state.params)) })) }, '複製'),
    el('button', { title: '削除', onclick: change(() => layers.splice(index, 1)) }, '削除'));
  const body = el('div', { class: 'body' },
    Object.entries(LAYER_FIELDS)
      .filter(([k]) => k !== 'enabled' && k !== 'type')
      .map(([k, f]) => fieldRow(ctx, f, layer, k)));
  // 行をクリックすると、消しゴムを当てる「選択中の層」になる（ボタン・チェックは除く。ソロ表示とは別）
  summary.title = 'クリックで、消しゴムを当てる層として選ぶ';
  summary.addEventListener('click', (e) => {
    if (!e.target.closest('button, input')) ctx.selectLayer(region, layer.id);
  });
  const cls = ['layer', layer.enabled ? '' : 'off', isSolo ? 'solo' : '', isSelected ? 'selected' : ''].join(' ');
  const card = fold(`layer:${region}:${index}`, summary, body, cls, index === 0);
  card.dataset.layer = index;
  card.dataset.id = layer.id;
  return card;
}

function styleSelect(ctx, region) {
  const part = region !== 'person' && region !== 'background';
  const sel = el('select', { id: 'selPreset', title: `${regionLabel(region)}に名前付きスタイルを当てる` },
    el('option', { value: '' }, 'スタイルを選ぶ…'),
    // 部分には、背景向けの意味を持つスタイル（白・線だけ）は出さない（「白抜き」を使う）
    Object.entries(STYLES).filter(([, st]) => !(part && st.backgroundOnly))
      .map(([value, st]) => el('option', { value, title: st.description }, st.label)));
  // 自分用スタイル（サーバが対応しているときだけ）: 組み込みの下に「自分用」「このプロジェクト」の見出しで並べる
  const mine = ctx.userStyles ? ctx.userStyles(region) : null;
  if (mine) {
    for (const [origin, label] of [['user', '自分用'], ['project', 'このプロジェクト']]) {
      const items = mine.filter((r) => r.origin === origin);
      if (!items.length) continue;
      sel.append(el('optgroup', { label, 'data-origin': origin }, items.map((r) => el('option', {
        value: `@${origin}:${r.name}`, title: r.description || null,
      }, `${r.name}${r.includeLevels ? '（明るさ補正つき）' : ''}`))));
    }
    sel.append(el('optgroup', { label: '自分用スタイル', 'data-origin': 'actions' },
      el('option', { value: '@save' }, 'いまの設定をスタイルとして保存…'),
      el('option', { value: '@manage' }, '自分用スタイルを管理…')));
  }
  sel.addEventListener('change', () => {
    const value = sel.value;
    sel.value = '';                   // 選んだままにしない（ダイアログを開く項目もあるため）
    if (value) ctx.style(value, region);
  });
  return sel;
}

/** 領域（人物 / 背景 / 部分）の 明るさ補正・ベタ・層 */
function regionTone(ctx, region, target) {
  const { solo } = ctx.state;
  const layers = target.layers;
  const add = el('button', {
    id: 'btnAddLayer', disabled: layers.length >= MAX_LAYERS,
    onclick: () => {
      // 新しい層は、いちばん暗い層よりさらに暗い所に置く
      const last = layers[layers.length - 1];
      layers.push(newLayer(last ? {
        threshold: Math.max(0.05, Number((last.threshold * 0.75).toFixed(3))),
        angle: (last.angle + 45) % 180, spacing: last.spacing, width: last.width,
      } : { threshold: 0.6 }, layerIds(ctx.state.params)));
      ctx.setSolo(null, false);
      ctx.live(); ctx.commit(); ctx.rebuild();
    },
  }, '層を追加');
  const levels = fold(`levels:${region}`,
    el('summary', {}, `${regionLabel(region)}の明るさ補正（トーン分けだけに効く）`),
    el('div', { class: 'body' }, Object.entries(LEVEL_FIELDS).map(([k, f]) => fieldRow(ctx, f, target.levels, k))),
    'sub', false);
  levels.dataset.group = 'levels';
  return [
    levels,
    el('div', { 'data-group': 'solid', class: 'body' },
      // 「ベタの中の線画を白抜き」は、それを知らない古いサーバにつないでいる間は出さない
      Object.entries(SOLID_FIELDS).filter(([k]) => k !== 'lineKnockout' || ctx.state.partsOK)
        .map(([k, f]) => fieldRow(ctx, f, target.solid, k)),
      erasedMark(ctx, `solid-${region}`, `${regionLabel(region)}のベタには、消しゴムで消した所があります`)),
    el('div', { class: 'buttons' }, add, el('span', { class: 'note' }, `${layers.length} / ${MAX_LAYERS} 層`)),
    el('div', { class: 'note' }, '平滑化した輝度がしきい値未満の所に線を引きます。暗い所ほど多くの層が重なります。'),
    solo && solo.region === region
      ? el('div', { class: 'note warn' }, `層${solo.index + 1} をソロ表示中（ベタと線画も隠しています）`) : null,
    ...layers.map((l, i) => layerCard(ctx, region, layers, l, i)),
  ];
}

/** 部分（髪・肌・服など）のタブの中身。背景タブと同じ流儀: 最初は人物と同じ設定で描き、切り離すと独自の設定を持つ */
function partBody(ctx, name) {
  const { params } = ctx.state;
  const own = params.regions[name];
  const label = regionLabel(name);
  if (!own || own.linked !== false) {
    return [
      el('div', { class: 'note', id: 'partLinkedNote' }, `${label}は人物と同じ設定で描いています。`),
      el('div', { class: 'buttons' }, el('button', {
        id: 'btnUnlinkPart',
        onclick: () => {
          // いまの人物の設定を写してから切り離す（切り離した瞬間に見た目が変わらないように）
          params.regions[name] = newPart(params, false);
          ctx.live(); ctx.commit(); ctx.rebuild();
        },
      }, 'この部分だけ別の設定にする')),
    ];
  }
  return [
    el('div', { class: 'buttons' }, styleSelect(ctx, name), el('button', {
      id: 'btnLinkPart',
      onclick: () => { own.linked = true; ctx.setSolo(null, false); ctx.live(); ctx.commit(); ctx.rebuild(); },
    }, '人物と同じに戻す')),
    ...regionTone(ctx, name, own),
  ];
}

function toneSection(ctx) {
  const { params, hasMask } = ctx.state;
  const person = params.regions.person;
  const bg = params.regions.background;
  // タブは「人物 / 背景 / その画像にマスクのある部分…」
  const names = ['person', 'background', ...(ctx.state.parts || [])];
  const tab = names.includes(ctx.state.tab) ? ctx.state.tab : 'person';
  const tabs = el('div', { class: 'tabs' }, names.map((r) => el('button', {
    class: tab === r ? 'on' : '', 'data-tab': r, onclick: () => ctx.setTab(r),
    title: r === 'person' || r === 'background' ? null : `部分「${r}」（部分マスクのある領域）`,
  }, regionLabel(r))));
  // 押している間、その領域を色つきで重ねて見せる（マスクの出来を目で確かめる）
  const hi = el('button', { id: 'btnHighlight', type: 'button',
    title: '押している間、このタブの領域を色つきで重ねて見せます（マスクの出来を確かめる）。プレビューを Alt+クリックすると、その場所の領域のタブへ切り替わります' },
  'この領域を強調表示');
  hi.addEventListener('pointerdown', () => ctx.highlight(tab));
  for (const ev of ['pointerup', 'pointerleave', 'pointercancel', 'blur']) hi.addEventListener(ev, () => ctx.highlight(null));
  const hiRow = ctx.highlight ? el('div', { class: 'buttons' }, hi) : null;
  let body;
  if (tab !== 'person' && tab !== 'background') {
    body = partBody(ctx, tab);
  } else if (tab === 'person') {
    body = [
      el('div', { class: 'buttons' }, styleSelect(ctx, 'person'),
        el('span', { class: 'note' }, hasMask ? 'マスクの内側' : 'マスクが無いので画像全体')),
      ...regionTone(ctx, 'person', person),
    ];
  } else {
    const general = ['mode', 'grow', 'silhouetteWidth', 'lineStrength']
      .map((k) => fieldRow(ctx, BACKGROUND_FIELDS[k], bg, k));
    general[0].querySelector('select').addEventListener('change', () => ctx.rebuild());
    const link = bg.linked
      ? [el('div', { class: 'note', id: 'bgLinkedNote' }, '背景は人物と同じ 明るさ補正・ベタ・層 で描いています。'),
        el('div', { class: 'buttons' }, el('button', {
          id: 'btnUnlink',
          onclick: () => {
            // いまの人物の設定を写してから切り離す（切り離した瞬間に見た目が変わらないように）
            bg.levels = { ...person.levels };
            bg.solid = { ...person.solid };
            const taken = layerIds(ctx.state.params);
            bg.layers = person.layers.map((l) => { const id = newId(taken); taken.add(id); return { ...l, id }; });
            bg.linked = false;
            ctx.live(); ctx.commit(); ctx.rebuild();
          },
        }, '背景だけ別の設定にする'))]
      : [el('div', { class: 'buttons' }, styleSelect(ctx, 'background'), el('button', {
        id: 'btnLink',
        onclick: () => { bg.linked = true; ctx.setSolo(null, false); ctx.live(); ctx.commit(); ctx.rebuild(); },
      }, '人物と同じに戻す')),
      bg.mode !== 'keep' ? el('div', { class: 'note warn' }, '背景の描き方が「そのまま」以外なので、下のベタと層は使われません。') : null,
      ...regionTone(ctx, 'background', bg)];
    body = [
      el('div', { class: 'buttons' },
        el('button', { onclick: () => ctx.openMask() }, 'マスクを開く'),
        el('span', { class: hasMask ? 'note' : 'note warn', id: 'maskNote' },
          hasMask ? 'マスクあり（白=人物）' : 'マスクが無いため背景の設定は無効です（全体が人物扱い）')),
      el('div', { class: hasMask ? '' : 'disabled', id: 'bgControls' }, el('div', { class: 'body' }, [...general, ...link])),
    ];
  }
  return section('tone', [tabs, hiRow, ...body]);
}

function watermarkSection(ctx) {
  const { params } = ctx.state;
  const w = params.watermark;
  const f = SECTIONS.watermark;
  const rows = [];
  rows.push(boolRow(ctx, f.enabled, w, 'enabled', () => ctx.rebuild()));
  rows.push(textRow(ctx, f.text, w, 'text', WATERMARK_FALLBACK_TEXT));
  rows.push(selectRow(ctx, f.position, w, 'position'));
  rows.push(rangeRow(ctx, f.size, w, 'size', f.size.def));
  rows.push(rangeRow(ctx, f.opacity, w, 'opacity', f.opacity.def));
  return section('watermark', rows, false);
}

function outputSection(ctx) {
  const { params, lastExport, server, remote, images, autoExport, batchForce, batchDefaultOnly, versionsOK,
    batchAllFolders, folders, lastBatch, backupOK, broken } = ctx.state;
  const allFolders = el('input', { type: 'checkbox', id: 'chkExportAllFolders' });
  allFolders.checked = !!batchAllFolders;
  allFolders.addEventListener('change', () => ctx.setBatchAllFolders(allFolders.checked));
  const auto = el('input', { type: 'checkbox', id: 'chkAutoExport' });
  auto.checked = !!autoExport;
  auto.addEventListener('change', () => ctx.setAutoExport(auto.checked));
  const force = el('input', { type: 'checkbox', id: 'chkExportAllForce' });
  force.checked = !!batchForce;
  force.addEventListener('change', () => ctx.setBatchForce(force.checked));
  const defaultOnly = el('input', { type: 'checkbox', id: 'chkExportAllDefaultOnly' });
  defaultOnly.checked = !!batchDefaultOnly;
  defaultOnly.addEventListener('change', () => ctx.setBatchDefaultOnly(defaultOnly.checked));
  return section('output', [
    ...Object.entries(SECTIONS.output).map(([k, f]) => fieldRow(ctx, f, params.output, k)),
    el('div', { class: 'buttons' },
      el('button', { class: 'primary', id: 'btnExport', onclick: () => ctx.exportNow() }, 'PNG を書き出す'),
      el('span', { class: 'note' }, server ? 'フル解像度・グレースケール' : 'フル解像度・グレースケール（ダウンロード）')),
    el('div', { class: 'note', id: 'exportInfo' }, lastExport || ''),
    server && remote && el('label', { class: 'check', title: '設定を変えると、少し待ってから PNG を保存します。漫画の構成の画面（/manga/）で使っている白黒版もその場で替わります' },
      auto, '変更を自動で書き出す（漫画の画面へ反映）'),
    server && images.length > 0 && el('div', { class: 'buttons' },
      el('button', { id: 'btnExportAll', onclick: () => ctx.exportAll(),
        title: '全画像の全バージョンを、それぞれに保存済みの設定で書き出します。設定の無い画像には、いま画面に出ている設定を使います' }, folders.length > 1 ? 'このフォルダの全画像を書き出す' : '全画像を書き出す'),
      el('label', { class: 'check', title: '書き出し済みで設定が変わっていない画像も作り直す' }, force, 'すべて作り直す'),
      versionsOK && el('label', { class: 'check', title: '名前付きのバージョンは書き出さない' }, defaultOnly, '既定のバージョンだけ'),
      folders.length > 1 && el('label', { class: 'check', title: 'いまの画像フォルダだけでなく、すべての画像フォルダを書き出す' },
        allFolders, 'すべてのフォルダ')),
    server && lastBatch && el('div', { class: 'note', id: 'batchInfo', style: 'white-space: pre-wrap' }, lastBatch),
    el('div', { class: 'buttons' },
      el('button', { id: 'btnSaveJSON', onclick: () => ctx.saveJSON() }, '設定を保存 (JSON)'),
      el('button', { id: 'btnOpenJSON', onclick: () => ctx.openJSON() }, '設定を開く')),
    server && remote && backupOK && !broken && el('div', { class: 'buttons' },
      el('button', { id: 'btnRestore', onclick: () => ctx.restore(),
        title: 'この画像・バージョンの設定と消しゴム・ペンを、保存のたびに残している前の版から選んで戻します（戻したあとで取り消せます）' },
      '以前の状態に戻す…')),
    el('div', { class: 'note' }, server
      ? '設定は変更のたびに画像ごとに自動保存されます。設定ファイルを外から書き換えると、この画面にも反映されます。'
      : 'サーバなしで動作中です。設定は JSON で保存・読み込みしてください。'),
  ]);
}

export function buildPanel(root, ctx) {
  const scroll = root.scrollTop;
  root.replaceChildren(
    inputSection(ctx),
    simpleSection(ctx, 'lines'),
    toneSection(ctx),
    watermarkSection(ctx),
    outputSection(ctx),
  );
  root.scrollTop = scroll;
}
