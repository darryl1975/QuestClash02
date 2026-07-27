// QuestClash frontend — no framework/build step, plain DOM manipulation.
// Single global `appState` drives a 4-step wizard (upload → configure →
// generate → review) and is mirrored to localStorage after every change
// (persistDraft) so an in-progress quiz survives a reload (see
// checkForDraft/resumeDraft). A second, separate `takeState` drives the
// "take a saved quiz" flow, which is unrelated to the creation wizard.

// ── Global state ─────────────────────────────────────────────────────────
const DRAFT_KEY = 'questclash_draft_v1';
let APP_CONFIG = { question_types: [], question_type_labels: {} };
let appState = {
  sourceDoc: null,        // {filename, ext, text, char_count}
  categoryId: null,
  categoryName: '',
  model: '',
  selectedTypes: [],
  totalCount: 10,
  draftQuestions: [],     // [{_id, question_type, question_text, data, correct_answer, explanation}]
  warnings: [],
  currentStep: 'upload',
};

// ── Init ──────────────────────────────────────────────────────────────────
async function init() {
  try {
    const r = await fetch('/api/config');
    APP_CONFIG = await r.json();
  } catch (_) {}
  renderTypeCheckboxes();
  await loadCategories();
  await loadModels();
  checkForDraft();
}

// Only surface the "resume?" banner when there's a draft with actual
// generated questions in it — an abandoned upload/config with no
// questions yet isn't worth prompting the user to resume.
function checkForDraft() {
  const raw = localStorage.getItem(DRAFT_KEY);
  if (raw) {
    try {
      const saved = JSON.parse(raw);
      if (saved && saved.draftQuestions && saved.draftQuestions.length) {
        document.getElementById('resume-banner').style.display = 'flex';
      }
    } catch (_) {}
  }
}

function resumeDraft() {
  const raw = localStorage.getItem(DRAFT_KEY);
  if (!raw) return;
  appState = JSON.parse(raw); // wholesale replace of the in-memory state
  document.getElementById('resume-banner').style.display = 'none';
  if (appState.sourceDoc) {
    document.getElementById('doc-name').textContent = appState.sourceDoc.filename;
    document.getElementById('doc-name').classList.add('chosen');
    document.getElementById('upload-next-btn').disabled = false;
  }
  goToStep(appState.currentStep === 'upload' ? 'configure' : appState.currentStep);
  if (appState.draftQuestions.length) renderQuestionList();
}

function discardDraft() {
  localStorage.removeItem(DRAFT_KEY);
  document.getElementById('resume-banner').style.display = 'none';
}

function persistDraft() {
  try { localStorage.setItem(DRAFT_KEY, JSON.stringify(appState)); } catch (_) {}
}

function showSavedToast(msg) {
  let el = document.getElementById('saved-toast');
  if (!el) {
    el = document.createElement('div');
    el.id = 'saved-toast';
    el.className = 'saved-toast';
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.classList.add('visible');
  clearTimeout(window._toastTimer);
  window._toastTimer = setTimeout(() => el.classList.remove('visible'), 4000);
}

function showError(msg) {
  const el = document.getElementById('error-banner');
  el.textContent = msg;
  el.style.display = msg ? 'block' : 'none';
  if (msg) window.scrollTo({ top: 0, behavior: 'smooth' });
}

// ── Tabs ──────────────────────────────────────────────────────────────────
function switchTab(name) {
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  document.getElementById('tab-create').classList.toggle('active', name === 'create');
  document.getElementById('tab-saved').classList.toggle('active', name === 'saved');
  if (name === 'saved') loadSavedQuizzes();
}

// ── Wizard step navigation ─────────────────────────────────────────────────
const STEP_ORDER = ['upload', 'configure', 'generate', 'review'];

function goToStep(step) {
  appState.currentStep = step;
  STEP_ORDER.forEach(s => {
    document.getElementById(`step-${s}`).style.display = (s === step) ? 'block' : 'none';
  });
  document.querySelectorAll('.step-dot').forEach(dot => {
    const s = dot.dataset.step;
    dot.classList.remove('active', 'done');
    if (s === step) dot.classList.add('active');
    else if (STEP_ORDER.indexOf(s) < STEP_ORDER.indexOf(step)) dot.classList.add('done');
  });
  showError('');
  persistDraft();
}

// ── Step 1: Upload ─────────────────────────────────────────────────────────
async function onFileChosen() {
  const input = document.getElementById('doc-input');
  const file = input.files[0];
  if (!file) return;
  document.getElementById('doc-name').textContent = file.name;
  document.getElementById('doc-name').classList.add('chosen');
  document.getElementById('upload-status').textContent = 'Extracting text…';
  document.getElementById('upload-next-btn').disabled = true;
  showError('');

  const fd = new FormData();
  fd.append('file', file);
  try {
    const r = await fetch('/api/upload', { method: 'POST', body: fd });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || 'Upload failed');
    appState.sourceDoc = data;
    document.getElementById('upload-status').textContent =
      `Extracted ${data.char_count.toLocaleString()} characters from ${data.filename}.`;
    document.getElementById('upload-next-btn').disabled = false;
    persistDraft();
  } catch (e) {
    showError(e.message);
    document.getElementById('upload-status').textContent = '';
  }
}

