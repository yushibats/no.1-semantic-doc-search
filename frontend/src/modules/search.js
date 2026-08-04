/**
 * 検索モジュール
 * 
 * セマンティック検索機能を担当（テキスト検索・画像検索）
 * 
 * 主な機能:
 * - テキストベースのセマンティック検索
 * - 画像ベースの類似画像検索
 * - 検索結果の表示と管理
 * - ファイルダウンロード機能
 * 
 * ネットワーク通信:
 * - バックエンドAPIとのHTTPS通信
 * - ファイルアップロード（FormData）
 * - 認証トークン付きリクエスト
 * - エラーハンドリングとユーザー通知
 */

import { apiCall as authApiCall, fetchWithAuth as authFetchWithAuth } from './auth.js';
import {
  showLoading as utilsShowLoading,
  hideLoading as utilsHideLoading,
  showToast as utilsShowToast,
  showImageModal as utilsShowImageModal,
  showTextPreviewModal as utilsShowTextPreviewModal
} from './utils.js';

// 検索画像の状態管理
let selectedSearchImage = null;
let currentSearchType = 'text'; // 'text' or 'image'
let dynamicFieldDefinitions = [];
let dynamicFiltersLoaded = false;
let v2RetrievalActive = false;
let currentSearchController = null;
let searchCancelled = false;
let searchProgressTimer = null;
const searchProgress = { startedAt: 0, state: {}, steps: new Map() };
let searchConcepts = [];
let searchConceptSettings = { initial_display_limit: 8 };
let searchConceptFilter = '';
let searchConceptSuggestionIds = null;
let searchConceptSuggestionTimer = null;
let searchConceptSuggestionSerial = 0;
const searchConceptSuggestionCache = new Map();
const selectedSearchConceptIds = new Set();
const expandedConceptCategories = new Set();
const defaultRetrievalModes = [
  'visual_vector',
  'oracle_text',
  'text_vector',
  'vlm_text',
  'vlm_vector'
];
const vectorRetrievalModes = new Set(['text_vector', 'vlm_vector', 'visual_vector']);

function flattenSearchFolders(nodes, depth = 0, result = []) {
  for (const node of nodes || []) {
    result.push({ ...node, uiDepth: depth });
    flattenSearchFolders(node.children, depth + 1, result);
  }
  return result;
}

function renderMetadataSearchFilters(data) {
  const panel = document.getElementById('metadataSearchFilters');
  if (panel) panel.hidden = !data.document_library_ready;
  const folder = document.getElementById('searchFolderId');
  if (folder) {
    const current = folder.value;
    folder.innerHTML = '<option value="">すべてのフォルダ</option>' + flattenSearchFolders(data.folders)
      .map(item => `<option value="${escapeHtml(item.folder_id)}">${'　'.repeat(item.uiDepth)}${escapeHtml(item.name)}</option>`).join('');
    folder.value = current;
  }
  const customerList = document.getElementById('searchCustomerSuggestions');
  if (customerList) customerList.innerHTML = (data.customer_suggestions || [])
    .map(item => `<option value="${escapeHtml(item.value)}"></option>`).join('');
  const tagRoot = document.getElementById('searchTagFilters');
  if (tagRoot) {
    const groupById = new Map((data.tag_groups || []).map(group => [group.group_id, group]));
    tagRoot.innerHTML = (data.tags || []).filter(tag => tag.active).map(tag =>
      `<label class="metadata-tag-option"><input type="checkbox" data-search-tag value="${escapeHtml(tag.tag_id)}"> ${escapeHtml(groupById.get(tag.group_id)?.name || '')}: ${escapeHtml(tag.name)}</label>`
    ).join('') || '<span class="text-xs text-gray-500">タグは未設定です</span>';
  }
  const bounds = data.date_bounds || {};
  const yearFrom = document.getElementById('searchYearFrom');
  const yearTo = document.getElementById('searchYearTo');
  if (yearFrom && bounds.min_year) yearFrom.placeholder = String(bounds.min_year);
  if (yearTo && bounds.max_year) yearTo.placeholder = String(bounds.max_year);
  searchConcepts = (data.search_concepts || []).filter(concept => concept.status === 'ACTIVE');
  searchConceptSettings = data.search_concept_settings || searchConceptSettings;
  searchConceptSuggestionIds = null;
  searchConceptSuggestionCache.clear();
  renderSearchConcepts();
}

const conceptFacetLabels = {
  BEFORE: '現況の課題（Before）',
  AFTER: '実現したいこと（After）',
  OTHER: 'その他の特徴'
};

function selectedConceptPayload() {
  return {
    selected_concept_ids: [...selectedSearchConceptIds],
    concept_mode: document.getElementById('searchConceptRequireAll')?.checked ? 'REQUIRE_ALL' : 'BOOST'
  };
}

function renderSearchConcepts() {
  const panel = document.getElementById('searchConceptPanel');
  const root = document.getElementById('searchConceptFacets');
  const selectedRoot = document.getElementById('selectedSearchConcepts');
  if (!panel || !root || !selectedRoot) return;
  panel.hidden = !searchConceptSettings.enabled || !searchConcepts.length;
  if (panel.hidden) return;

  const selected = searchConcepts.filter(item => selectedSearchConceptIds.has(item.concept_id));
  const summaryCount = document.getElementById('searchConceptSummaryCount');
  if (summaryCount) {
    summaryCount.textContent = selected.length
      ? selected.length + '件選択中'
      : searchConcepts.length + '件から選択';
  }
  selectedRoot.hidden = !selected.length;
  selectedRoot.innerHTML = selected.length
    ? `<strong>選択中:</strong>${selected.map(item => `<button type="button" class="search-concept-chip selected" onclick="window.searchModule.toggleSearchConcept('${escapeHtml(item.concept_id)}')" aria-pressed="true">${escapeHtml(item.display_label)} <i class="fas fa-times"></i></button>`).join('')}`
    : '';

  const needle = searchConceptFilter.trim().toLocaleLowerCase('ja-JP');
  const localFiltered = searchConcepts.filter(item => !needle
    || `${item.display_label} ${item.category_name}`.toLocaleLowerCase('ja-JP').includes(needle));
  const suggestionRank = new Map(
    (searchConceptSuggestionIds || []).map((conceptId, index) => [conceptId, index])
  );
  const filtered = needle && Array.isArray(searchConceptSuggestionIds)
    ? searchConcepts
      .filter(item => suggestionRank.has(item.concept_id))
      .sort((a, b) => suggestionRank.get(a.concept_id) - suggestionRank.get(b.concept_id))
    : localFiltered;
  const byFacet = new Map();
  filtered.forEach(item => {
    const facet = item.facet || 'OTHER';
    const categories = byFacet.get(facet) || new Map();
    const key = `${facet}:${item.category_code}`;
    const category = categories.get(key) || { key, name: item.category_name, items: [] };
    category.items.push(item);
    categories.set(key, category);
    byFacet.set(facet, categories);
  });
  const initialLimit = Number(searchConceptSettings.initial_display_limit) || 8;
  root.innerHTML = ['BEFORE', 'AFTER', 'OTHER'].flatMap(facet => {
    const categories = byFacet.get(facet);
    if (!categories?.size) return [];
    return [`<section class="search-concept-facet" data-facet="${facet}"><h4>${conceptFacetLabels[facet]}</h4>${[...categories.values()].map(category => {
      category.items.sort((a, b) => {
        if (suggestionRank.size) {
          const rankA = suggestionRank.get(a.concept_id) ?? Number.MAX_SAFE_INTEGER;
          const rankB = suggestionRank.get(b.concept_id) ?? Number.MAX_SAFE_INTEGER;
          if (rankA !== rankB) return rankA - rankB;
        }
        return (b.support_set_count || 0) - (a.support_set_count || 0)
          || a.display_label.localeCompare(b.display_label, 'ja');
      });
      const expanded = expandedConceptCategories.has(category.key) || Boolean(needle);
      const visible = expanded ? category.items : category.items.slice(0, initialLimit);
      return `<div class="search-concept-category"><h5>${escapeHtml(category.name)}</h5><div class="search-concept-chips">${visible.map(item => {
        const isSelected = selectedSearchConceptIds.has(item.concept_id);
        return `<button type="button" class="search-concept-chip${isSelected ? ' selected' : ''}" aria-pressed="${isSelected}" onclick="window.searchModule.toggleSearchConcept('${escapeHtml(item.concept_id)}')">${escapeHtml(item.display_label)}</button>`;
      }).join('')}</div>${!expanded && category.items.length > initialLimit ? `<button type="button" class="search-concept-more" onclick="window.searchModule.expandSearchConceptCategory('${escapeHtml(category.key)}')">ほか${category.items.length - initialLimit}件を表示</button>` : ''}</div>`;
    }).join('')}</section>`];
  }).join('') || '<div class="search-concept-empty">一致する候補はありません</div>';
}

