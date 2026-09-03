import { apiCall as authApiCall } from './auth.js';
import { showLoading, hideLoading, showToast } from './utils.js';

let comparisonState = null;
let analysisPollTimer = null;

const escapeHtml = value => String(value ?? '')
  .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
  .replaceAll('"', '&quot;').replaceAll("'", '&#039;');

const sameReference = (left, right) => Boolean(left && right
  && left.document_id === right.document_id
  && left.revision_id === right.revision_id
  && Number(left.page_number) === Number(right.page_number));

function authenticatedUrl(path) {
  if (!path) return '';
  const normalized = path.startsWith('/ai/api/')
    ? path
    : path.startsWith('/') ? `/ai/api${path}` : path;
  const token = localStorage.getItem('loginToken');
  if (!token) return normalized;
  return `${normalized}${normalized.includes('?') ? '&' : '?'}token=${encodeURIComponent(token)}`;
}

function pageLabel(page) {
  if (!page) return '未選択';
  const extras = [page.floor_code, page.plan_variant ? `案${page.plan_variant}` : null]
    .filter(Boolean).join(' / ');
  return `${page.file_name} — ページ${page.page_number}${extras ? `（${extras}）` : ''}`;
}

function pageSourceLabel(page) {
  const source = { RULE: 'ファイル名・本文ルール', VLM: 'VLM候補', USER: 'ユーザー確定', MIGRATION: '移行値' }[page?.source] || page?.source || '不明';
  return `${source} / ${page?.confirmed ? '確認済み' : '要確認'} / 信頼度 ${Math.round(Number(page?.confidence || 0) * 100)}%`;
}

function pageOptionList(side) {
  const candidates = side === 'before'
    ? comparisonState.data.before_candidates
    : comparisonState.data.after_candidates;
  const selected = comparisonState.data.pair?.[side];
  if (!candidates.length) return '<option value="">候補がありません</option>';
  return candidates.map((page, index) =>
    `<option value="${index}"${sameReference(page, selected) ? ' selected' : ''}>${escapeHtml(pageLabel(page))}</option>`
  ).join('');
}

function renderPageCard(side, title) {
  const page = comparisonState.data.pair?.[side];
  return `<section class="case-comparison-side ${side}">
    <div class="case-comparison-side-heading"><span>${escapeHtml(title)}</span><small>${side === 'before' ? '現況' : '提案（完成実績ではありません）'}</small></div>
    <label class="case-comparison-page-select">表示する図面
      <select class="form-input" onchange="window.caseComparisonModule.selectComparisonPage('${side}', this.value)">${pageOptionList(side)}</select>
    </label>
    ${page ? `<button type="button" class="case-comparison-image-button" onclick="window.caseComparisonModule.openComparisonImage('${side}')">
      <img src="${escapeHtml(authenticatedUrl(page.image_url))}" alt="${escapeHtml(pageLabel(page))}" loading="lazy">
      <span><i class="fas fa-expand"></i> 拡大表示</span>
    </button>
    <div class="case-comparison-page-meta"><strong>${escapeHtml(pageLabel(page))}</strong><small>${escapeHtml(pageSourceLabel(page))}</small></div>`
      : `<div class="case-comparison-missing"><i class="fas fa-triangle-exclamation"></i>${escapeHtml(comparisonState.data.pair?.missing_reason || `${title}の候補がありません`)}</div>`}
  </section>`;
}

const contentKindLabels = {
  FLOOR_PLAN: '平面図', SITE_PLAN: '配置図', ELEVATION: '立面図', AREA_TABLE: '面積表',
  PERSPECTIVE: 'パース', PHOTO: '写真', OTHER: 'その他'
};
const phaseLabels = { EXISTING: '現況', PROPOSED: '提案', COMPLETED: '完成後', UNKNOWN: '不明' };

