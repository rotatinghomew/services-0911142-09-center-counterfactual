const state = {context: null, current: null, study: null, areas: []};
const element = identifier => document.getElementById(identifier);

async function request(path, body) {
  const response = await fetch(path, body === undefined ? {} : {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
  });
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || '请求失败');
  return value;
}

function message(value, error = false) {
  element('status').textContent = value;
  element('status').className = error ? 'error' : '';
}

async function guarded(action) {
  try { await action(); } catch (error) { message(error.message, true); }
}

function selected() {
  const selection = {};
  for (const control of state.context.product.options) selection[control[0]] = element('select-' + control[0]).value;
  return {revision: element('revision').value, selection};
}

function renderControls() {
  const revision = element('revision').value || state.context.default_revision;
  element('revision').replaceChildren();
  for (const dataset of state.context.datasets) {
    const option = document.createElement('option');
    option.value = dataset.revision;
    option.textContent = dataset.label + ' · ' + dataset.revision.slice(0, 9);
    element('revision').append(option);
  }
  element('revision').value = revision;
  if (!element('revision').value) element('revision').value = state.context.default_revision;
  if (element('controls').children.length) return;
  for (const [key, labelText, choices] of state.context.product.options) {
    const label = document.createElement('label');
    label.textContent = labelText;
    const select = document.createElement('select');
    select.id = 'select-' + key;
    for (const choice of choices) {
      const option = document.createElement('option');
      option.value = choice;
      option.textContent = choice;
      select.append(option);
    }
    select.value = state.context.product.selection[key];
    label.append(select);
    element('controls').append(label);
  }
}

function displayValue(value) {
  if (value === null || value === undefined) return '未观测';
  if (typeof value === 'number') return Number.isInteger(value) ? String(value) : value.toFixed(4);
  return typeof value === 'object' ? JSON.stringify(value) : String(value);
}

function drawChart(series) {
  const canvas = element('chart');
  const context = canvas.getContext('2d');
  context.clearRect(0, 0, canvas.width, canvas.height);
  const values = series.filter(point => typeof point.y === 'number' && Number.isFinite(point.y));
  context.font = '12px sans-serif';
  if (!values.length) { context.fillText('该范围没有可定量的描述值', 30, 50); return; }
  const low = Math.min(0, ...values.map(point => point.y));
  const high = Math.max(1, ...values.map(point => point.y));
  const scale = value => 250 - (value - low) / (high - low || 1) * 210;
  context.strokeStyle = '#90a5ad';
  context.beginPath(); context.moveTo(45, 20); context.lineTo(45, 250); context.lineTo(940, 250); context.stroke();
  context.fillStyle = '#0a7168';
  const scatter = values.every(point => typeof point.x === 'number');
  const minimumX = scatter ? Math.min(...values.map(point => point.x)) : 0;
  const maximumX = scatter ? Math.max(...values.map(point => point.x)) : 1;
  values.forEach((point, index) => {
    const position = scatter ? 60 + (point.x - minimumX) / (maximumX - minimumX || 1) * 850 : 55 + index * 875 / values.length;
    if (scatter) {
      context.beginPath(); context.arc(position, scale(point.y), 3.5, 0, Math.PI * 2); context.fill();
    } else {
      context.fillRect(position, Math.min(scale(point.y), scale(0)), Math.max(3, 800 / values.length), Math.max(1, Math.abs(scale(point.y) - scale(0))));
    }
    if (values.length < 12) context.fillText(point.label, position, 275);
  });
  context.fillStyle = '#29434e';
  context.fillText(high.toFixed(2), 0, 45); context.fillText(low.toFixed(2), 0, 250);
  context.fillText(scatter ? '所选资料的数值坐标（仅为原始描述）' : '按表中顺序展示描述值；缺测不绘制为零', 50, 20);
}

function render(result, heading) {
  state.current = result;
  element('summary').replaceChildren();
  for (const [label, value] of Object.entries(result.view.summary)) {
    const card = document.createElement('article');
    const title = document.createElement('span'); title.textContent = label;
    const amount = document.createElement('strong'); amount.textContent = displayValue(value);
    card.append(title, amount); element('summary').append(card);
  }
  element('notes').replaceChildren();
  for (const note of result.view.notes) {
    const paragraph = document.createElement('p'); paragraph.textContent = note; element('notes').append(paragraph);
  }
  const rows = result.view.rows;
  const fields = rows.length ? Object.keys(rows[0]) : [];
  const header = document.createElement('tr');
  for (const field of fields) { const cell = document.createElement('th'); cell.textContent = field; header.append(cell); }
  element('columns').replaceChildren(header); element('rows').replaceChildren();
  for (const row of rows) {
    const line = document.createElement('tr'); line.tabIndex = 0;
    for (const field of fields) { const cell = document.createElement('td'); cell.textContent = displayValue(row[field]); line.append(cell); }
    line.onclick = () => { element('details').textContent = JSON.stringify(row, null, 2); };
    line.onkeydown = event => { if (event.key === 'Enter') line.click(); };
    element('rows').append(line);
  }
  drawChart(result.view.series);
  element('details').textContent = JSON.stringify({revision: result.revision, selection: result.selection,
    history: result.view.history || null}, null, 2);
  message((heading || '已更新描述') + ' · 数据 ' + result.revision.slice(0, 12));
}