export function toggleSearchConcept(conceptId) {
  if (selectedSearchConceptIds.has(conceptId)) selectedSearchConceptIds.delete(conceptId);
  else selectedSearchConceptIds.add(conceptId);
  renderSearchConcepts();
}

function setSearchConceptSuggestionStatus(message = '', state = '') {
  const status = document.getElementById('searchConceptQueryStatus');
  if (!status) return;
  status.textContent = message;
  status.hidden = !message;
  status.dataset.state = state;
}

export function filterSearchConcepts(value = '') {
  searchConceptFilter = value;
  searchConceptSuggestionIds = null;
  const normalized = value.trim();
  const requestSerial = ++searchConceptSuggestionSerial;
  if (searchConceptSuggestionTimer) {
    clearTimeout(searchConceptSuggestionTimer);
    searchConceptSuggestionTimer = null;
  }

  if (normalized.length < 2) {
    setSearchConceptSuggestionStatus(
      normalized ? '2文字以上入力すると、文章の意味から関連候補を探します' : ''
    );
    renderSearchConcepts();
    return;
  }

  const cached = searchConceptSuggestionCache.get(normalized);
  if (cached) {
    searchConceptSuggestionIds = cached.concept_ids;
    setSearchConceptSuggestionStatus(cached.message, cached.source);
    renderSearchConcepts();
    return;
  }

  setSearchConceptSuggestionStatus('入力内容に関連する検索条件を探しています...', 'LOADING');
  renderSearchConcepts();
  searchConceptSuggestionTimer = setTimeout(async () => {
    try {
      const result = await authApiCall('/ai/api/search/v2/concepts/suggest', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query: normalized, limit: 24 }),
        timeout: 30000
      });
      if (requestSerial !== searchConceptSuggestionSerial) return;
      const conceptIds = Array.isArray(result.concept_ids) ? result.concept_ids : [];
      const cachedResult = {
        concept_ids: conceptIds,
        source: result.source || 'AI',
        message: result.message || `${conceptIds.length}件の関連候補を表示しています`
      };
      if (searchConceptSuggestionCache.size >= 50) searchConceptSuggestionCache.clear();
      searchConceptSuggestionCache.set(normalized, cachedResult);
      searchConceptSuggestionIds = conceptIds;
      setSearchConceptSuggestionStatus(cachedResult.message, cachedResult.source);
      renderSearchConcepts();
    } catch (error) {
      if (requestSerial !== searchConceptSuggestionSerial) return;
      searchConceptSuggestionIds = null;
      setSearchConceptSuggestionStatus(
        'AIによる絞り込みを利用できないため、文字の部分一致を表示しています',
        'ERROR'
      );
      renderSearchConcepts();
    }
  }, 500);
}

export function expandSearchConceptCategory(categoryKey) {
  expandedConceptCategories.add(categoryKey);
  renderSearchConcepts();
}

export function clearSearchConcepts() {
  selectedSearchConceptIds.clear();
  renderSearchConcepts();
}

function collectMetadataFilters() {
  const folderId = document.getElementById('searchFolderId')?.value || '';
  const customer = document.getElementById('searchCustomerName')?.value.trim() || '';
  const yearFrom = Number(document.getElementById('searchYearFrom')?.value) || null;
  const yearTo = Number(document.getElementById('searchYearTo')?.value) || null;
  const month = Number(document.getElementById('searchMonth')?.value) || null;
  return {
    folder: folderId ? {
      folder_id: folderId,
      include_descendants: Boolean(document.getElementById('searchIncludeDescendants')?.checked)
    } : null,
    tags: {
      all_of: [...document.querySelectorAll('[data-search-tag]:checked')].map(input => input.value),
      any_of: [],
      none_of: []
    },
    customer: customer ? {
      values: [customer],
      match: document.getElementById('searchCustomerMatch')?.value || 'normalized_exact'
    } : null,
    document_year_from: yearFrom,
    document_year_to: yearTo,
    document_months: month ? [month] : []
  };
}

const stepLabels = {
  initialization: '検索準備',
  query_variants: '検索バリエーション生成',
  keyword_plan: '検索キーワード生成',
  embedding: 'ベクトル作成',
  retrieval: '候補取得',
  candidate_merge: '候補統合',
  rerank: '再ランキング',
  llm_judge: 'LLM最終判定',
  verify: 'VLM確認',
  format_results: '結果整形'
};

const escapeHtml = (value) => String(value ?? '')
  .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
  .replaceAll('"', '&quot;').replaceAll("'", '&#039;');

const displayFilename = (fileResult) => {
  const fallback = fileResult.object_name?.split('/').pop() || '';
  return (fileResult.original_filename || fallback).replace(/^\d{8}_\d{6}_[a-f0-9]{8}_/i, '');
};

function setRetrievalModeError(message = '', { focus = false } = {}) {
  const fieldset = document.getElementById('searchRetrievalModes');
  const error = document.getElementById('searchRetrievalModesError');
  if (fieldset) {
    if (message) fieldset.setAttribute('aria-invalid', 'true');
    else fieldset.removeAttribute('aria-invalid');
  }
  if (error) {
    error.textContent = message;
    error.hidden = !message;
  }
  if (message && focus) {
    const settings = document.getElementById('searchAdvancedSettings');
    if (settings) settings.open = true;
    document.querySelector('input[name="retrievalMode"]:not(:disabled)')?.focus();
  }
}

function updateRetrievalModeOptions(options) {
  if (!Array.isArray(options)) return;
  const inputs = [...document.querySelectorAll('input[name="retrievalMode"]')];
  if (!inputs.length) return;
  const optionByValue = new Map(options.map(option => [option.value, option]));
  inputs.forEach(input => {
    const option = optionByValue.get(input.value);
    if (!option) return;
    const label = input.closest('.search-retrieval-mode-option');
    const title = label?.querySelector('strong');
    const description = label?.querySelector('[data-mode-description]');
    const status = label?.querySelector('[data-mode-status]');
    const available = Boolean(option.available);
    if (title && option.label) title.textContent = option.label;
    if (description && option.description) description.textContent = option.description;
    input.disabled = !available;
    if (!available) input.checked = false;
    label?.classList.toggle('unavailable', !available);
    if (status) {
      status.textContent = available ? '' : (option.unavailable_reason || '現在利用できません。');
      status.hidden = available;
    }
  });
  setRetrievalModeError();
}

function collectRetrievalModes({ requireVector = false } = {}) {
  const inputs = [...document.querySelectorAll('input[name="retrievalMode"]')];
  if (!inputs.length) return [...defaultRetrievalModes];
  const available = inputs.filter(input => !input.disabled);
  const selected = available.filter(input => input.checked).map(input => input.value);
  if (!available.length) {
    setRetrievalModeError('利用できる検索方式がありません。設定画面を確認してください。', { focus: true });
    return null;
  }
  if (!selected.length) {
    setRetrievalModeError('検索方式を1つ以上選択してください。', { focus: true });
    return null;
  }
  if (requireVector && !selected.some(mode => vectorRetrievalModes.has(mode))) {
    setRetrievalModeError('画像だけで検索する場合は、類似検索方式を1つ以上選択してください。', { focus: true });
    return null;
  }
  setRetrievalModeError();
  return selected;
}

document.addEventListener?.('change', event => {
  if (event.target?.matches?.('input[name="retrievalMode"]')) setRetrievalModeError();
});

export async function loadDynamicSearchFilters() {
  try {
    const data = await authApiCall('/ai/api/search/v2/filters');
    v2RetrievalActive = Boolean(data.v2_retrieval_active);
    dynamicFieldDefinitions = data.fields || [];
    dynamicFiltersLoaded = true;
    renderMetadataSearchFilters(data);
    const wrapper = document.getElementById('dynamicSearchFilters');
    const container = document.getElementById('dynamicSearchFilterFields');
    if (wrapper && container) {
      wrapper.hidden = !(v2RetrievalActive && dynamicFieldDefinitions.length);
      const operatorLabels = { eq: '一致', contains: '含む', gte: '以上', lte: '以下', between: '範囲' };
      container.innerHTML = dynamicFieldDefinitions.map((field, index) => {
        const type = field.value_type === 'number' ? 'number' : (field.value_type === 'date' ? 'date' : 'text');
        const valueId = `dynamic-filter-value-${index}`;
        const valueControl = field.value_type === 'boolean'
          ? `<select id="${valueId}" class="form-input" data-filter-value ${field.conflicted ? 'disabled' : ''}><option value="">指定なし</option><option value="true">はい</option><option value="false">いいえ</option></select>`
          : `<input id="${valueId}" class="form-input" data-filter-value type="${type}" ${field.conflicted ? 'disabled' : ''}>`;
        return `<div class="dynamic-search-filter" data-filter-key="${escapeHtml(field.key)}" data-value-type="${escapeHtml(field.value_type)}" data-conflicted="${field.conflicted ? 'true' : 'false'}">
          <label class="form-label" for="${valueId}">${escapeHtml(field.label)} <small>${escapeHtml(field.key)}</small></label>
          <div class="dynamic-search-filter-controls">
            <select class="form-input" data-filter-operator aria-label="${escapeHtml(field.label)}の比較方法" ${field.conflicted ? 'disabled' : ''} onchange="window.searchModule.toggleFilterBetween(this)">${(field.allowed_operators || []).map(operator => `<option value="${operator}">${operatorLabels[operator] || escapeHtml(operator)}</option>`).join('')}</select>
            ${valueControl}
            <input class="form-input" data-filter-value-second type="${type}" hidden placeholder="上限値" aria-label="${escapeHtml(field.label)}の上限値">
          </div>
          ${field.conflicted ? '<div class="dynamic-search-filter-error" role="alert">有効なプロファイル間で型または演算子が一致していません。</div>' : ''}
        </div>`;
      }).join('');
    }
    updateRetrievalModeOptions(data.retrieval_modes);
    const imageQuery = document.getElementById('imageSearchQuery');
    if (imageQuery) {
      imageQuery.disabled = !v2RetrievalActive;
      imageQuery.placeholder = v2RetrievalActive
        ? '画像と組み合わせる条件を入力'
        : '検索索引の初期化後に利用できます';
      if (!v2RetrievalActive) imageQuery.value = '';
    }
  } catch (error) {
    v2RetrievalActive = false;
    const wrapper = document.getElementById('dynamicSearchFilters');
    const imageQuery = document.getElementById('imageSearchQuery');
    if (wrapper) wrapper.hidden = true;
    if (imageQuery) imageQuery.disabled = true;
    console.warn('Dynamic search filters are unavailable:', error);
  }
}