function renderClassificationEditor() {
  const pages = comparisonState.data.all_pages || [];
  const index = Math.min(comparisonState.editPageIndex || 0, Math.max(0, pages.length - 1));
  comparisonState.editPageIndex = index;
  const page = pages[index];
  if (!page) return '<p class="case-comparison-empty">分類対象のページがありません。</p>';
  const option = (values, selected, labels) => values.map(value =>
    `<option value="${value}"${value === selected ? ' selected' : ''}>${escapeHtml(labels[value])}</option>`
  ).join('');
  return `<div class="case-page-classification-editor">
    <label>対象ページ<select id="caseClassificationPage" class="form-input" onchange="window.caseComparisonModule.selectClassificationPage(this.value)">
      ${pages.map((item, pageIndex) => `<option value="${pageIndex}"${pageIndex === index ? ' selected' : ''}>${escapeHtml(pageLabel(item))}${item.confirmed ? '' : '（要確認）'}</option>`).join('')}
    </select></label>
    <div class="case-page-classification-grid">
      <label>ページ種別<select id="caseClassificationKind" class="form-input">${option(Object.keys(contentKindLabels), page.content_kind, contentKindLabels)}</select></label>
      <label>区分<select id="caseClassificationPhase" class="form-input">${option(Object.keys(phaseLabels), page.phase, phaseLabels)}</select></label>
      <label>階<input id="caseClassificationFloor" class="form-input" value="${escapeHtml(page.floor_code || '')}" placeholder="例: 1F"></label>
      <label>プラン案<input id="caseClassificationVariant" class="form-input" value="${escapeHtml(page.plan_variant || '')}" placeholder="例: A"></label>
    </div>
    <div class="case-page-classification-actions">
      <span>${escapeHtml(pageSourceLabel(page))}</span>
      <button type="button" class="apex-button-secondary" onclick="window.caseComparisonModule.savePageClassification()"><i class="fas fa-check"></i> 分類を確認して保存</button>
    </div>
  </div>`;
}

function factValue(code, phase = 'COMMON', field = 'value_text') {
  const item = (comparisonState.data.facts || []).find(fact => fact.fact_code === code && fact.phase === phase);
  return item?.[field] ?? '';
}

function renderBuildingFacts() {
  const textField = (id, label, code, phase = 'COMMON', placeholder = '') =>
    `<label>${label}<input id="${id}" class="form-input" value="${escapeHtml(factValue(code, phase))}" placeholder="${escapeHtml(placeholder)}"></label>`;
  return `<div class="case-building-facts-grid">
    ${textField('caseFactBuildingType', '建物種別', 'BUILDING_TYPE', 'COMMON', '戸建て / マンション')}
    ${textField('caseFactStructure', '構造', 'STRUCTURE', 'COMMON', '木造 / RC造')}
    ${textField('caseFactUse', '用途', 'USE', 'COMMON', '住宅')}
    ${textField('caseFactAreaType', '面積種別', 'AREA_TYPE', 'COMMON', '延床面積 / 専有面積')}
    <label>面積<input id="caseFactAreaValue" type="number" min="0" step="0.01" class="form-input" value="${escapeHtml(factValue('AREA_VALUE', 'COMMON', 'value_number'))}"></label>
    <label>単位<select id="caseFactAreaUnit" class="form-input"><option value="㎡"${factValue('AREA_VALUE', 'COMMON', 'unit') !== '坪' ? ' selected' : ''}>㎡</option><option value="坪"${factValue('AREA_VALUE', 'COMMON', 'unit') === '坪' ? ' selected' : ''}>坪</option></select></label>
    ${textField('caseFactExistingLayout', '現況間取り', 'LAYOUT', 'EXISTING', '例: 3LDK')}
    ${textField('caseFactProposedLayout', '提案間取り', 'LAYOUT', 'PROPOSED', '例: 2LDK')}
  </div>
  <div class="case-building-facts-actions"><small>確定して保存した値は、再解析で上書きされず検索の厳密な絞り込みに使われます。</small><button type="button" class="apex-button-secondary" onclick="window.caseComparisonModule.saveBuildingFacts()"><i class="fas fa-save"></i> 建物条件を保存</button></div>`;
}

function renderAnalysisResult() {
  const analysis = comparisonState.analysis;
  if (!analysis) return '<p class="case-analysis-empty">必要なときだけ「AIで変更点を分析」を押してください。ローカルGPUは使用しません。</p>';
  if (analysis.status === 'PENDING' || analysis.status === 'RUNNING') {
    return `<div class="case-analysis-running"><i class="fas fa-spinner fa-spin"></i><strong>分析中</strong><span>現況図と提案図、保存済みテキストを照合しています。</span></div>`;
  }
  if (analysis.status === 'FAILED') {
    return `<div class="case-analysis-failed"><strong>分析に失敗しました</strong><span>${escapeHtml(analysis.error_summary || '接続設定を確認して再試行してください。')}</span></div>`;
  }
  const result = analysis.result || {};
  const items = Array.isArray(result.change_items) ? result.change_items : [];
  return `<div class="case-analysis-result">
    ${analysis.cached ? '<span class="case-analysis-cache">保存済み分析を再利用</span>' : ''}
    <p>${escapeHtml(result.summary || '要約はありません。')}</p>
    ${items.map(item => `<article><strong>${escapeHtml(item.category || '変更点')}</strong><div><b>現況:</b> ${escapeHtml(item.before || '確認できず')}</div><div><b>提案:</b> ${escapeHtml(item.after || '確認できず')}</div><small>根拠: ${escapeHtml([item.evidence_before, item.evidence_after].filter(Boolean).join(' / ') || '記載なし')} / 信頼度 ${Math.round(Number(item.confidence || 0) * 100)}%</small>${item.uncertainty ? `<em>${escapeHtml(item.uncertainty)}</em>` : ''}</article>`).join('') || '<p>断定できる変更点はありませんでした。</p>'}
    ${(result.unchanged_or_unclear || []).length ? `<details><summary>変更不明・確認できない項目</summary><ul>${result.unchanged_or_unclear.map(item => `<li>${escapeHtml(item)}</li>`).join('')}</ul></details>` : ''}
    <small>提案図の内容は完成実績ではなく、提案内容として表示しています。</small>
  </div>`;
}