// ── Step 2: Configure ───────────────────────────────────────────────────────
async function loadCategories() {
  try {
    const r = await fetch('/api/categories');
    const data = await r.json();
    const sel = document.getElementById('category-select');
    const savedFilter = document.getElementById('saved-category-filter');
    const opts = data.categories.map(c => `<option value="${c.id}">${c.name}</option>`).join('');
    sel.innerHTML = '<option value="">— choose category —</option>' + opts;
    if (savedFilter) savedFilter.innerHTML = '<option value="">All categories</option>' + opts;
    if (appState.categoryId) sel.value = appState.categoryId;
  } catch (e) {
    showError('Could not load categories: ' + e.message);
  }
}

async function createCategory() {
  const name = document.getElementById('new-category-name').value.trim();
  if (!name) return;
  try {
    const r = await fetch('/api/categories', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || 'Could not create category');
    await loadCategories();
    document.getElementById('category-select').value = data.id;
    document.getElementById('new-category-name').value = '';
    checkConfigureReady();
  } catch (e) {
    showError(e.message);
  }
}

function renderTypeCheckboxes() {
  const container = document.getElementById('type-checkboxes');
  const types = APP_CONFIG.question_types.length ? APP_CONFIG.question_types : [
    'multiple_choice', 'multiple_select', 'true_false', 'yes_no', 'fill_blank', 'short_answer', 'ordering',
  ];
  container.innerHTML = types.map(t => `
    <label class="type-card" id="type-card-${t}">
      <input type="checkbox" value="${t}" onchange="onTypeToggle('${t}')">
      <span class="type-label">${APP_CONFIG.question_type_labels[t] || t}</span>
      <span class="type-count" id="type-count-${t}"></span>
    </label>
  `).join('');
}

function onTypeToggle(type) {
  document.getElementById(`type-card-${type}`).classList.toggle(
    'checked', document.querySelector(`#type-card-${type} input`).checked
  );
  renderSplitPreview();
  checkConfigureReady();
}

function getSelectedTypes() {
  return Array.from(document.querySelectorAll('#type-checkboxes input:checked')).map(i => i.value);
}

function splitCount(total, types) {
  const result = {};
  if (!types.length) return result;
  const base = Math.floor(total / types.length);
  const remainder = total % types.length;
  types.forEach((t, i) => { result[t] = base + (i < remainder ? 1 : 0); });
  return result;
}

function renderSplitPreview() {
  const total = parseInt(document.getElementById('total-count').value, 10) || 0;
  const types = getSelectedTypes();
  const dist = splitCount(total, types);
  APP_CONFIG.question_types.forEach(t => {
    const el = document.getElementById(`type-count-${t}`);
    if (el) el.textContent = dist[t] ? `× ${dist[t]}` : '';
  });
  const preview = document.getElementById('split-preview');
  if (!types.length) {
    preview.textContent = 'Select at least one question type.';
  } else {
    preview.textContent = 'Split: ' + types.map(t => `${APP_CONFIG.question_type_labels[t] || t}: ${dist[t]}`).join(' · ');
  }
}

async function loadModels() {
  const sel = document.getElementById('model-select');
  sel.innerHTML = '<option value="">Loading models…</option>';
  try {
    const r = await fetch('/api/models');
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || 'Could not load models');
    window._allModels = data.models;
    renderModelOptions();
  } catch (e) {
    sel.innerHTML = '<option value="">Failed to load models</option>';
    showError(e.message);
  }
}

function renderModelOptions() {
  const sel = document.getElementById('model-select');
  const showAll = document.getElementById('show-all-models').checked;
  const models = (window._allModels || []).filter(m => showAll || m.is_cloud);
  if (!models.length) {
    sel.innerHTML = '<option value="">No models found</option>';
    return;
  }
  sel.innerHTML = models.map(m =>
    `<option value="${m.name}">${m.name}${m.is_cloud ? ' ☁ cloud' : ''}${m.size_label ? ' · ' + m.size_label : ''}</option>`
  ).join('');
  if (appState.model) sel.value = appState.model;
  checkConfigureReady();
}

function checkConfigureReady() {
  const category = document.getElementById('category-select').value;
  const total = parseInt(document.getElementById('total-count').value, 10) || 0;
  const types = getSelectedTypes();
  const model = document.getElementById('model-select').value;
  const ready = category && total >= 1 && total <= 100 && types.length > 0 && model;
  document.getElementById('generate-btn').disabled = !ready;
  return ready;
}