function getMinScore() {
  const value = parseFloat(document.getElementById('minScore')?.value);
  return Number.isFinite(value) ? Math.min(Math.max(value, 0), 1) : 0;
}

function isSearchButtonBusy(button) {
  return button?.dataset.searchBusy === 'true';
}

function setSearchButtonBusy(button, busy, label, cancellable = true) {
  if (!button) return;
  if (busy) {
    if (!button.dataset.originalHtml) button.dataset.originalHtml = button.innerHTML;
    button.dataset.searchBusy = 'true';
    button.disabled = !cancellable;
    button.setAttribute('aria-busy', 'true');
    button.innerHTML = cancellable
      ? '<i class="fas fa-times" aria-hidden="true"></i> キャンセル'
      : `<span class="spinner spinner-sm" aria-hidden="true"></span> ${label}`;
    return;
  }
  button.disabled = false;
  button.removeAttribute('aria-busy');
  delete button.dataset.searchBusy;
  if (button.dataset.originalHtml) button.innerHTML = button.dataset.originalHtml;
  delete button.dataset.originalHtml;
}

function updateSearchElapsed() {
  const elapsed = document.getElementById('searchAgentElapsed');
  if (elapsed && searchProgress.startedAt) {
    elapsed.textContent = `${((Date.now() - searchProgress.startedAt) / 1000).toFixed(1)}秒`;
  }
}

function stopSearchProgressTimer() {
  if (searchProgressTimer) clearInterval(searchProgressTimer);
  searchProgressTimer = null;
  updateSearchElapsed();
}

function startSearchProgressTimer() {
  stopSearchProgressTimer();
  updateSearchElapsed();
  searchProgressTimer = setInterval(updateSearchElapsed, 1000);
}

const chips = (values = []) => values.map(value => `
  <span class="search-agent-chip">${escapeHtml(value)}</span>
`).join('');

function stepDetails(name) {
  const diagnostics = searchProgress.state.result?.diagnostics || {};
  const queryPlan = searchProgress.state.queryPlan || searchProgress.state.result?.diagnostics?.query_plan;
  const keywordPlan = searchProgress.state.keywordPlan || searchProgress.state.result?.diagnostics?.keyword_plan;
  const retrievalSummary = searchProgress.state.retrievalSummary || diagnostics.retrieval_summary;
  const candidateMerge = searchProgress.state.candidateMerge || diagnostics.candidate_merge;
  const rerankSummary = searchProgress.state.rerankSummary || diagnostics.rerank_summary;
  const formatSummary = searchProgress.state.formatSummary || diagnostics.format_summary;
  if (name === 'query_variants' && queryPlan) {
    const sourceLabels = { deterministic: 'ルールベース', llm: 'LLM', off: '原文のみ' };
    return `
      <strong>検索バリエーション</strong>
      <div class="search-agent-chip-list">${chips(queryPlan.variants || [])}</div>
      ${queryPlan.query_expansion_source ? `<div>生成方式: ${escapeHtml(sourceLabels[queryPlan.query_expansion_source] || queryPlan.query_expansion_source)}</div>` : ''}
    `;
  }
  if (name === 'keyword_plan' && keywordPlan?.terms?.length) {
    return `
      <strong>検索キーワード</strong>
      <div class="search-agent-chip-list">${chips(keywordPlan.terms)}</div>
      <div>対象: ${escapeHtml(keywordPlan.target || 'Oracle Text')}</div>
    `;
  }
  if (name === 'retrieval' && retrievalSummary?.channels?.length) {
    return `
      <strong>検索チャンネル</strong>
      <div class="search-agent-step-grid">
        ${retrievalSummary.channels.map(channel => `
          <div>${escapeHtml(channel.channel)}</div>
          <div>${channel.status === 'ok' ? '成功' : '失敗'} / ${escapeHtml(channel.count)}件</div>
        `).join('')}
      </div>
      ${retrievalSummary.filename_filter ? `<div>ファイル名条件: ${escapeHtml(retrievalSummary.filename_filter)}</div>` : ''}
    `;
  }
  if (name === 'candidate_merge' && candidateMerge) {
    return `
      <div>方式: ${escapeHtml(candidateMerge.method || 'weighted_rrf')}</div>
      <div>入力リスト: ${escapeHtml(candidateMerge.source_lists)} / 統合後候補: ${escapeHtml(candidateMerge.candidate_count)}件</div>
      <div>上限: ${escapeHtml(candidateMerge.limit)}件</div>
    `;
  }
  if (name === 'rerank' && rerankSummary) {
    return `
      <div>状態: ${rerankSummary.skipped ? 'スキップ' : (rerankSummary.enabled ? '有効' : '無効')}</div>
      <div>候補: ${escapeHtml(rerankSummary.candidate_count)}件 / 採用上限: ${escapeHtml(rerankSummary.top_n)}件</div>
      ${rerankSummary.degraded ? '<div>一部降格: rerank</div>' : ''}
    `;
  }
  if (name === 'format_results' && formatSummary) {
    return `
      <div>文書: ${escapeHtml(formatSummary.total_documents)}件</div>
      <div>証拠: ${escapeHtml(formatSummary.total_evidence)}件</div>
    `;
  }
  return '';
}

function renderSearchProgress(message = '') {
  const root = document.getElementById('searchAgentProgress');
  if (!root) return;
  root.hidden = false;
  const status = document.getElementById('searchAgentStatus');
  const elapsed = document.getElementById('searchAgentElapsed');
  const steps = document.getElementById('searchAgentSteps');
  const details = document.getElementById('searchAgentDetails');
  if (status) status.textContent = message || searchProgress.state.message || '検索中...';
  updateSearchElapsed();
  if (steps) {
    steps.innerHTML = [...searchProgress.steps.entries()].map(([name, statusValue]) => {
      const detail = stepDetails(name);
      const header = `
        <span>${escapeHtml(stepLabels[name] || name)}</span>
        <span class="search-agent-step-status">${statusValue === 'done' ? '完了' : '処理中'}</span>
      `;
      return `
        <li class="search-agent-step search-agent-step-${statusValue}">
          ${detail ? `
            <details>
              <summary>${header}</summary>
              <div class="search-agent-step-body">${detail}</div>
            </details>
          ` : `<div class="search-agent-step-static">${header}</div>`}
        </li>
      `;
    }).join('');
  }
  if (details) {
    const degraded = searchProgress.state.result?.diagnostics?.degraded || [];
    details.innerHTML = degraded.length ? `<div>一部降格: ${escapeHtml(degraded.join(', '))}</div>` : '';
    details.hidden = !details.innerHTML;
  }
}

function resetSearchProgress(message = '検索を開始しました') {
  searchProgress.startedAt = Date.now();
  searchProgress.state = { message };
  searchProgress.steps = new Map();
  const root = document.getElementById('searchAgentProgress');
  if (root) root.open = true;
  renderSearchProgress(message);
  startSearchProgressTimer();
}

function finishSearchProgress(message) {
  if (message) searchProgress.state.message = message;
  stopSearchProgressTimer();
  renderSearchProgress(message);
  const root = document.getElementById('searchAgentProgress');
  if (root) root.open = false;
}

function applyStateDelta(delta = []) {
  delta.forEach(operation => {
    if (operation.op !== 'replace' || !operation.path?.startsWith('/')) return;
    searchProgress.state[operation.path.slice(1)] = operation.value;
  });
}