// ---------- 反事实研究 ----------

function studySelection() {
  const defaults = state.context.product.analysis_defaults;
  // 只把“规则上仍合格、由分析者手动勾选”的片区作为手动剔除；
  // 规则判定受污染的片区由服务端依据 sharing/events 自行排除，不混入手动名单。
  const excluded_areas = state.areas.filter(area => {
    const box = element('exclude-' + area);
    return box && box.checked && !box.disabled;
  });
  const selection = {
    outcome: element('study-outcome').value || defaults.outcome,
    donor_scope: element('study-scope').value || defaults.donor_scope,
    treatment_week: Number(element('study-treatment').value),
    train_end: Number(element('study-train-end').value),
    eval_start: Number(element('study-eval-start').value),
    eval_end: Number(element('study-eval-end').value),
    placebo_week: Number(element('study-placebo').value),
    rate_per_1000: element('study-rate').checked,
    excluded_areas,
  };
  return {revision: element('revision').value, selection};
}

function drawStudyChart(analysis) {
  const canvas = element('study-chart');
  const context = canvas.getContext('2d');
  context.clearRect(0, 0, canvas.width, canvas.height);
  const outcome = analysis.params.outcome;
  const payload = analysis.outcomes[outcome];
  const actual = payload.actual;
  const counterfactual = payload.counterfactual;
  const treatment = analysis.params.treatment_week;
  const all = [...actual, ...counterfactual].filter(value => typeof value === 'number');
  const low = Math.min(...all);
  const high = Math.max(...all);
  const x = week => 55 + week / (actual.length - 1) * 870;
  const y = value => 250 - (value - low) / (high - low || 1) * 215;
  context.font = '12px sans-serif';
  context.strokeStyle = '#d7e5e2';
  context.beginPath(); context.moveTo(45, 20); context.lineTo(45, 250); context.lineTo(940, 250); context.stroke();
  // 开放周竖线
  context.strokeStyle = '#b06a28'; context.setLineDash([6, 4]);
  context.beginPath(); context.moveTo(x(treatment), 20); context.lineTo(x(treatment), 250); context.stroke();
  context.setLineDash([]);
  context.fillStyle = '#b06a28'; context.fillText('开放周 ' + treatment, x(treatment) + 4, 32);
  const line = (values, color, width) => {
    context.strokeStyle = color; context.lineWidth = width; context.beginPath();
    let started = false;
    values.forEach((value, week) => {
      if (typeof value !== 'number') return;
      const px = x(week), py = y(value);
      if (!started) { context.moveTo(px, py); started = true; } else context.lineTo(px, py);
    });
    context.stroke(); context.lineWidth = 1;
  };
  line(actual, '#143d48', 2);
  line(counterfactual, '#2e9b81', 2);
  context.fillStyle = '#29434e';
  context.fillText('实际（深）与合成反事实（绿），单位：' +
    (analysis.params.rate_per_1000 ? '每千登记人口周接触率' : '周接触次数'), 50, 18);
  context.fillText(high.toFixed(2), 0, 40); context.fillText(low.toFixed(2), 0, 250);
}

function renderEligibility(analysis) {
  const box = element('study-eligibility');
  // 重算前先抓取当前手动勾选状态（合格片区的勾选即手动剔除）
  const previous = {};
  for (const area of state.areas) {
    const old = element('exclude-' + area);
    if (old) previous[area] = {checked: old.checked, disabled: old.disabled};
  }
  state.areas = analysis.eligibility.map(item => item.area);
  box.replaceChildren();
  for (const item of analysis.eligibility) {
    const row = document.createElement('label');
    row.className = 'donor-row' + (item.eligible ? '' : ' donor-off');
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox'; checkbox.id = 'exclude-' + item.area;
    const ruleExcluded = !item.eligible && item.reasons.length > 0 &&
      !item.reasons.includes('manual_excluded');
    checkbox.disabled = ruleExcluded;
    const prior = previous[item.area];
    // 规则排除项保持勾选且禁用；合格片区保留分析者上一次的手动选择
    checkbox.checked = prior ? prior.checked : !item.eligible;
    const text = document.createElement('span');
    text.textContent = item.area + (item.eligible ? ' · 合格对照' : ' · 已排除') +
      (item.reasons.length ? '（' + item.reasons.join('；') + '）' : '（无排除原因）');
    row.append(checkbox, text);
    box.append(row);
  }
}