// ── Step 3: Generate ─────────────────────────────────────────────────────
// Reads the /api/generate response as newline-delimited JSON (see
// main.py's StreamingResponse) rather than a single JSON body, so progress
// events can be rendered into the log as they arrive instead of only after
// the whole generation run completes.
async function startGeneration() {
  if (!checkConfigureReady()) return;
  showError('');
  const categorySelect = document.getElementById('category-select');
  appState.categoryId = parseInt(categorySelect.value, 10);
  appState.categoryName = categorySelect.options[categorySelect.selectedIndex].textContent;
  appState.model = document.getElementById('model-select').value;
  appState.selectedTypes = getSelectedTypes();
  appState.totalCount = parseInt(document.getElementById('total-count').value, 10);

  goToStep('generate');
  document.getElementById('progress-fill').style.width = '0%';
  document.getElementById('generate-log').textContent = '';
  document.getElementById('generate-status').textContent = 'Starting…';

  const body = {
    source_text: appState.sourceDoc.text,
    category: appState.categoryName,
    model: appState.model,
    selected_types: appState.selectedTypes,
    total_count: appState.totalCount,
  };

  let typeDistribution = {};
  let doneTypes = 0;

  try {
    const resp = await fetch('/api/generate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      throw new Error(data.detail || `Generation failed (HTTP ${resp.status})`);
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    const log = document.getElementById('generate-log');

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let lines = buffer.split('\n');
      buffer = lines.pop(); // last "line" may be a partial chunk — keep it for next read
      for (const line of lines) {
        if (!line.trim()) continue;
        const event = JSON.parse(line);
        handleGenerateEvent(event);
      }
    }

    function handleGenerateEvent(event) {
      if (event.event === 'plan') {
        typeDistribution = event.type_distribution;
        log.textContent += `Plan: ${JSON.stringify(typeDistribution)}\n`;
      } else if (event.event === 'attempt') {
        log.textContent += `[${event.type}] attempt ${event.attempt}/${event.max_attempts}…\n`;
        document.getElementById('generate-status').textContent = `Generating ${event.type} (attempt ${event.attempt})…`;
      } else if (event.event === 'type_done') {
        doneTypes += 1;
        log.textContent += `[${event.type}] done: ${event.count} question(s) in ${event.attempts} attempt(s).\n`;
        const total = Object.keys(typeDistribution).length || 1;
        document.getElementById('progress-fill').style.width = `${Math.round((doneTypes / total) * 100)}%`;
      } else if (event.event === 'warning') {
        log.textContent += `⚠ ${event.message}\n`;
        appState.warnings.push(event.message);
      } else if (event.event === 'error') {
        throw new Error(event.message);
      } else if (event.event === 'done') {
        appState.draftQuestions = event.questions.map((q, i) => ({ ...q, _id: `q-${Date.now()}-${i}` }));
        appState.warnings = event.warnings || [];
        document.getElementById('progress-fill').style.width = '100%';
        document.getElementById('generate-status').textContent = 'Done.';
      }
      log.scrollTop = log.scrollHeight;
    }

    if (!appState.draftQuestions.length) {
      throw new Error('Generation finished but produced no valid questions.');
    }
    persistDraft();
    renderQuestionList();
    goToStep('review');
  } catch (e) {
    showError('Generation failed: ' + e.message);
    goToStep('configure');
  }
}

// ── Step 4: Review / Edit ───────────────────────────────────────────────────
function renderQuestionList() {
  const container = document.getElementById('question-list');
  const meta = document.getElementById('review-meta');
  meta.textContent = `${appState.categoryName} · ${appState.model} · ${appState.draftQuestions.length} question(s)`;

  const warnBanner = document.getElementById('warning-banner');
  if (appState.warnings && appState.warnings.length) {
    warnBanner.textContent = appState.warnings.join('\n');
    warnBanner.style.display = 'block';
  } else {
    warnBanner.style.display = 'none';
  }

  container.innerHTML = appState.draftQuestions.map((q, idx) => renderQuestionCard(q, idx)).join('');
}

function label(type) { return APP_CONFIG.question_type_labels[type] || type; }