function handleSearchEvent(event) {
  if (event.type === 'RUN_STARTED') resetSearchProgress();
  if (event.type === 'STATE_SNAPSHOT') searchProgress.state = event.snapshot || {};
  if (event.type === 'STEP_STARTED') searchProgress.steps.set(event.stepName, 'running');
  if (event.type === 'STEP_FINISHED') searchProgress.steps.set(event.stepName, 'done');
  if (event.type === 'STATE_DELTA') applyStateDelta(event.delta);
  if (event.type === 'RUN_FINISHED') {
    const result = event.result || searchProgress.state.result;
    if (result) searchProgress.state.result = result;
    finishSearchProgress('検索が完了しました');
    return result;
  }
  if (event.type === 'RUN_ERROR') {
    finishSearchProgress(event.message || '検索に失敗しました');
    throw new Error(event.message || '検索に失敗しました');
  }
  renderSearchProgress(event.message);
  return null;
}

function parseSseBlock(block) {
  const data = block.split(/\r?\n/)
    .filter(line => line.startsWith('data:'))
    .map(line => line.slice(5).trimStart())
    .join('\n');
  return data ? JSON.parse(data) : null;
}

async function readSearchEventStream(response) {
  let finalResult = null;
  let buffer = '';
  const consume = text => {
    buffer += text;
    const blocks = buffer.split(/\r?\n\r?\n/);
    buffer = blocks.pop() || '';
    blocks.forEach(block => {
      const event = parseSseBlock(block);
      if (!event) return;
      finalResult = handleSearchEvent(event) || finalResult;
    });
  };

  if (!response.body?.getReader) {
    consume(await response.text());
  } else {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      consume(decoder.decode(value, { stream: true }));
    }
    consume(decoder.decode());
  }
  if (buffer.trim()) {
    const event = parseSseBlock(buffer);
    if (event) finalResult = handleSearchEvent(event) || finalResult;
  }
  if (!finalResult) throw new Error('検索結果が返されませんでした');
  return finalResult;
}

async function streamSearch(endpoint, options) {
  searchCancelled = false;
  currentSearchController = new AbortController();
  resetSearchProgress();
  try {
    const response = await authFetchWithAuth(endpoint, {
      ...options,
      signal: currentSearchController.signal
    });
    if (!response.ok) {
      const error = await response.json().catch(() => ({ detail: response.statusText }));
      throw new Error(error.detail || '検索に失敗しました');
    }
    return await readSearchEventStream(response);
  } catch (error) {
    if (searchCancelled) {
      finishSearchProgress('検索をキャンセルしました');
      throw new Error('検索をキャンセルしました');
    }
    finishSearchProgress(error.message || '検索に失敗しました');
    throw error;
  } finally {
    currentSearchController = null;
  }
}

export function cancelCurrentSearch() {
  searchCancelled = true;
  currentSearchController?.abort();
  if (currentSearchController) finishSearchProgress('検索をキャンセルしました');
}

export function invalidateDynamicSearchFilters() {
  dynamicFiltersLoaded = false;
}

export function toggleFilterBetween(select) {
  const row = select.closest('[data-filter-key]');
  const second = row?.querySelector('[data-filter-value-second]');
  if (second) second.hidden = select.value !== 'between';
}

function collectDynamicFilters() {
  return [...document.querySelectorAll('[data-filter-key]')].flatMap(row => {
    if (row.dataset.conflicted === 'true') return [];
    const operator = row.querySelector('[data-filter-operator]').value;
    const first = row.querySelector('[data-filter-value]').value;
    if (first === '') return [];
    const second = row.querySelector('[data-filter-value-second]').value;
    if (operator === 'between' && second === '') {
      throw new Error(`${row.dataset.filterKey}: 範囲指定には下限値と上限値が必要です`);
    }
    const convert = value => {
      if (row.dataset.valueType !== 'number') return value;
      const number = Number(value);
      if (!Number.isFinite(number)) throw new Error(`${row.dataset.filterKey}: 数値を入力してください`);
      return number;
    };
    return [{
      field_key: row.dataset.filterKey,
      operator,
      value: operator === 'between' ? [convert(first), convert(second)] : convert(first)
    }];
  });
}

function objectUrl(bucket, objectName) {
  const encoded = String(objectName).split('/').map(encodeURIComponent).join('/');
  return `/ai/api/object/${encodeURIComponent(bucket)}/${encoded}`;
}

function adaptV2Response(data, { includeImageSimilarity = false } = {}) {
  const source = data.results || [];
  // rerank_score はcross-encoderの絶対的な関連度(0〜1)。無い場合(rerank無効/失敗)は
  // RRFスコアしかなく絶対的な意味を持たないため、%表示自体を出さない(null)。
  const toPercent = score => {
    if (score == null) return null;
    const value = Number(score);
    return Number.isFinite(value) ? Math.max(0, Math.min(1, value)) * 100 : null;
  };
  const adaptDocument = document => {
    const seen = new Set();
    const matched_images = (document.evidence || []).flatMap(evidence => {
      if (!evidence.asset_url || seen.has(evidence.asset_url)) return [];
      seen.add(evidence.asset_url);
      return [{
        embed_id: evidence.evidence_id,
        bucket: document.bucket,
        object_name: evidence.asset_url,
        page_number: evidence.page_number,
        score: evidence.score,
        rerank_score: evidence.rerank_score,
        image_similarity_score: evidence.image_similarity_score,
        match_percent: toPercent(evidence.rerank_score),
        image_similarity_percent: includeImageSimilarity
          ? toPercent(evidence.image_similarity_score)
          : null,
        url: objectUrl(document.bucket, evidence.asset_url),
        retrieval_channels: evidence.retrieval_channels || [],
        verification_status: evidence.verification_status,
        profile_verifications: evidence.profile_verifications || {},
        match_reasons: evidence.match_reasons || [],
        visual_rank: evidence.visual_rank,
        text_rerank_rank: evidence.text_rerank_rank,
        profile_slots: evidence.profile_slots || [],
        caption: evidence.caption,
        text_excerpt: evidence.text_excerpt
      }];
    });
    return {
      file_id: document.document_id,
      bucket: document.bucket,
      object_name: document.object_name,
      original_filename: document.file_name,
      match_percent: toPercent(document.rerank_score),
      image_similarity_percent: includeImageSimilarity
        ? toPercent(document.image_similarity_score)
        : null,
      matched_images,
      url: objectUrl(document.bucket, document.object_name),
      profile_slots: document.profile_slots,
      document_set_id: document.document_set_id,
      document_set_label: document.document_set_label,
      direct_match: document.direct_match !== false,
      matched_concept_ids: document.matched_concept_ids || [],
      thumbnail_object_name: document.thumbnail_object_name || null,
      thumbnail_page_number: document.thumbnail_page_number ?? null
    };
  };
  const adaptedResults = source.map(adaptDocument);
  const groups = (data.groups || []).map(group => ({
    group_key: group.group_key,
    document_set_id: group.document_set_id,
    label: group.label,
    score: group.score,
    matched_concept_ids: group.matched_concept_ids || [],
    direct_document_ids: (group.direct_matches || []).map(item => item.document_id),
    related_documents: (group.related_documents || []).map(adaptDocument)
  }));
  const adaptedById = new Map(adaptedResults.map(item => [item.file_id, item]));
  const groupedIds = new Set(groups.flatMap(group => group.direct_document_ids));
  const results = groups.length
    ? [
        ...groups.flatMap(group => group.direct_document_ids.map(documentId => adaptedById.get(documentId)).filter(Boolean)),
        ...adaptedResults.filter(item => !groupedIds.has(item.file_id))
      ]
    : adaptedResults;
  const groupByDocumentId = new Map();
  groups.forEach(group => group.direct_document_ids.forEach(documentId => groupByDocumentId.set(documentId, group)));
  results.forEach(item => {
    const group = groupByDocumentId.get(item.file_id);
    item.group_key = group?.group_key || `document:${item.file_id}`;
  });
  return {
    success: data.success,
    query: data.query,
    results,
    groups,
    total_groups: data.total_groups || groups.length,
    result_order: includeImageSimilarity ? 'image_similarity' : 'search_rank',
    total_files: results.length,
    total_images: results.reduce((count, item) => count + item.matched_images.length, 0),
    processing_time: data.processing_time || 0,
    trace_id: data.trace_id
  };
}

/**
 * 検索タイプを切り替え
 * 
 * テキスト検索と画像検索のUIを切り替える関数です。
 * 
 * @param {string} type - 検索タイプ ('text' または 'image')
 * 
 * ネットワーク通信の影響:
 * - UIの表示切り替えのみ（ネットワーク通信なし）
 * - ユーザーエクスペリエンスの向上
 */