function ensureModal() {
  let modal = document.getElementById('caseComparisonModal');
  if (modal) return modal;
  modal = document.createElement('div');
  modal.id = 'caseComparisonModal';
  modal.className = 'case-comparison-modal';
  modal.setAttribute('role', 'dialog');
  modal.setAttribute('aria-modal', 'true');
  modal.addEventListener('click', event => {
    if (event.target === modal) closeCaseComparison();
  });
  document.body.appendChild(modal);
  return modal;
}

function renderComparisonModal() {
  if (!comparisonState?.data) return;
  const modal = ensureModal();
  const previousScroller = modal.querySelector('.case-comparison-scroll');
  const previousScrollPosition = previousScroller
    ? { top: previousScroller.scrollTop, left: previousScroller.scrollLeft }
    : null;
  const data = comparisonState.data;
  const complete = Boolean(data.pair?.before && data.pair?.after);
  modal.innerHTML = `<div class="case-comparison-dialog">
    <header><div><h2>現況図と提案図を比較</h2><p>${escapeHtml(data.label)}</p></div><button type="button" onclick="window.caseComparisonModule.closeCaseComparison()" aria-label="閉じる"><i class="fas fa-times"></i></button></header>
    <div class="case-comparison-scroll">
      <div class="case-comparison-notice"><i class="fas fa-circle-info"></i><span>案件グループ内の確認済みメタ情報から組み合わせています。左右の候補は変更できます。</span><button type="button" onclick="window.caseComparisonModule.refreshCaseComparison()"><i class="fas fa-sync"></i> 再判定</button></div>
      <div class="case-comparison-grid">${renderPageCard('before', 'Before')}${renderPageCard('after', 'After')}</div>
      <div class="case-comparison-pair-actions"><span>${data.pair?.source === 'USER' ? 'ユーザーが選択した組み合わせ' : 'ルールで自動選択した組み合わせ'}</span><button type="button" class="apex-button" ${complete ? '' : 'disabled'} onclick="window.caseComparisonModule.saveComparisonPair()"><i class="fas fa-save"></i> この組み合わせを保存</button></div>
      <details class="case-comparison-details" ${comparisonState.classificationOpen ? 'open' : ''} ontoggle="window.caseComparisonModule.setSectionOpen('classification', this.open)"><summary>ページ分類を確認・修正 <small>未確定候補を含む ${data.all_pages?.length || 0}ページ</small></summary>${renderClassificationEditor()}</details>
      <details class="case-comparison-details" ${comparisonState.factsOpen ? 'open' : ''} ontoggle="window.caseComparisonModule.setSectionOpen('facts', this.open)"><summary>案件の建物条件を確認・修正 <small>検索の絞り込みに使用</small></summary>${renderBuildingFacts()}</details>
      <section class="case-comparison-analysis"><div class="case-comparison-analysis-heading"><div><h3>変更点のAI分析</h3><p>ボタンを押した時だけOCI Enterprise AIで分析します。</p></div><div><button type="button" class="apex-button" ${complete && !['PENDING', 'RUNNING'].includes(comparisonState.analysis?.status) ? '' : 'disabled'} onclick="window.caseComparisonModule.runCaseComparisonAnalysis(false)"><i class="fas fa-wand-magic-sparkles"></i> AIで変更点を分析</button>${comparisonState.analysis?.status === 'COMPLETED' || comparisonState.analysis?.status === 'FAILED' ? '<button type="button" class="apex-button-secondary" onclick="window.caseComparisonModule.runCaseComparisonAnalysis(true)"><i class="fas fa-rotate"></i> 再分析</button>' : ''}</div></div>${renderAnalysisResult()}</section>
    </div>
  </div>`;
  modal.hidden = false;
  if (previousScrollPosition) {
    const nextScroller = modal.querySelector('.case-comparison-scroll');
    if (nextScroller) {
      nextScroller.scrollTop = previousScrollPosition.top;
      nextScroller.scrollLeft = previousScrollPosition.left;
    }
  }
  document.body.classList.add('case-comparison-open');
}