function renderQuestionCard(q, idx) {
  let body = '';
  if (q.question_type === 'multiple_choice') {
    body = (q.data.options || []).map((opt, oi) => `
      <div class="option-row">
        <input type="radio" name="correct-${q._id}" ${q.correct_answer.correct_index === oi ? 'checked' : ''}
          onchange="setField('${q._id}', 'correct_answer.correct_index', ${oi})">
        <input type="text" value="${escapeAttr(opt)}" oninput="setOption('${q._id}', ${oi}, this.value)">
      </div>`).join('');
  } else if (q.question_type === 'multiple_select') {
    body = (q.data.options || []).map((opt, oi) => `
      <div class="option-row">
        <input type="checkbox" ${((q.correct_answer.correct_indices || []).includes(oi)) ? 'checked' : ''}
          onchange="toggleMultiSelect('${q._id}', ${oi}, this.checked)">
        <input type="text" value="${escapeAttr(opt)}" oninput="setOption('${q._id}', ${oi}, this.value)">
      </div>`).join('');
  } else if (q.question_type === 'true_false') {
    const val = q.correct_answer.value;
    body = `<div class="tf-toggle">
      <button type="button" class="${val === true ? 'selected' : ''}" onclick="setField('${q._id}', 'correct_answer.value', true)">True</button>
      <button type="button" class="${val === false ? 'selected' : ''}" onclick="setField('${q._id}', 'correct_answer.value', false)">False</button>
    </div>`;
  } else if (q.question_type === 'yes_no') {
    const val = q.correct_answer.value;
    body = `<div class="yn-toggle">
      <button type="button" class="${val === 'yes' ? 'selected' : ''}" onclick="setField('${q._id}', 'correct_answer.value', 'yes')">Yes</button>
      <button type="button" class="${val === 'no' ? 'selected' : ''}" onclick="setField('${q._id}', 'correct_answer.value', 'no')">No</button>
    </div>`;
  } else if (q.question_type === 'fill_blank') {
    body = `
      <div class="field-label">Correct answer</div>
      <input type="text" value="${escapeAttr(q.correct_answer.correct_answer || '')}"
        oninput="setField('${q._id}', 'correct_answer.correct_answer', this.value)">
      <div class="field-label">Acceptable variants (comma separated)</div>
      <input type="text" value="${escapeAttr((q.correct_answer.acceptable_answers || []).join(', '))}"
        oninput="setField('${q._id}', 'correct_answer.acceptable_answers', this.value.split(',').map(s => s.trim()).filter(Boolean))">
    `;
  } else if (q.question_type === 'short_answer') {
    body = `
      <div class="field-label">Model answer</div>
      <textarea rows="2" oninput="setField('${q._id}', 'correct_answer.model_answer', this.value)">${escapeHtml(q.correct_answer.model_answer || '')}</textarea>
      <div class="field-label">Keywords (comma separated)</div>
      <input type="text" value="${escapeAttr((q.correct_answer.keywords || []).join(', '))}"
        oninput="setField('${q._id}', 'correct_answer.keywords', this.value.split(',').map(s => s.trim()).filter(Boolean))">
    `;
  } else if (q.question_type === 'ordering') {
    const items = q.data.items || [];
    body = `<div class="field-label">Correct sequence (top = first step)</div>` + items.map((item, oi) => `
      <div class="order-row">
        <span class="order-index">${oi + 1}</span>
        <input type="text" value="${escapeAttr(item)}" oninput="setOrderItem('${q._id}', ${oi}, this.value)">
        <div class="order-btns">
          <button type="button" onclick="moveOrderItem('${q._id}', ${oi}, -1)" ${oi === 0 ? 'disabled' : ''}>↑</button>
          <button type="button" onclick="moveOrderItem('${q._id}', ${oi}, 1)" ${oi === items.length - 1 ? 'disabled' : ''}>↓</button>
        </div>
      </div>`).join('');
  }

  return `
    <div class="question-card" data-id="${q._id}">
      <div class="q-head">
        <span class="q-type-badge">${label(q.question_type)}</span>
        <button class="delete-q-btn" onclick="deleteQuestion('${q._id}')">🗑 Delete</button>
      </div>
      <textarea class="q-text" rows="2" oninput="setField('${q._id}', 'question_text', this.value)">${escapeHtml(q.question_text)}</textarea>
      ${body}
      <div class="explanation-field">
        <div class="field-label">Explanation (optional)</div>
        <textarea rows="1" oninput="setField('${q._id}', 'explanation', this.value)">${escapeHtml(q.explanation || '')}</textarea>
      </div>
    </div>
  `;
}

function findQuestion(id) { return appState.draftQuestions.find(q => q._id === id); }

// Generic dot-path setter so renderQuestionCard's inline handlers can write
// straight to nested fields (e.g. 'correct_answer.value') without a
// dedicated setter per question type.
function setField(id, path, value) {
  const q = findQuestion(id);
  if (!q) return;
  const parts = path.split('.');
  let obj = q;
  for (let i = 0; i < parts.length - 1; i++) obj = obj[parts[i]];
  obj[parts[parts.length - 1]] = value;
  persistDraft();
}

function setOption(id, optIdx, value) {
  const q = findQuestion(id);
  if (!q) return;
  q.data.options[optIdx] = value;
  persistDraft();
}

function toggleMultiSelect(id, optIdx, checked) {
  const q = findQuestion(id);
  if (!q) return;
  let indices = new Set(q.correct_answer.correct_indices || []);
  if (checked) indices.add(optIdx); else indices.delete(optIdx);
  q.correct_answer.correct_indices = Array.from(indices).sort((a, b) => a - b);
  persistDraft();
}

function setOrderItem(id, idx, value) {
  const q = findQuestion(id);
  if (!q) return;
  q.data.items[idx] = value;
  persistDraft();
}

function moveOrderItem(id, idx, dir) {
  const q = findQuestion(id);
  if (!q) return;
  const items = q.data.items;
  const j = idx + dir;
  if (j < 0 || j >= items.length) return;
  [items[idx], items[j]] = [items[j], items[idx]];
  renderQuestionList();
  persistDraft();
}

function deleteQuestion(id) {
  appState.draftQuestions = appState.draftQuestions.filter(q => q._id !== id);
  renderQuestionList();
  persistDraft();
}