export function switchSearchType(type) {
  currentSearchType = type;
  
  const textTab = document.getElementById('searchTypeTextTab');
  const imageTab = document.getElementById('searchTypeImageTab');
  const textPanel = document.getElementById('textSearchPanel');
  const imagePanel = document.getElementById('imageSearchPanel');
  
  if (type === 'text') {
    // テキスト検索タブをアクティブに
    textTab.style.borderBottomColor = '#1a365d';
    textTab.style.color = '#1a365d';
    imageTab.style.borderBottomColor = 'transparent';
    imageTab.style.color = '#64748b';
    
    textPanel.style.display = 'block';
    imagePanel.style.display = 'none';
    textTab.setAttribute('aria-selected', 'true');
    imageTab.setAttribute('aria-selected', 'false');
    textTab.tabIndex = 0;
    imageTab.tabIndex = -1;
  } else {
    // 画像検索タブをアクティブに
    imageTab.style.borderBottomColor = '#1a365d';
    imageTab.style.color = '#1a365d';
    textTab.style.borderBottomColor = 'transparent';
    textTab.style.color = '#64748b';
    
    imagePanel.style.display = 'block';
    textPanel.style.display = 'none';
    imageTab.setAttribute('aria-selected', 'true');
    textTab.setAttribute('aria-selected', 'false');
    imageTab.tabIndex = 0;
    textTab.tabIndex = -1;
  }
}

/**
 * 検索画像を選択
 * @param {Event} event - ファイル選択イベント
 */
export function handleSearchImageSelect(event) {
  const file = event.target.files[0];
  if (!file) return;
  
  // ファイルサイズチェック (最大10MB)
  const maxSize = 10 * 1024 * 1024;
  if (file.size > maxSize) {
    utilsShowToast('画像ファイルは10MB以下にしてください', 'warning');
    return;
  }
  
  // ファイルタイプチェック
  if (!file.type.match(/^image\/(png|jpeg|jpg|webp)$/)) {
    utilsShowToast('PNG, JPG, JPEG, WebP形式の画像のみ対応しています', 'warning');
    return;
  }
  
  selectedSearchImage = file;
  
  // プレビュー表示
  const reader = new FileReader();
  reader.onload = (e) => {
    const previewImg = document.getElementById('searchImagePreviewImg');
    const previewDiv = document.getElementById('imageSearchPreview');
    const placeholder = document.getElementById('imageSearchPlaceholder');
    const filenameSpan = document.getElementById('searchImageFilename');
    
    if (previewImg && previewDiv && placeholder && filenameSpan) {
      previewImg.src = e.target.result;
      filenameSpan.textContent = file.name;
      previewDiv.style.display = 'block';
      placeholder.style.display = 'none';
    }
  };
  reader.readAsDataURL(file);
}

/**
 * 検索画像をクリア
 */
export function clearSearchImage() {
  selectedSearchImage = null;
  
  const fileInput = document.getElementById('searchImageInput');
  const previewDiv = document.getElementById('imageSearchPreview');
  const placeholder = document.getElementById('imageSearchPlaceholder');
  
  if (fileInput) fileInput.value = '';
  if (previewDiv) previewDiv.style.display = 'none';
  if (placeholder) placeholder.style.display = 'block';
}

/**
 * 画像検索を実行
 */
export async function performImageSearch() {
  const submitButton = document.getElementById('imageSearchSubmitBtn');
  if (isSearchButtonBusy(submitButton)) {
    cancelCurrentSearch();
    return;
  }
  if (!selectedSearchImage) {
    utilsShowToast('検索する画像を選択してください', 'warning');
    return;
  }
  
  // 共通のフィルター値を使用
  const filenameFilter = document.getElementById('filenameFilter').value.trim();
  const topK = parseInt(document.getElementById('topK').value) || 10;
  const minScore = getMinScore();
  const imageQuery = document.getElementById('imageSearchQuery')?.value.trim() || '';
  const verify = Boolean(document.getElementById('searchVlmVerify')?.checked);
  const retrievalModes = collectRetrievalModes({ requireVector: !imageQuery });
  if (!retrievalModes) return;
  let usesEventStream = false;
  searchCancelled = false;
  
  try {
    hideSearchResults();
    setSearchButtonBusy(submitButton, true, '検索中...');
    if (!dynamicFiltersLoaded) await loadDynamicSearchFilters();
    usesEventStream = true;
    setSearchButtonBusy(submitButton, true, '検索中...', usesEventStream);
    if (searchCancelled) throw new Error('検索をキャンセルしました');

    // FormDataを作成
    const formData = new FormData();
    formData.append('image', selectedSearchImage);
    formData.append('top_k', topK.toString());
    formData.append('min_score', minScore.toString());
    if (filenameFilter) formData.append('filename_filter', filenameFilter);
    const endpoint = '/ai/api/search/v2/image/events';
    formData.append('query', imageQuery);
    formData.append('field_filters', JSON.stringify(collectDynamicFilters()));
    formData.append('metadata_filters', JSON.stringify(collectMetadataFilters()));
    formData.append('document_types', '[]');
    formData.append('retrieval_modes', JSON.stringify(retrievalModes));
    formData.append('verify', verify ? 'true' : 'false');
    const conceptPayload = selectedConceptPayload();
    formData.append('selected_concept_ids', JSON.stringify(conceptPayload.selected_concept_ids));
    formData.append('concept_mode', conceptPayload.concept_mode);

    const data = usesEventStream ? await streamSearch(endpoint, {
      method: 'POST',
      body: formData
    }) : await authApiCall(endpoint, {
      method: 'POST',
      body: formData,
      timeout: 70000
    });

    displaySearchResults(adaptV2Response(data, { includeImageSimilarity: true }));
    
    // 検索完了メッセージを表示
    utilsShowToast('画像検索が完了しました', 'success');
    
  } catch (error) {
    const message = error.message.includes('タイムアウト')
      ? '画像検索がタイムアウトしました。条件を見直して再度お試しください'
      : `画像検索に失敗しました: ${error.message}。再度お試しください`;
    utilsShowToast(message, 'error');
  } finally {
    if (!usesEventStream) utilsHideLoading();
    setSearchButtonBusy(submitButton, false);
  }
}

/**
 * 認証トークン付きのURLを生成
 * @param {string} url - ベースURL(検索APIから返却されたURLまたはバケット/オブジェクト名)
 * @param {string} bucket - バケット名(オプション、旧形式互換用)
 * @param {string} objectName - オブジェクト名(オプション、旧形式互換用)
 * @returns {string} トークン付きのURL
 */
function getAuthenticatedImageUrl(urlOrBucket, objectName) {
  const token = localStorage.getItem('loginToken');
  
  // 既に完全なURLが渡された場合(検索APIのurlフィールド)
  if (urlOrBucket && (urlOrBucket.startsWith('http://') || urlOrBucket.startsWith('https://') || urlOrBucket.startsWith('/'))) {
    const url = urlOrBucket;
    if (token) {
      const separator = url.includes('?') ? '&' : '?';
      return `${url}${separator}token=${encodeURIComponent(token)}`;
    }
    return url;
  }
  
  // 旧形式互換: bucket + objectName が渡された場合
  if (urlOrBucket && objectName) {
    const baseUrl = `/ai/api/object/${urlOrBucket}/${encodeURIComponent(objectName)}`;
    if (token) {
      return `${baseUrl}?token=${encodeURIComponent(token)}`;
    }
    return baseUrl;
  }
  
  return urlOrBucket || '';
}

/**
 * 検索を実行
 */
export async function performSearch() {
  const submitButton = document.getElementById('textSearchSubmitBtn');
  if (isSearchButtonBusy(submitButton)) {
    cancelCurrentSearch();
    return;
  }
  const query = document.getElementById('searchQuery').value.trim();
  const filenameFilter = document.getElementById('filenameFilter').value.trim();
  const topK = parseInt(document.getElementById('topK').value) || 10;
  const minScore = getMinScore();
  const verify = Boolean(document.getElementById('searchVlmVerify')?.checked);
  const conceptPayload = selectedConceptPayload();
  let usesEventStream = false;
  searchCancelled = false;
  
  if (!query && !conceptPayload.selected_concept_ids.length) {
    utilsShowToast('自然言語を入力するか、検索タグを1つ以上選んでください', 'warning');
    return;
  }
  const retrievalModes = collectRetrievalModes();
  if (!retrievalModes) return;
  
  try {
    hideSearchResults();
    setSearchButtonBusy(submitButton, true, '検索中...');
    if (!dynamicFiltersLoaded) await loadDynamicSearchFilters();
    usesEventStream = true;
    setSearchButtonBusy(submitButton, true, '検索中...', usesEventStream);
    if (searchCancelled) throw new Error('検索をキャンセルしました');

    const requestBody = { query, top_k: topK, min_score: minScore, filename_filter: filenameFilter || null, field_filters: collectDynamicFilters(), metadata_filters: collectMetadataFilters(), document_types: [], current_version_only: true, retrieval_modes: retrievalModes, verify, ...conceptPayload };
    const endpoint = '/ai/api/search/v2/events';

    const data = usesEventStream ? await streamSearch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(requestBody)
    }) : await authApiCall(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(requestBody),
      timeout: 70000
    });

    displaySearchResults(adaptV2Response(data));
    
    // 検索完了メッセージを表示
    utilsShowToast('検索が完了しました', 'success');
    
  } catch (error) {
    const message = error.message.includes('タイムアウト')
      ? '検索がタイムアウトしました。条件を見直して再度お試しください'
      : `検索に失敗しました: ${error.message}。再度お試しください`;
    utilsShowToast(message, 'error');
  } finally {
    if (!usesEventStream) utilsHideLoading();
    setSearchButtonBusy(submitButton, false);
  }
}