function renderDiagnostics(analysis) {
  const box = element('study-diagnostics');
  box.replaceChildren();
  const add = (label, value, weak = false) => {
    const row = document.createElement('div');
    row.className = 'diag-row' + (weak ? ' diag-weak' : '');
    const name = document.createElement('span'); name.textContent = label;
    const val = document.createElement('strong'); val.textContent = displayValue(value);
    row.append(name, val); box.append(row);
  };
  const d = analysis.diagnostics;
  add('合格对照片区数', d.donor_count, d.donor_count < 3);
  add('训练期 RMSPE', d.pre_rmspe);
  add('留时段 RMSPE', d.holdout_rmspe);
  add('处理后 RMSPE', d.post_rmspe);
  add('处理后/训练期 RMSPE 比', d.post_to_pre_rmspe_ratio,
    d.post_to_pre_rmspe_ratio !== null && d.post_to_pre_rmspe_ratio < 2);
  add('三渠道恒等', d.identity_passes ? '通过' : '未通过', !d.identity_passes);
  add('安慰剂排位（越小越极端）',
    analysis.placebo.treated_rank === null || analysis.placebo.treated_rank === undefined ?
      '样本不足' : analysis.placebo.treated_rank + ' / ' + (analysis.placebo.donor_count + 1),
    (d.placebo_rank_ratio || 0) > 0.5);
  add('留一地区方向变化数', d.leave_one_out_sign_changes || 0, (d.leave_one_out_sign_changes || 0) > 0);
  const verdict = document.createElement('p');
  verdict.className = analysis.identified ? 'verdict-ok' : 'verdict-bad';
  verdict.textContent = analysis.identified ? '识别结论：诊断通过，可讨论该反事实轨迹（仍为单次个案）' :
    '识别结论：诊断不足，无法识别因果效果，不输出平滑效果线';
  box.append(verdict);
}

function renderStudy(payload, heading) {
  state.study = payload;
  const analysis = payload.analysis;
  const outcome = analysis.params.outcome;
  element('study-status').textContent = (heading || '已更新反事实分析') +
    ' · 数据 ' + payload.revision.slice(0, 12);
  element('study-status').className = analysis.identified ? 'verdict-ok' : 'verdict-bad';
  element('study-conclusion').textContent = analysis.conclusion;
  // 渠道效果卡
  const cards = element('study-cards');
  cards.replaceChildren();
  for (const channel of ['center', 'home', 'total']) {
    const item = analysis.outcomes[channel];
    const card = document.createElement('article');
    const title = document.createElement('span');
    title.textContent = ({center: '中心渠道周均缺口', home: '上门渠道周均缺口', total: '总量周均缺口'})[channel];
    const amount = document.createElement('strong');
    amount.textContent = displayValue(item.post_mean_gap);
    const sub = document.createElement('span');
    sub.textContent = '处理后累计缺口 ' + displayValue(item.post_total_gap) + ' · 共' + item.post_weeks + '周';
    card.append(title, amount, sub); cards.append(card);
  }
  drawStudyChart(analysis);
  renderEligibility(analysis);
  renderDiagnostics(analysis);
  // 权重表：单渠道显示该渠道；total 同时展示 center 与 home（total 本身不拟合权重）
  const weightBody = element('study-weights');
  weightBody.replaceChildren();
  const weightChannels = outcome === 'total' ? ['center', 'home'] : [outcome];
  for (const channel of weightChannels) {
    const weights = analysis.outcomes[channel].weights || {};
    for (const [area, weight] of Object.entries(weights)) {
      const tr = document.createElement('tr');
      for (const value of [channel, area, weight]) {
        const td = document.createElement('td'); td.textContent = displayValue(value); tr.append(td);
      }
      weightBody.append(tr);
    }
  }
  // 安慰剂与留一表
  const placeboBody = element('study-placebo-rows');
  placeboBody.replaceChildren();
  for (const item of analysis.placebo.donor_distribution) {
    const tr = document.createElement('tr');
    for (const value of [item.area, item.pre_rmspe, item.post_mean_gap, item.post_rmspe]) {
      const td = document.createElement('td'); td.textContent = displayValue(value); tr.append(td);
    }
    placeboBody.append(tr);
  }
  const looBody = element('study-loo-rows');
  looBody.replaceChildren();
  for (const item of analysis.leave_one_out) {
    const tr = document.createElement('tr');
    const values = item.available ? [item.dropped, item.pre_rmspe, item.post_mean_gap,
      item.weight_in_full_model === null ? '（总量渠道）' : item.weight_in_full_model]
      : [item.dropped, '不可用', '不可用', '不可用'];
    for (const value of values) {
      const td = document.createElement('td'); td.textContent = displayValue(value); tr.append(td);
    }
    looBody.append(tr);
  }
  element('study-notes').replaceChildren();
  for (const note of analysis.notes.concat(analysis.warnings)) {
    const p = document.createElement('p'); p.textContent = note;
    if (analysis.warnings.includes(note)) p.className = 'warn-line';
    element('study-notes').append(p);
  }
  element('study-details').textContent = JSON.stringify({
    revision: payload.revision, selection: payload.selection, analysis
  }, null, 2);
}