function escapeHtml(s) {
  return String(s || '').replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
}
function escapeAttr(s) {
  return escapeHtml(s).replace(/"/g, '&quot;');
}

// ── Save ─────────────────────────────────────────────────────────────────
async function saveQuiz() {
  if (!appState.draftQuestions.length) {
    showError('No questions to save.');
    return;
  }
  const payload = {
    title: `${appState.categoryName} quiz — ${appState.sourceDoc.filename}`,
    category_id: appState.categoryId,
    source_filename: appState.sourceDoc.filename,
    source_file_ext: appState.sourceDoc.ext,
    source_text_excerpt: appState.sourceDoc.text,
    source_char_count: appState.sourceDoc.char_count,
    model_used: appState.model,
    requested_total_count: appState.totalCount,
    type_distribution: splitCount(appState.totalCount, appState.selectedTypes),
    generation_warnings: appState.warnings || [],
    questions: appState.draftQuestions.map(q => ({
      question_type: q.question_type,
      question_text: q.question_text,
      data: q.data,
      correct_answer: q.correct_answer,
      explanation: q.explanation || null,
    })),
  };
  try {
    const r = await fetch('/api/save', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || 'Save failed');
    localStorage.removeItem(DRAFT_KEY);
    showSavedToast(`Quiz saved: "${data.title}" (${data.questions.length} question(s)).`);
    appState = {
      sourceDoc: null, categoryId: null, categoryName: '', model: '',
      selectedTypes: [], totalCount: 10, draftQuestions: [], warnings: [], currentStep: 'upload',
    };
    document.getElementById('doc-name').textContent = 'No file chosen';
    document.getElementById('doc-name').classList.remove('chosen');
    document.getElementById('doc-input').value = '';
    document.getElementById('upload-status').textContent = '';
    document.getElementById('upload-next-btn').disabled = true;
    goToStep('upload');
    switchTab('saved');
  } catch (e) {
    showError('Could not save: ' + e.message);
  }
}

// ── Saved Quizzes tab ────────────────────────────────────────────────────
async function loadSavedQuizzes() {
  const categoryId = document.getElementById('saved-category-filter').value;
  const container = document.getElementById('saved-quiz-list');
  container.innerHTML = '<div class="empty-note">Loading…</div>';
  try {
    const params = categoryId ? `?category_id=${categoryId}` : '';
    const r = await fetch(`/api/quizzes${params}`);
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || 'Could not load quizzes');
    if (!data.quizzes.length) {
      container.innerHTML = '<div class="empty-note">No saved quizzes yet.</div>';
      return;
    }
    container.innerHTML = data.quizzes.map(q => `
      <div class="saved-quiz-row" onclick="openSavedDetail(${q.id})">
        <div>
          <div class="title">${escapeHtml(q.title)}</div>
          <div class="sub">${escapeHtml(q.category_name)} · ${q.question_count} question(s) · ${escapeHtml(q.model_used)} · ${new Date(q.created_at).toLocaleString()}</div>
        </div>
      </div>
    `).join('');
  } catch (e) {
    container.innerHTML = `<div class="empty-note">${escapeHtml(e.message)}</div>`;
  }
}

let currentQuizId = null;

async function openSavedDetail(quizId) {
  try {
    const r = await fetch(`/api/quizzes/${quizId}`);
    const quiz = await r.json();
    if (!r.ok) throw new Error(quiz.detail || 'Could not load quiz');
    currentQuizId = quizId;
    document.getElementById('saved-detail-title').textContent = quiz.title;
    document.getElementById('saved-detail-meta').textContent =
      `${quiz.category_name} · ${quiz.model_used} · ${quiz.questions.length} question(s) · ${new Date(quiz.created_at).toLocaleString()}`;
    document.getElementById('saved-detail-questions').innerHTML = quiz.questions.map(renderQuestionPreview).join('');
    document.getElementById('saved-quiz-detail').style.display = 'block';
    document.getElementById('saved-quiz-detail').scrollIntoView({ behavior: 'smooth' });
    await loadPastAttempts(quizId);
  } catch (e) {
    showError(e.message);
  }
}

function closeSavedDetail() {
  document.getElementById('saved-quiz-detail').style.display = 'none';
}

async function loadPastAttempts(quizId) {
  const container = document.getElementById('past-attempts-list');
  try {
    const r = await fetch(`/api/quizzes/${quizId}/attempts`);
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || 'Could not load attempts');
    if (!data.attempts.length) {
      container.innerHTML = '<div class="empty-note">No attempts yet.</div>';
      return;
    }
    container.innerHTML = data.attempts.map(a => `
      <div class="attempt-row" onclick="viewPastAttempt(${a.attempt_id})">
        <span>${new Date(a.started_at).toLocaleString()}${a.pending_self_grade ? ' · pending self-grade' : ''}</span>
        <span class="attempt-score">${a.score}/${a.total_questions} (${a.percentage}%)</span>
      </div>
    `).join('');
  } catch (e) {
    container.innerHTML = `<div class="empty-note">${escapeHtml(e.message)}</div>`;
  }
}

async function viewPastAttempt(attemptId) {
  try {
    const r = await fetch(`/api/attempts/${attemptId}`);
    const attempt = await r.json();
    if (!r.ok) throw new Error(attempt.detail || 'Could not load attempt');
    renderAttemptResults(attempt);
  } catch (e) {
    showError(e.message);
  }
}

// ── Take Quiz (one question at a time, full-page) ──────────────────────────
let takeState = null;

function showQuizAttemptPage() {
  document.body.classList.add('attempt-mode');
  window.scrollTo(0, 0);
}

function hideQuizAttemptPage() {
  document.body.classList.remove('attempt-mode');
}