/**
 * 検索結果を表示
 * @param {Object} data - 検索結果データ
 */
export function displaySearchResults(data) {
  const resultsDiv = document.getElementById('searchResults');
  const summarySpan = document.getElementById('searchResultsSummary');
  const listDiv = document.getElementById('searchResultsList');
  
  if (!data.results || data.results.length === 0) {
    const minScore = getMinScore();
    const retryHint = minScore > 0
      ? `最小ベクトル類似度（現在 ${minScore.toFixed(2)}）を下げるか、別のキーワードで検索してみてください`
      : '別のキーワードや検索方式で検索してみてください';
    resultsDiv.style.display = 'block';
    summarySpan.textContent = '検索結果なし';
    listDiv.innerHTML = `
      <div class="empty-state">
        <div class="empty-state-icon"><i class="fas fa-search" style="color: #94a3b8;"></i></div>
        <div class="empty-state-title">検索結果が見つかりませんでした</div>
        <div class="empty-state-subtitle">${retryHint}</div>
      </div>
    `;
    return;
  }
  
  resultsDiv.style.display = 'block';
  const groupSummary = data.total_groups ? `${data.total_groups}案件・` : '';
  summarySpan.textContent = `${groupSummary}${data.total_files}ファイルの直接一致 (${data.total_images}画像, ${data.processing_time.toFixed(2)}秒)`;
  
  // ファイル単位で表示
  listDiv.innerHTML = data.results.map((fileResult, fileIndex) => {
    const originalFilename = displayFilename(fileResult);
    const group = data.groups?.find(item => item.group_key === fileResult.group_key);
    const groupIndex = data.groups?.findIndex(item => item.group_key === fileResult.group_key) ?? -1;
    const previous = data.results[fileIndex - 1];
    const next = data.results[fileIndex + 1];
    const isGroupStart = Boolean(group) && previous?.group_key !== fileResult.group_key;
    const isGroupEnd = Boolean(group) && next?.group_key !== fileResult.group_key;
    const groupHeader = isGroupStart ? `<section class="search-result-group">
      <div class="search-result-group-header">
        <div><i class="fas fa-layer-group"></i><strong>${escapeHtml(group.label)}</strong>${group.document_set_id ? '' : '<span>未グループ</span>'}</div>
        <small>直接一致 ${group.direct_document_ids.length}件${group.related_documents.length ? `・関連資料 ${group.related_documents.length}件` : ''}</small>
      </div>` : '';
    
    // ファイル情報カード
    const fileCardHtml = `
      <div class="card search-result-card">
        <!-- ファイルヘッダー -->
        <div class="card-header search-result-header">
          <div class="search-result-header-row">
            <div class="search-result-header-left">
              <span class="badge search-result-badge-white">#${fileIndex + 1}</span>
              <div>
                <div class="search-result-filename"><i class="fas fa-file"></i> ${escapeHtml(originalFilename)}</div>
              </div>
            </div>
            <div class="search-result-stats">
              ${fileResult.image_similarity_percent != null ? `<span class="badge search-result-stat-badge">
                画像類似度: ${fileResult.image_similarity_percent.toFixed(1)}%
              </span>` : ''}
              ${fileResult.match_percent != null ? `<span class="badge search-result-stat-badge">
                関連度: ${fileResult.match_percent.toFixed(1)}%
              </span>` : ''}
              <span class="badge search-result-stat-badge">
                ${fileResult.matched_images.length
                  ? `${fileResult.matched_images.length}ページ`
                  : fileResult.matched_concept_ids?.length
                    ? '文書全体一致'
                    : 'ページ画像なし'}
              </span>
              <button 
                onclick="window.searchModule.downloadFile('${fileResult.bucket}', '${encodeURIComponent(fileResult.object_name)}')"
                class="search-result-download-btn"
                title="ファイルをダウンロード"
              >
                <i class="fas fa-download"></i> ダウンロード
              </button>
            </div>
          </div>
        </div>
        
        <!-- ページ画像グリッド -->
        <div class="card-body">
          <div class="search-result-body-title">
            <i class="fas fa-images"></i>
            ${fileResult.matched_images.length
              ? `マッチしたページ画像（${data.result_order === 'image_similarity' ? '画像類似度が高い順' : '検索順位順'}）`
              : fileResult.thumbnail_object_name
                ? '文書の代表画像（ページ単位の一致ではありません）'
                : 'ページ単位の画像一致はありません'}
          </div>
          <div class="search-result-images-grid">
            ${fileResult.matched_images.map((img, imgIndex) => {
              // img.url(APIから返却された絶対URL)を優先、なければbucket+object_nameから生成
              const imageUrl = img.url ? getAuthenticatedImageUrl(img.url) : getAuthenticatedImageUrl(img.bucket, img.object_name);
              
              return `
                <article class="image-card search-result-image-card">
                  <button
                    type="button"
                    class="search-result-image-open"
                    onclick="window.searchModule.showSearchImageModal(${fileIndex}, ${imgIndex})"
                    aria-label="ページ ${img.page_number}を拡大"
                  >
                  <!-- サムネイル画像 -->
                  <div class="search-result-image-aspect">
                    <img
                      src="${imageUrl}" 
                      alt="ページ ${img.page_number}"
                      loading="lazy"
                      decoding="async"
                      style="
                        position: absolute;
                        top: 0;
                        left: 0;
                        width: 100%;
                        height: 100%;
                        object-fit: contain;
                      "
                      onerror="this.src='data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 width=%27200%27 height=%27200%27%3E%3Crect fill=%27%23f1f5f9%27 width=%27200%27 height=%27200%27/%3E%3Ctext x=%2750%25%27 y=%2750%25%27 text-anchor=%27middle%27 dy=%27.3em%27 fill=%27%2394a3b8%27 font-size=%2724%27%3E画像エラー%3C/text%3E%3C/svg%3E'"
                    />
                    <!-- スコアバッジ -->
                    ${img.image_similarity_percent != null || img.match_percent != null ? `<div style="
                      position: absolute;
                      top: 8px;
                      right: 8px;
                      display: grid;
                      gap: 4px;
                      justify-items: end;
                    ">
                      ${img.image_similarity_percent != null ? `<span style="background:rgba(26,54,93,.95);color:white;padding:4px 8px;border-radius:4px;font-size:11px;font-weight:600;box-shadow:0 2px 4px rgba(0,0,0,.2)">
                        画像類似度 ${img.image_similarity_percent.toFixed(1)}%
                      </span>` : ''}
                      ${img.match_percent != null ? `<span style="background:rgba(30,64,175,.92);color:white;padding:4px 8px;border-radius:4px;font-size:11px;font-weight:600;box-shadow:0 2px 4px rgba(0,0,0,.2)">
                        関連度 ${img.match_percent.toFixed(1)}%
                      </span>` : ''}
                    </div>` : ''}
                  </div>
                  </button>

                  <!-- 画像情報 -->
                  <div class="search-result-image-info">
                    <div class="search-result-image-heading">
                      <div class="search-result-image-title">
                        <i class="fas fa-file"></i> ページ ${img.page_number}
                      </div>
                      <button type="button" class="search-evidence-detail-button" onclick="window.searchModule.showSearchEvidenceDetails(${fileIndex}, ${imgIndex})">
                        <i class="fas fa-circle-info"></i> 詳細
                      </button>
                    </div>
                    ${img.image_similarity_percent != null ? `<div class="search-result-image-similarity">
                      画像類似度: ${img.image_similarity_percent.toFixed(1)}%
                    </div>` : ''}
                    ${img.match_percent != null ? `<div class="search-result-image-similarity">
                      関連度: ${img.match_percent.toFixed(1)}%
                    </div>` : ''}
                  </div>
                </article>
              `;
            }).join('')}
            ${!fileResult.matched_images.length && fileResult.thumbnail_object_name ? `
              <button
                type="button"
                class="image-card search-result-image-card search-result-representative-card"
                onclick="window.searchModule.showSearchRepresentativeImage(${fileIndex})"
              >
                <div class="search-result-image-aspect">
                  <img
                    src="${getAuthenticatedImageUrl(objectUrl(fileResult.bucket, fileResult.thumbnail_object_name))}"
                    alt="${escapeHtml(originalFilename)}の代表画像"
                    loading="lazy"
                    decoding="async"
                  >
                  <span class="search-result-representative-badge">代表画像</span>
                </div>
                <div class="search-result-image-info">
                  <div class="search-result-image-title">
                    <i class="fas fa-file"></i>
                    ${fileResult.thumbnail_page_number != null
                      ? `ページ ${fileResult.thumbnail_page_number}`
                      : '先頭ページ'}
                  </div>
                  <div class="search-result-image-similarity">AI検索候補による文書全体一致</div>
                </div>
              </button>
            ` : ''}
          </div>
          ${!fileResult.matched_images.length && !fileResult.thumbnail_object_name ? `
            <div class="search-result-no-page-evidence">
              <i class="fas fa-info-circle"></i>
              この結果は文書全体の情報で一致しました。表示できるページ画像はありません。
            </div>
          ` : ''}
        </div>
      </div>
    `;
    
    const relatedHtml = isGroupEnd && group.related_documents.length ? `<div class="search-related-documents">
      <h4><i class="fas fa-paperclip"></i> 同じ案件の関連資料</h4>
      ${group.related_documents.map((item, relatedIndex) => {
        const thumbnailUrl = item.thumbnail_object_name
          ? getAuthenticatedImageUrl(objectUrl(item.bucket, item.thumbnail_object_name))
          : null;
        const thumbnail = thumbnailUrl
          ? `<button type="button" class="search-related-thumbnail" onclick="window.searchModule.showRelatedDocumentThumbnail(${groupIndex}, ${relatedIndex})" aria-label="${escapeHtml(displayFilename(item))}の代表画像を拡大">
              <img src="${thumbnailUrl}" alt="" loading="lazy" decoding="async">
            </button>`
          : `<span class="search-related-thumbnail search-related-thumbnail-empty" aria-hidden="true"><i class="far fa-file"></i></span>`;
        return `<div class="search-related-document">
          <div class="search-related-document-main">${thumbnail}<span>${escapeHtml(displayFilename(item))}</span></div>
          <div class="search-related-document-actions">
            <button type="button" class="apex-button-secondary apex-button-xs" onclick="window.searchModule.showRelatedDocumentTexts(${groupIndex}, ${relatedIndex})"><i class="fas fa-file-lines"></i> 生成テキスト</button>
            <button type="button" class="apex-button-secondary apex-button-xs" onclick="window.searchModule.downloadFile('${escapeHtml(item.bucket)}', '${encodeURIComponent(item.object_name)}')"><i class="fas fa-download"></i> ダウンロード</button>
          </div>
        </div>`;
      }).join('')}
    </div>` : '';
    const groupFooter = isGroupEnd ? '</section>' : '';
    return groupHeader + fileCardHtml + relatedHtml + groupFooter;
  }).join('');
  
  // 検索結果データをグローバルに保存（画像モーダル用）
  window._searchResultsData = data;
}