async function loadComparison(refresh = false) {
  const group = window._searchResultsData?.groups?.[comparisonState.groupIndex];
  if (!group?.document_set_id) throw new Error('案件グループが見つかりません');
  comparisonState.data = await authApiCall(`/ai/api/document-library/document-sets/${encodeURIComponent(group.document_set_id)}/comparison${refresh ? '?refresh=true' : ''}`);
  renderComparisonModal();
}

export async function showCaseComparison(groupIndex) {
  comparisonState = { groupIndex: Number(groupIndex), data: null, editPageIndex: 0, analysis: null, classificationOpen: false, factsOpen: false };
  try {
    showLoading('現況図と提案図の組み合わせを確認しています...');
    await loadComparison(false);
    if (!comparisonState.data?.all_pages?.length) await loadComparison(true);
  } catch (error) {
    comparisonState = null;
    showToast(`比較画面を開けませんでした: ${error.message}`, 'error');
  } finally {
    hideLoading();
  }
}

export function closeCaseComparison() {
  if (analysisPollTimer) clearTimeout(analysisPollTimer);
  analysisPollTimer = null;
  document.getElementById('caseComparisonModal')?.remove();
  document.body.classList.remove('case-comparison-open');
  comparisonState = null;
}

export function selectComparisonPage(side, indexValue) {
  if (!comparisonState?.data || !['before', 'after'].includes(side)) return;
  const candidates = side === 'before' ? comparisonState.data.before_candidates : comparisonState.data.after_candidates;
  const selected = candidates[Number(indexValue)] || null;
  comparisonState.data.pair[side] = selected;
  comparisonState.data.pair.complete = Boolean(comparisonState.data.pair.before && comparisonState.data.pair.after);
  comparisonState.analysis = null;
  renderComparisonModal();
}

export function openComparisonImage(side) {
  const page = comparisonState?.data?.pair?.[side];
  if (!page?.image_url) return;
  window.open(authenticatedUrl(page.image_url), '_blank', 'noopener');
}

export async function saveComparisonPair({ quiet = false } = {}) {
  const pair = comparisonState?.data?.pair;
  if (!pair?.before || !pair?.after) {
    showToast('現況図と提案図を両方選択してください', 'error');
    return null;
  }
  const payload = {
    before: { document_id: pair.before.document_id, revision_id: pair.before.revision_id, page_number: pair.before.page_number },
    after: { document_id: pair.after.document_id, revision_id: pair.after.revision_id, page_number: pair.after.page_number },
    floor_code: pair.before.floor_code || pair.after.floor_code || null,
    plan_variant: pair.after.plan_variant || pair.before.plan_variant || null
  };
  try {
    const documentSetId = comparisonState.data.document_set_id;
    comparisonState.data = await authApiCall(`/ai/api/document-library/document-sets/${encodeURIComponent(documentSetId)}/comparison`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload)
    });
    if (!quiet) showToast('比較する組み合わせを保存しました', 'success');
    renderComparisonModal();
    return payload;
  } catch (error) {
    if (quiet) throw error;
    showToast(`比較する組み合わせを保存できませんでした: ${error.message}`, 'error');
    return null;
  }
}

export function setSectionOpen(section, open) {
  if (!comparisonState) return;
  if (section === 'classification') comparisonState.classificationOpen = Boolean(open);
  if (section === 'facts') comparisonState.factsOpen = Boolean(open);
}

export function selectClassificationPage(indexValue) {
  comparisonState.editPageIndex = Number(indexValue) || 0;
  comparisonState.classificationOpen = true;
  renderComparisonModal();
}