async function startTakeQuiz() {
  if (!currentQuizId) return;
  try {
    const r = await fetch(`/api/quizzes/${currentQuizId}/take`);
    const quiz = await r.json();
    if (!r.ok) throw new Error(quiz.detail || 'Could not load quiz');
    // Ordering questions start pre-seeded with the given (shuffled) item
    // order as the user's current answer, since "leave it as shown" is a
    // valid answer for this type and there's no natural "unanswered" state.
    const userAnswers = {};
    quiz.questions.forEach(q => {
      if (q.question_type === 'ordering') {
        userAnswers[q.question_id] = { order: q.data.items.map((_, i) => i) };
      }
    });
    takeState = { quizId: quiz.id, title: quiz.title, questions: quiz.questions, userAnswers, currentIndex: 0 };
    document.getElementById('take-quiz-title').textContent = quiz.title;
    document.getElementById('attempt-results-panel').style.display = 'none';
    document.getElementById('take-quiz-panel').style.display = 'block';
    showQuizAttemptPage();
    renderCurrentTakeQuestion();
  } catch (e) {
    showError(e.message);
  }
}

function cancelTakeQuiz() {
  takeState = null;
  hideQuizAttemptPage();
}

function isTakeQuestionAnswered(q) {
  const a = takeState.userAnswers[q.question_id];
  if (!a) return false;
  if (q.question_type === 'multiple_choice') return a.selected_index !== undefined;
  if (q.question_type === 'multiple_select') return (a.selected_indices || []).length > 0;
  if (q.question_type === 'true_false' || q.question_type === 'yes_no') return a.value !== undefined;
  if (q.question_type === 'fill_blank' || q.question_type === 'short_answer') return !!(a.text && a.text.trim());
  if (q.question_type === 'ordering') return !!a.order;
  return false;
}

function updateTakeNextButton() {
  const q = takeState.questions[takeState.currentIndex];
  document.getElementById('take-quiz-next-btn').disabled = !isTakeQuestionAnswered(q);
}

function renderCurrentTakeQuestion() {
  const idx = takeState.currentIndex;
  const total = takeState.questions.length;
  const q = takeState.questions[idx];
  document.getElementById('take-quiz-questions').innerHTML = renderTakeQuestion(q, idx);
  document.getElementById('take-quiz-progress').textContent = `Question ${idx + 1} of ${total}`;
  document.getElementById('take-quiz-progress-fill').style.width = `${(idx / total) * 100}%`;
  const isLast = idx === total - 1;
  const nextBtn = document.getElementById('take-quiz-next-btn');
  nextBtn.textContent = isLast ? 'Submit Quiz →' : 'Next →';
  updateTakeNextButton();
}

function handleTakeQuizNext() {
  if (takeState.currentIndex === takeState.questions.length - 1) {
    submitAttempt();
  } else {
    takeState.currentIndex++;
    renderCurrentTakeQuestion();
  }
}

function renderTakeQuestion(q, idx) {
  let body = '';
  if (q.question_type === 'multiple_choice') {
    body = (q.data.options || []).map((opt, oi) => `
      <div class="take-option-row" onclick="setTakeAnswer(${q.question_id}, {selected_index: ${oi}})">
        <input type="radio" name="take-${q.question_id}" ${isTakeSelected(q.question_id, 'selected_index', oi) ? 'checked' : ''}>
        <span>${escapeHtml(opt)}</span>
      </div>`).join('');
  } else if (q.question_type === 'multiple_select') {
    body = (q.data.options || []).map((opt, oi) => `
      <div class="take-option-row" onclick="toggleTakeMultiSelect(${q.question_id}, ${oi})">
        <input type="checkbox" ${isTakeMultiSelected(q.question_id, oi) ? 'checked' : ''}>
        <span>${escapeHtml(opt)}</span>
      </div>`).join('');
  } else if (q.question_type === 'true_false') {
    const val = (takeState.userAnswers[q.question_id] || {}).value;
    body = `<div class="tf-toggle">
      <button type="button" class="${val === true ? 'selected' : ''}" onclick="setTakeAnswer(${q.question_id}, {value: true})">True</button>
      <button type="button" class="${val === false ? 'selected' : ''}" onclick="setTakeAnswer(${q.question_id}, {value: false})">False</button>
    </div>`;
  } else if (q.question_type === 'yes_no') {
    const val = (takeState.userAnswers[q.question_id] || {}).value;
    body = `<div class="yn-toggle">
      <button type="button" class="${val === 'yes' ? 'selected' : ''}" onclick="setTakeAnswer(${q.question_id}, {value: 'yes'})">Yes</button>
      <button type="button" class="${val === 'no' ? 'selected' : ''}" onclick="setTakeAnswer(${q.question_id}, {value: 'no'})">No</button>
    </div>`;
  } else if (q.question_type === 'fill_blank') {
    const val = (takeState.userAnswers[q.question_id] || {}).text || '';
    body = `<input type="text" placeholder="Your answer…" value="${escapeAttr(val)}"
      oninput="setTakeAnswerText(${q.question_id}, this.value)">`;
  } else if (q.question_type === 'short_answer') {
    const val = (takeState.userAnswers[q.question_id] || {}).text || '';
    body = `<textarea rows="3" placeholder="Your answer…" oninput="setTakeAnswerText(${q.question_id}, this.value)">${escapeHtml(val)}</textarea>`;
  } else if (q.question_type === 'ordering') {
    const order = (takeState.userAnswers[q.question_id] || {}).order || q.data.items.map((_, i) => i);
    body = order.map((itemIdx, pos) => `
      <div class="order-row">
        <span class="order-index">${pos + 1}</span>
        <span style="flex:1">${escapeHtml(q.data.items[itemIdx])}</span>
        <div class="order-btns">
          <button type="button" onclick="moveTakeOrder(${q.question_id}, ${pos}, -1)" ${pos === 0 ? 'disabled' : ''}>↑</button>
          <button type="button" onclick="moveTakeOrder(${q.question_id}, ${pos}, 1)" ${pos === order.length - 1 ? 'disabled' : ''}>↓</button>
        </div>
      </div>`).join('');
  }
  return `
    <div class="take-q-card">
      <div class="q-index">${label(q.question_type)}</div>
      <div class="q-text">${escapeHtml(q.question_text)}</div>
      ${body}
    </div>
  `;
}