async function refresh() {
  state.context = await request('/api/context');
  renderControls();
  element('archive').replaceChildren();
  for (const notebook of state.context.notebooks) {
    const button = document.createElement('button'); button.textContent = notebook.title + ' #' + notebook.id;
    button.onclick = () => guarded(async () => {
      const saved = await request('/api/notebooks/' + notebook.id);
      element('revision').value = saved.revision;
      for (const [key, value] of Object.entries(saved.selection)) {
        const control = element('select-' + key); if (control) control.value = value;
      }
      render(saved.result, '已读取保存的描述，不随新数据变化');
    });
    element('archive').append(button);
  }
  element('study-archive').replaceChildren();
  for (const study of state.context.studies || []) {
    const button = document.createElement('button');
    button.textContent = study.title + ' #' + study.id;
    button.onclick = () => guarded(async () => {
      const saved = await request('/api/studies/' + study.id);
      element('revision').value = saved.revision;
      renderStudy({revision: saved.revision, selection: saved.selection,
        analysis: saved.result.analysis}, '已读取冻结研究，权重与资格不随新数据变化');
    });
    element('study-archive').append(button);
  }
}

element('inspect').onclick = () => guarded(async () => render(await request('/api/explore', selected())));
element('save').onclick = () => guarded(async () => {
  const saved = await request('/api/notebooks', {...selected(), title: element('title').value});
  await refresh(); render(saved.result, '已保存记录 #' + saved.id);
});
element('raw').onclick = () => guarded(async () => {
  const revision = state.current ? state.current.revision : element('revision').value;
  element('details').textContent = JSON.stringify(await request('/api/data?revision=' + encodeURIComponent(revision)), null, 2);
});
element('upload').onchange = () => guarded(async () => {
  const file = element('upload').files[0]; if (!file) return;
  const data = JSON.parse(await file.text());
  const uploaded = await request('/api/datasets', {label: file.name, data});
  await refresh(); element('revision').value = uploaded.revision;
  render(await request('/api/explore', selected()), '已导入独立数据版本');
});
element('export').onclick = () => guarded(async () => {
  if (!state.current) throw new Error('请先查看一个范围');
  const data = await request('/api/data?revision=' + encodeURIComponent(state.current.revision));
  const blob = new Blob([JSON.stringify({description: state.current, data}, null, 2)], {type: 'application/json'});
  const url = URL.createObjectURL(blob); const link = document.createElement('a');
  link.href = url; link.download = 'analysis-' + state.current.revision.slice(0, 12) + '.json'; link.click();
  URL.revokeObjectURL(url);
});

element('study-run').onclick = () => guarded(async () => {
  const payload = await request('/api/studies', studySelection());
  renderStudy(payload);
});
element('study-save').onclick = () => guarded(async () => {
  const saved = await request('/api/studies', {...studySelection(), title: element('study-title').value});
  await refresh();
  // 保存接口返回的记录嵌套在 result 下，归一化为 renderStudy 需要的 {revision, selection, analysis}
  renderStudy({revision: saved.revision, selection: saved.selection,
    analysis: saved.result.analysis}, '已冻结研究 #' + saved.id);
});
element('study-export').onclick = () => guarded(async () => {
  if (!state.study) throw new Error('请先运行一次反事实分析');
  const blob = new Blob([JSON.stringify(state.study, null, 2)], {type: 'application/json'});
  const url = URL.createObjectURL(blob); const link = document.createElement('a');
  link.href = url; link.download = 'study-' + state.study.revision.slice(0, 12) + '.json'; link.click();
  URL.revokeObjectURL(url);
});
guarded(async () => {
  await refresh();
  render(await request('/api/explore', selected()));
  const defaults = state.context.product.analysis_defaults;
  element('study-outcome').value = defaults.outcome;
  element('study-scope').value = defaults.donor_scope;
  element('study-treatment').value = defaults.treatment_week;
  element('study-train-end').value = defaults.train_end;
  element('study-eval-start').value = defaults.eval_start;
  element('study-eval-end').value = defaults.eval_end;
  element('study-placebo').value = defaults.placebo_week;
  renderStudy(await request('/api/studies', studySelection()));
});