/**
 * 検索結果用画像モーダルを表示（ナビゲーション対応版）
 * @param {number} fileIndex - ファイルのインデックス
 * @param {number} imageIndex - 画像のインデックス
 */
export function showSearchImageModal(fileIndex, imageIndex) {
  // グローバルに保存された検索結果データを取得
  const data = window._searchResultsData;
  if (!data || !data.results || !data.results[fileIndex]) {
    utilsShowToast('画像データが見つかりません', 'error');
    return;
  }
  
  const fileResult = data.results[fileIndex];
  const matchedImages = fileResult.matched_images;
  
  if (!matchedImages || imageIndex >= matchedImages.length) {
    utilsShowToast('画像が見つかりません', 'error');
    return;
  }
  
  // 画像URLとタイトルのリストを作成
  const imageUrls = matchedImages.map(img => {
    return img.url ? getAuthenticatedImageUrl(img.url) : getAuthenticatedImageUrl(img.bucket, img.object_name);
  });
  
  const imageTitles = matchedImages.map(img => {
    const scores = [];
    if (img.image_similarity_percent != null) {
      scores.push(`画像類似度: ${img.image_similarity_percent.toFixed(1)}%`);
    }
    if (img.match_percent != null) {
      scores.push(`関連度: ${img.match_percent.toFixed(1)}%`);
    }
    return `ページ ${img.page_number}${scores.length ? ` - ${scores.join(' | ')}` : ''}`;
  });
  
  // 共通のshowImageModal関数を呼び出す（画像リストとインデックスを渡す）
  utilsShowImageModal(imageUrls[imageIndex], imageTitles[imageIndex], imageUrls, imageIndex, imageTitles);
}

/**
 * 文書全体一致の代表画像を拡大表示
 * @param {number} fileIndex - ファイルのインデックス
 */
export function showSearchRepresentativeImage(fileIndex) {
  const fileResult = window._searchResultsData?.results?.[fileIndex];
  if (!fileResult?.thumbnail_object_name) {
    utilsShowToast('代表画像が見つかりません', 'info');
    return;
  }
  const imageUrl = getAuthenticatedImageUrl(
    objectUrl(fileResult.bucket, fileResult.thumbnail_object_name)
  );
  const page = fileResult.thumbnail_page_number != null
    ? `（ページ ${fileResult.thumbnail_page_number}）`
    : '';
  const title = `${displayFilename(fileResult)} — 代表画像${page}`;
  utilsShowImageModal(imageUrl, title, [imageUrl], 0, [title]);
}

/**
 * 同じ案件の関連資料の代表画像を拡大表示
 * @param {number} groupIndex - 案件グループのインデックス
 * @param {number} relatedIndex - 関連資料のインデックス
 */
export function showRelatedDocumentThumbnail(groupIndex, relatedIndex) {
  const data = window._searchResultsData;
  const group = data?.groups?.[groupIndex];
  const selected = group?.related_documents?.[relatedIndex];
  if (!selected?.thumbnail_object_name) {
    utilsShowToast('代表画像が見つかりません', 'info');
    return;
  }

  const thumbnails = group.related_documents.filter(item => item.thumbnail_object_name);
  const selectedIndex = thumbnails.findIndex(item => item.file_id === selected.file_id);
  const imageUrls = thumbnails.map(item =>
    getAuthenticatedImageUrl(objectUrl(item.bucket, item.thumbnail_object_name))
  );
  const imageTitles = thumbnails.map(item => {
    const page = item.thumbnail_page_number != null
      ? `（ページ ${item.thumbnail_page_number}）`
      : '';
    return `${displayFilename(item)} — 代表画像${page}`;
  });
  const modalIndex = Math.max(0, selectedIndex);
  utilsShowImageModal(
    imageUrls[modalIndex],
    imageTitles[modalIndex],
    imageUrls,
    modalIndex,
    imageTitles
  );
}

const pageTextArtifactOrder = ['PAGE_TEXT', 'NATIVE_TEXT', 'MINERU_TEXT', 'OCR_TEXT', 'VLM_TEXT'];

function searchPageTextLabel(item) {
  const component = String(item.component_key || '');
  const suffix = component.includes(':') ? component.split(':').slice(1).join(':') : '';
  const componentLabels = {
    native: 'ネイティブ抽出',
    mineru: 'MinerU解析',
    ocr: 'OCR',
    normalize: '正規化',
    render: 'ページ画像生成'
  };
  const artifactLabels = {
    PAGE_TEXT: 'ページテキスト',
    NATIVE_TEXT: '抽出テキスト',
    MINERU_TEXT: 'MinerUテキスト',
    OCR_TEXT: 'OCRテキスト',
    VLM_TEXT: 'VLMテキスト'
  };
  const componentLabel = component.startsWith('vlm:')
    ? `VLMプロファイル ${suffix}`
    : (componentLabels[component] || component || '生成結果');
  return `${componentLabel}・${artifactLabels[item.artifact_kind] || item.artifact_kind}`;
}

function searchPageTextSections(items) {
  return (items || []).slice().sort((left, right) => {
    const leftIndex = pageTextArtifactOrder.indexOf(left.artifact_kind);
    const rightIndex = pageTextArtifactOrder.indexOf(right.artifact_kind);
    return (leftIndex < 0 ? 99 : leftIndex) - (rightIndex < 0 ? 99 : rightIndex);
  }).map(item => {
    let text = item.raw_text || '';
    if (item.payload_json != null) {
      text += `${text ? '\n\n' : ''}--- 構造化出力 (JSON) ---\n${JSON.stringify(item.payload_json, null, 2)}`;
    }
    return {
      label: `${searchPageTextLabel(item)}${item.stage_status === 'STALE' ? '（要更新）' : ''}`,
      text,
      meta: [item.artifact_kind, item.created_at ? `生成日時: ${item.created_at}` : null]
        .filter(Boolean).join('　')
    };
  });
}

function retrievalChannelLabel(channel) {
  const value = String(channel || '');
  if (value === 'keyword:page_text') return '正規化したページテキストに検索語が一致';
  if (value.startsWith('keyword:vlm_text_slot_')) return `VLMプロファイル${value.split('_').pop()}の生成テキストに検索語が一致`;
  if (value.includes('page_image_page_text')) return 'ページ画像とページテキストを合わせた意味類似検索で候補化';
  if (value.includes('page_image')) return 'ページ画像の意味類似検索で候補化';
  if (value.includes('vlm_text')) return 'VLM生成テキストの意味類似検索で候補化';
  if (value.startsWith('vector:')) return 'ページテキストの意味類似検索で候補化';
  if (value.startsWith('concept:')) return '選択したAI検索タグとの一致で候補化';
  return '検索処理の候補抽出経路';
}