function isTakeSelected(qid, field, value) {
  const a = takeState.userAnswers[qid];
  return a && a[field] === value;
}
function isTakeMultiSelected(qid, optIdx) {
  const a = takeState.userAnswers[qid];
  return a && (a.selected_indices || []).includes(optIdx);
}

function setTakeAnswer(qid, value) {
  takeState.userAnswers[qid] = value;
  renderCurrentTakeQuestion();
}

function setTakeAnswerText(qid, text) {
  takeState.userAnswers[qid] = { text };
  updateTakeNextButton();
}

function toggleTakeMultiSelect(qid, optIdx) {
  const current = takeState.userAnswers[qid] || { selected_indices: [] };
  const set = new Set(current.selected_indices || []);
  if (set.has(optIdx)) set.delete(optIdx); else set.add(optIdx);
  takeState.userAnswers[qid] = { selected_indices: Array.from(set).sort((a, b) => a - b) };
  renderCurrentTakeQuestion();
}

function moveTakeOrder(qid, pos, dir) {
  const q = takeState.questions.find(q => q.question_id === qid);
  const order = (takeState.userAnswers[qid] || {}).order || q.data.items.map((_, i) => i);
  const j = pos + dir;
  if (j < 0 || j >= order.length) return;
  [order[pos], order[j]] = [order[j], order[pos]];
  takeState.userAnswers[qid] = { order };
  renderCurrentTakeQuestion();
}

async function submitAttempt() {
  const answers = takeState.questions.map(q => ({
    question_id: q.question_id,
    user_answer: takeState.userAnswers[q.question_id] || {},
  }));
  showError('');
  try {
    const r = await fetch(`/api/quizzes/${takeState.quizId}/attempts`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ answers }),
    });
    const result = await r.json();
    if (!r.ok) throw new Error(result.detail || 'Could not submit attempt');
    renderAttemptResults(result);
    await loadPastAttempts(takeState.quizId);
  } catch (e) {
    showError(e.message);
  }
}

// ── Attempt Results ──────────────────────────────────────────────────────
// pendingGrades stages the user's self-grade choices for short_answer
// questions locally (keyed by attempt_answer_id) before finalizeSelfGrade()
// sends them all to PATCH /api/attempts/{id}/self-grade in one call — the
// score shown is not final until that happens.
let currentAttempt = null;
let pendingGrades = {};