export async function savePageClassification() {
  const page = comparisonState?.data?.all_pages?.[comparisonState.editPageIndex];
  if (!page) return;
  try {
    showLoading('ページ分類を保存しています...');
    await authApiCall(`/ai/api/document-library/page-classifications/${encodeURIComponent(page.document_id)}/${encodeURIComponent(page.revision_id)}/${page.page_number}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({
        content_kind: document.getElementById('caseClassificationKind').value,
        phase: document.getElementById('caseClassificationPhase').value,
        floor_code: document.getElementById('caseClassificationFloor').value.trim() || null,
        plan_variant: document.getElementById('caseClassificationVariant').value.trim() || null,
        confirmed: true
      })
    });
    comparisonState.classificationOpen = true;
    await loadComparison(false);
    showToast('ページ分類を確定しました', 'success');
  } catch (error) {
    showToast(`ページ分類を保存できませんでした: ${error.message}`, 'error');
  } finally { hideLoading(); }
}

export async function saveBuildingFacts() {
  const items = [];
  const addText = (fact_code, phase, id) => {
    const value_text = document.getElementById(id)?.value.trim();
    if (value_text) items.push({ fact_code, phase, value_text, confirmed: true });
  };
  addText('BUILDING_TYPE', 'COMMON', 'caseFactBuildingType');
  addText('STRUCTURE', 'COMMON', 'caseFactStructure');
  addText('USE', 'COMMON', 'caseFactUse');
  addText('AREA_TYPE', 'COMMON', 'caseFactAreaType');
  addText('LAYOUT', 'EXISTING', 'caseFactExistingLayout');
  addText('LAYOUT', 'PROPOSED', 'caseFactProposedLayout');
  const areaInput = document.getElementById('caseFactAreaValue')?.value.trim() || '';
  const area = areaInput ? Number(areaInput) : null;
  if (Number.isFinite(area) && area >= 0) items.push({ fact_code: 'AREA_VALUE', phase: 'COMMON', value_number: area, unit: document.getElementById('caseFactAreaUnit')?.value || '㎡', confirmed: true });
  try {
    const id = comparisonState.data.document_set_id;
    comparisonState.data.facts = await authApiCall(`/ai/api/document-library/document-sets/${encodeURIComponent(id)}/building-facts`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ items })
    });
    comparisonState.factsOpen = true;
    renderComparisonModal();
    showToast(items.length ? '建物条件を保存しました' : '建物条件をすべて解除しました', 'success');
  } catch (error) { showToast(`建物条件を保存できませんでした: ${error.message}`, 'error'); }
}

export async function refreshCaseComparison() {
  try {
    showLoading('保存済みテキストからページ分類を再判定しています...');
    await loadComparison(true);
    showToast('自動判定を更新しました。ユーザー確定済みの値は保持されています', 'success');
  } catch (error) { showToast(`再判定できませんでした: ${error.message}`, 'error'); }
  finally { hideLoading(); }
}

async function pollAnalysis(analysisId) {
  analysisPollTimer = null;
  if (!comparisonState) return;
  try {
    const analysis = await authApiCall(`/ai/api/document-library/comparison-analyses/${encodeURIComponent(analysisId)}`);
    if (!comparisonState) return;
    comparisonState.analysis = analysis;
    renderComparisonModal();
    if (analysis.status === 'PENDING' || analysis.status === 'RUNNING') {
      analysisPollTimer = setTimeout(() => pollAnalysis(analysisId), 1800);
    }
  } catch (error) {
    if (!comparisonState) return;
    comparisonState.analysis = { status: 'FAILED', error_summary: error.message };
    renderComparisonModal();
  }
}

export async function runCaseComparisonAnalysis(force = false) {
  if (analysisPollTimer) clearTimeout(analysisPollTimer);
  analysisPollTimer = null;
  try {
    const selection = await saveComparisonPair({ quiet: true });
    if (!selection) return;
    comparisonState.analysis = { status: 'PENDING' };
    renderComparisonModal();
    const id = comparisonState.data.document_set_id;
    const analysis = await authApiCall(`/ai/api/document-library/document-sets/${encodeURIComponent(id)}/comparison-analyses`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...selection, force: Boolean(force) }), timeout: 30000
    });
    comparisonState.analysis = analysis;
    renderComparisonModal();
    if (analysis.status === 'PENDING' || analysis.status === 'RUNNING') await pollAnalysis(analysis.analysis_id);
  } catch (error) {
    if (comparisonState) {
      comparisonState.analysis = { status: 'FAILED', error_summary: error.message };
      renderComparisonModal();
    }
  }
}

window.caseComparisonModule = {
  showCaseComparison, closeCaseComparison, selectComparisonPage, openComparisonImage,
  saveComparisonPair, setSectionOpen, selectClassificationPage, savePageClassification,
  saveBuildingFacts, refreshCaseComparison, runCaseComparisonAnalysis
};