function deterministicEvidenceSection(image) {
  const lines = [];
  if (image.match_percent != null) {
    lines.push(`関連度 ${image.match_percent.toFixed(1)}%`);
    lines.push('関連度は、候補抽出後に検索文とページ内容を再順位付けしたスコアを0〜100%で表示しています。生成AIによる説明値ではありません。');
  } else {
    lines.push('再順位付けスコアがないため、関連度の百分率は表示していません。');
  }
  if (image.image_similarity_percent != null) {
    lines.push(`画像類似度 ${image.image_similarity_percent.toFixed(1)}%（検索画像とページ画像のベクトル距離から算出）`);
  }
  if (image.visual_rank != null) lines.push(`画像検索内の順位: ${image.visual_rank}位`);
  if (image.text_rerank_rank != null) lines.push(`テキスト再順位付け内の順位: ${image.text_rerank_rank}位`);
  if (image.retrieval_channels?.length) {
    lines.push('', '候補になった検索経路:');
    image.retrieval_channels.forEach(channel => {
      lines.push(`・${retrievalChannelLabel(channel)}  [${channel}]`);
    });
  }
  if (image.match_reasons?.length) {
    lines.push('', '検索処理が記録した一致理由:');
    image.match_reasons.forEach(reason => lines.push(`・${reason}`));
  }
  if (image.verification_status && image.verification_status !== 'not_requested') {
    lines.push('', `VLM精密確認: ${image.verification_status}`);
  }
  if (image.caption) lines.push('', '画像説明:', image.caption);
  if (image.text_excerpt) lines.push('', '検索時のテキスト抜粋:', image.text_excerpt);
  return {
    id: 'relevance-details',
    label: '関連度の詳細',
    meta: '検索処理が保持している値をルールで説明しています',
    text: lines.join('\n')
  };
}

async function requestEvidenceAiExplanation(fileResult, image, query) {
  const response = await authApiCall('/ai/api/search/v2/evidence/explain', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      query: query || '',
      file_name: displayFilename(fileResult),
      page_number: image.page_number,
      relevance_percent: image.match_percent,
      image_similarity_percent: image.image_similarity_percent,
      retrieval_channels: image.retrieval_channels || [],
      match_reasons: image.match_reasons || [],
      text_excerpt: image.text_excerpt || '',
      caption: image.caption || '',
      visual_rank: image.visual_rank,
      text_rerank_rank: image.text_rerank_rank
    }),
    timeout: 120000
  });
  return {
    id: 'ai-explanation',
    label: 'AIによる解説',
    meta: 'ボタン操作時に生成した補足です。検索順位や関連度は変更しません。',
    text: response.explanation
  };
}

export async function showSearchEvidenceDetails(fileIndex, imageIndex) {
  const data = window._searchResultsData;
  const fileResult = data?.results?.[fileIndex];
  const image = fileResult?.matched_images?.[imageIndex];
  if (!fileResult || !image) {
    utilsShowToast('検索結果の詳細が見つかりません', 'error');
    return;
  }
  try {
    utilsShowLoading('生成テキストと関連度の詳細を取得しています...');
    let generatedSections = [];
    try {
      const pageTexts = await authApiCall(`/ai/api/documents/${encodeURIComponent(fileResult.file_id)}/page-texts?release=latest&page_number=${Number(image.page_number)}`);
      generatedSections = searchPageTextSections(pageTexts.items);
    } catch (textError) {
      if (textError?.status !== 404) throw textError;
    }
    const sections = [deterministicEvidenceSection(image), ...generatedSections];
    utilsShowTextPreviewModal(
      `${displayFilename(fileResult)} — ページ ${Number(image.page_number)} の詳細`,
      sections,
      {
        primaryActionLabel: 'AIで関連理由を解説',
        onPrimaryAction: () => requestEvidenceAiExplanation(fileResult, image, data.query)
      }
    );
  } catch (error) {
    utilsShowToast(`詳細を取得できませんでした: ${error.message}`, 'error');
  } finally {
    utilsHideLoading();
  }
}

export async function showRelatedDocumentTexts(groupIndex, relatedIndex) {
  const selected = window._searchResultsData?.groups?.[groupIndex]?.related_documents?.[relatedIndex];
  if (!selected) {
    utilsShowToast('関連資料が見つかりません', 'error');
    return;
  }
  const pageNumber = Number(selected.thumbnail_page_number || 1);
  try {
    utilsShowLoading('生成テキストを取得しています...');
    const data = await authApiCall(`/ai/api/documents/${encodeURIComponent(selected.file_id)}/page-texts?release=latest&page_number=${pageNumber}`);
    const sections = searchPageTextSections(data.items);
    if (!sections.length) {
      utilsShowToast('このページにはまだ生成テキストがありません', 'info');
      return;
    }
    utilsShowTextPreviewModal(`${displayFilename(selected)} — ページ ${pageNumber} の生成テキスト`, sections);
  } catch (error) {
    if (error?.status === 404) {
      utilsShowToast('このページにはまだ生成テキストがありません', 'info');
      return;
    }
    utilsShowToast(`生成テキストを取得できませんでした: ${error.message}`, 'error');
  } finally {
    utilsHideLoading();
  }
}

/**
 * ファイルをダウンロード
 * @param {string} bucket - バケット名
 * @param {string} encodedObjectName - エンコードされたオブジェクト名
 */
export async function downloadFile(bucket, encodedObjectName) {
  try {
    // bucket が既に完全なURLの場合(検索結果のurl)と、bucket+objectNameの場合の両対応
    let fileUrl;
    if (bucket && (bucket.startsWith('http://') || bucket.startsWith('https://') || bucket.startsWith('/'))) {
      fileUrl = getAuthenticatedImageUrl(bucket);
    } else {
      fileUrl = getAuthenticatedImageUrl(bucket, decodeURIComponent(encodedObjectName));
    }
    
    // 新しいタブで開く
    window.open(fileUrl, '_blank');
    
    utilsShowToast('ファイルを開きました', 'success');
  } catch (error) {
    utilsShowToast(`ダウンロードに失敗しました: ${error.message}`, 'error');
  }
}

/**
 * 前回の検索結果表示を非表示にする
 */
function hideSearchResults() {
  const resultsDiv = document.getElementById('searchResults');
  if (resultsDiv) resultsDiv.style.display = 'none';
}

/**
 * 検索結果をクリア
 */
export function clearSearchResults() {
  cancelCurrentSearch();
  setRetrievalModeError();
  // テキスト検索のクリア
  document.getElementById('searchQuery').value = '';

  // 画像検索のクリア
  clearSearchImage();
  const imageQuery = document.getElementById('imageSearchQuery');
  if (imageQuery) imageQuery.value = '';

  // 検索結果を非表示
  hideSearchResults();
  const progress = document.getElementById('searchAgentProgress');
  if (progress) progress.hidden = true;
  stopSearchProgressTimer();
}

// windowオブジェクトに登録（HTMLから呼び出せるように）
window.searchModule = {
  performSearch,
  performImageSearch,
  displaySearchResults,
  showSearchImageModal,
  showSearchRepresentativeImage,
  showRelatedDocumentThumbnail,
  showSearchEvidenceDetails,
  showRelatedDocumentTexts,
  downloadFile,
  clearSearchResults,
  switchSearchType,
  handleSearchImageSelect,
  clearSearchImage,
  cancelCurrentSearch,
  loadDynamicSearchFilters,
  toggleFilterBetween,
  toggleSearchConcept,
  filterSearchConcepts,
  expandSearchConceptCategory,
  clearSearchConcepts
};

// デフォルトエクスポート
export default {
  performSearch,
  performImageSearch,
  displaySearchResults,
  showSearchImageModal,
  showSearchRepresentativeImage,
  showRelatedDocumentThumbnail,
  showSearchEvidenceDetails,
  showRelatedDocumentTexts,
  downloadFile,
  clearSearchResults,
  switchSearchType,
  handleSearchImageSelect,
  clearSearchImage,
  cancelCurrentSearch,
  loadDynamicSearchFilters,
  toggleFilterBetween,
  toggleSearchConcept,
  filterSearchConcepts,
  expandSearchConceptCategory,
  clearSearchConcepts
}

/**
 * 画像モーダルを閉じる
 */
export function closeImageModal() {
  const modal = document.getElementById('imageModal');
  if (!modal) return;
  
  // ESCハンドラーを削除（グローバル変数を参照）
  const escapeHandler = window._imageModalEscapeHandler;
  if (escapeHandler) {
    document.removeEventListener('keydown', escapeHandler);
    window._imageModalEscapeHandler = null;
  }
  
  // 即座に削除（フラッシュを防ぐためアニメーションなし）
  modal.remove();
  
  // 追加の安全策：app.js側のグローバル変数もクリーンアップ
  if (typeof window._imageModalEscapeHandler !== 'undefined') {
    window._imageModalEscapeHandler = null;
  }
}