function renderAttemptResults(attempt) {
  currentAttempt = attempt;
  pendingGrades = {};
  showQuizAttemptPage();
  document.getElementById('take-quiz-panel').style.display = 'none';
  const banner = document.getElementById('score-banner');
  banner.innerHTML = `${attempt.score} / ${attempt.total_questions} correct — ${attempt.percentage}%` +
    (attempt.pending_self_grade ? `<div class="score-sub">${attempt.pending_self_grade} short-answer question(s) awaiting self-grade</div>` : '');

  document.getElementById('attempt-results-list').innerHTML = attempt.items.map(renderAttemptItem).join('');
  document.getElementById('finalize-row').style.display = attempt.pending_self_grade ? 'flex' : 'none';
  document.getElementById('attempt-results-panel').style.display = 'block';
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function resultOptionRow(text, isCorrect, isUserWrong) {
  const cls = isCorrect ? 'correct' : isUserWrong ? 'incorrect' : '';
  const tag = isCorrect ? '✅ Correct answer' : isUserWrong ? '❌ Your answer' : '';
  return `<div class="result-option-row ${cls}"><span>${escapeHtml(text)}</span>${tag ? `<span class="tag">${tag}</span>` : ''}</div>`;
}

function renderAttemptItem(item) {
  const state = item.is_correct === true ? 'correct' : item.is_correct === false ? 'incorrect' : 'pending';
  const icon = state === 'correct' ? '✅' : state === 'incorrect' ? '❌' : '🕒';
  const opts = item.data.options || [];
  let body = '';

  if (item.question_type === 'multiple_choice') {
    const correctIdx = item.correct_answer.correct_index;
    const userIdx = item.user_answer.selected_index;
    body = opts.map((opt, oi) => resultOptionRow(opt, oi === correctIdx, oi === userIdx && oi !== correctIdx)).join('');
  } else if (item.question_type === 'multiple_select') {
    const correctSet = new Set(item.correct_answer.correct_indices || []);
    const userSet = new Set(item.user_answer.selected_indices || []);
    body = opts.map((opt, oi) => resultOptionRow(opt, correctSet.has(oi), userSet.has(oi) && !correctSet.has(oi))).join('');
  } else if (item.question_type === 'true_false') {
    const correctVal = item.correct_answer.value;
    const userVal = item.user_answer.value;
    body = [true, false].map(v => resultOptionRow(v ? 'True' : 'False', v === correctVal, v === userVal && v !== correctVal)).join('');
  } else if (item.question_type === 'yes_no') {
    const correctVal = item.correct_answer.value;
    const userVal = item.user_answer.value;
    body = ['yes', 'no'].map(v => resultOptionRow(v === 'yes' ? 'Yes' : 'No', v === correctVal, v === userVal && v !== correctVal)).join('');
  } else if (item.question_type === 'fill_blank') {
    const userText = item.user_answer.text || '(no answer)';
    body = `<div class="result-answer-box ${state}">Your answer: ${escapeHtml(userText)}</div>` +
      (state !== 'correct' ? `<div class="result-correct-note">Correct answer: ${escapeHtml(item.correct_answer.correct_answer || '')}${(item.correct_answer.acceptable_answers || []).length ? ' (also: ' + item.correct_answer.acceptable_answers.map(escapeHtml).join(', ') + ')' : ''}</div>` : '');
  } else if (item.question_type === 'short_answer') {
    const userText = item.user_answer.text || '(no answer)';
    body = `<div class="result-answer-box ${state}">Your answer: ${escapeHtml(userText)}</div>` +
      `<div class="result-correct-note">Model answer: ${escapeHtml(item.correct_answer.model_answer || '')}</div>`;
  } else if (item.question_type === 'ordering') {
    const items = item.data.items || [];
    const userOrder = (item.user_answer.order || []).map(i => items[i]).filter(Boolean);
    body = `<div class="field-label">Your order</div>` +
      userOrder.map((s, i) => `<div class="order-row ${state}"><span class="order-index">${i + 1}</span><span style="flex:1">${escapeHtml(s)}</span></div>`).join('') +
      (state !== 'correct'
        ? `<div class="field-label" style="margin-top:.8rem">Correct order</div>` +
          (item.correct_answer.correct_order || []).map(i => items[i]).filter(Boolean).map((s, i) => `<div class="order-row correct"><span class="order-index">${i + 1}</span><span style="flex:1">${escapeHtml(s)}</span></div>`).join('')
        : '');
  }

  let selfGradeHtml = '';
  if (state === 'pending') {
    const choice = pendingGrades[item.attempt_answer_id];
    selfGradeHtml = `
      <div class="button-row" style="margin-top:.6rem">
        <button class="btn-small ${choice === true ? 'btn-primary' : 'btn-ghost'}" onclick="setSelfGrade(${item.attempt_answer_id}, true)">Mark Correct</button>
        <button class="btn-small ${choice === false ? 'btn-danger' : 'btn-ghost'}" onclick="setSelfGrade(${item.attempt_answer_id}, false)">Mark Incorrect</button>
      </div>`;
  }

  return `
    <div class="question-card attempt-answer-card ${state}">
      <div class="q-head"><span class="q-type-badge">${label(item.question_type)}</span></div>
      <div style="font-weight:600;margin-bottom:.5rem"><span class="result-icon">${icon}</span>${escapeHtml(item.question_text)}</div>
      ${body}
      ${item.explanation ? `<div class="explanation-field" style="color:#718096;font-size:.8rem">${escapeHtml(item.explanation)}</div>` : ''}
      ${selfGradeHtml}
    </div>
  `;
}

function setSelfGrade(attemptAnswerId, isCorrect) {
  pendingGrades[attemptAnswerId] = isCorrect;
  document.getElementById('attempt-results-list').innerHTML = currentAttempt.items.map(renderAttemptItem).join('');
}

async function finalizeSelfGrade() {
  const results = Object.entries(pendingGrades).map(([id, is_correct]) => ({
    attempt_answer_id: parseInt(id, 10), is_correct,
  }));
  if (!results.length) {
    showError('Mark at least one short-answer question before finalizing.');
    return;
  }
  try {
    const r = await fetch(`/api/attempts/${currentAttempt.attempt_id}/self-grade`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ results }),
    });
    const attempt = await r.json();
    if (!r.ok) throw new Error(attempt.detail || 'Could not finalize score');
    renderAttemptResults(attempt);
    if (currentQuizId) await loadPastAttempts(currentQuizId);
  } catch (e) {
    showError(e.message);
  }
}

function closeAttemptResults() {
  document.getElementById('attempt-results-panel').style.display = 'none';
  hideQuizAttemptPage();
}

function renderQuestionPreview(q) {
  return `
    <div class="question-card">
      <div class="q-head"><span class="q-type-badge">${label(q.question_type)}</span></div>
      <div style="font-weight:600">${escapeHtml(q.question_text)}</div>
    </div>
  `;
}

init();
