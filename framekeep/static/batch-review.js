'use strict';

let categoryBatchReview = null;
const suggestedCategories = ['Photos', 'Screenshots', 'Illustrations'];

async function previewAllSuggestions() {
  const scope = {...filters(), kind:'Needs review'};
  const title = 'Accept all AI suggestions';
  modal(title, '<p id="accept-all-loading" role="status">Counting suggestions in this view…</p>');
  const loading = $('#accept-all-loading');
  const preview = await post('/review/accept-all/preview', {filters:scope});
  if (!loading.isConnected || !$('#modal').open) return;
  if (!preview.total) {
    modal(title, '<p>There are no AI suggestions to accept in this view.</p>'
      + '<div class="modal-actions"><button class="button primary" data-action="close-modal">Done</button></div>');
    return;
  }
  const counts = suggestedCategories.filter(kind => preview.counts[kind]).map(kind => '<li><span>' + kind
    + '</span><strong>' + number(preview.counts[kind]) + '</strong></li>').join('');
  modal(title, '<p>Apply the suggested categories to ' + quantity(preview.total, 'photo')
    + ' matching the current filters, across all pages.</p><ul class="accept-all-counts">' + counts + '</ul>'
    + (preview.older ? '<p class="field-hint">Includes ' + quantity(preview.older, 'suggestion') + ' from an earlier model.</p>' : '')
    + '<p>You can undo this as one batch from Batch history.</p>'
    + actions('accept-all-confirm', 'Accept ' + quantity(preview.total, 'suggestion')));
  const button = $('#accept-all-confirm');
  button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      const result = await post('/review/accept-all', {filters:scope, revision:preview.revision});
      if (button.isConnected) $('#modal').close();
      await updateGalleryAfterCategoryChange();
      toast('Saved ' + quantity(result.updated, 'category', 'categories') + '. Undo from Batch history.');
    } catch (error) {
      if (button.isConnected) button.disabled = false;
      modalError(error);
    }
  });
}

function batchModal(title, body) {
  modal(title, '<div id="batch-review-panel">' + body + '</div>');
}

async function openCategoryBatches() {
  categoryBatchReview = {scope:{...filters(), kind:'Needs review'}, page:null, request:0};
  await loadCategoryBatch();
}

function isBatchOpen(review, request) {
  return categoryBatchReview === review && review.request === request && $('#modal').open && $('#batch-review-panel');
}

async function loadCategoryBatch(kind = null, offset = 0) {
  const review = categoryBatchReview;
  const request = ++review.request;
  batchModal('Review in batches', '<p role="status">Loading category suggestions…</p>');
  const page = await post('/review/batch-preview', {filters:review.scope, kind, offset});
  if (!isBatchOpen(review, request)) return;
  review.page = page;
  const uncertain = Object.values(page.suggestions).reduce((total, count) => total + count, 0);
  const analysisButtons = (page.pending ? '<button class="button outline small" data-batch-analyze="new">Analyze pending</button>' : '')
    + (page.older ? '<button class="button outline small" data-batch-analyze="older">Refresh ' + number(page.older) + ' older suggestions</button>' : '');
  const groups = page.groups.map(group => '<label class="batch-card" data-batch-card>'
    + '<input type="checkbox" data-batch-group="' + group.id + '" aria-label="Include ' + esc(group.filename) + '" disabled>'
    + '<img data-media="/api/photos/' + group.id + '/thumbnail" alt="' + esc(group.filename) + '">'
    + '<strong data-copyable>' + esc(group.filename) + '</strong><small>' + (group.count > 1 ? number(group.count) + ' identical files' : '1 photo')
    + (group.older ? ' · Older suggestion' : '') + '</small></label>').join('');
  batchModal('Review in batches', '<div class="batch-queues"><div><strong>' + number(page.pending) + '</strong> awaiting analysis</div>'
    + '<div><strong>' + number(uncertain) + '</strong> AI unsure</div></div>'
    + '<div class="batch-tools">' + analysisButtons + '<button class="text-button" data-batch-history="0">Batch history</button></div>'
    + '<label class="field-label" for="batch-kind">Suggested category</label><select class="text-input" id="batch-kind">'
    + suggestedCategories.map(label => '<option' + (label === page.kind ? ' selected' : '') + ' value="' + label + '">'
      + label + ' · ' + number(page.suggestions[label]) + '</option>').join('') + '</select>'
    + (page.groups.length ? '<p>Choose the previews that fit this category. A checked group includes all its identical files in this view.</p>'
      + '<div class="batch-tools"><button class="text-button" id="batch-select-all" disabled>Select all previews</button>'
      + '<button class="text-button" id="batch-clear" disabled>Clear selection</button></div>'
      + '<div class="batch-grid">' + groups + '</div>'
      + '<div class="batch-tools batch-pages"><button class="button outline small" data-batch-page="' + Math.max(0, page.offset - page.page_size) + '"'
      + (page.offset === 0 ? ' disabled' : '') + '>Previous</button><span>' + number(page.offset + 1) + '–' + number(page.offset + page.groups.length)
      + ' of ' + number(page.total) + ' groups</span><button class="button outline small" data-batch-page="' + (page.offset + page.page_size) + '"'
      + (page.offset + page.groups.length >= page.total ? ' disabled' : '') + '>Next</button></div>'
      : '<p>No suggestions for this category in the current view.</p>')
    + '<div class="modal-actions"><button class="button outline" id="batch-reload">Refresh preview</button>'
    + '<button class="button outline" data-action="close-modal">Done</button>'
    + (page.groups.length ? '<button class="button primary" id="batch-approve" disabled>Approve selected</button>' : '') + '</div>');
  for (const card of $$('[data-batch-card]')) {
    const checkbox = $('input', card);
    const image = $('img', card);
    image.addEventListener('load', () => {
      checkbox.disabled = false;
      updateBatchSelection();
    });
    const unavailable = () => {card.dataset.unavailable = ''; card.title = 'Preview unavailable. Rescan the source to try again.';};
    image.addEventListener('error', unavailable);
    image.addEventListener('previewunavailable', unavailable);
  }
}

function updateBatchSelection() {
  const approve = $('#batch-approve');
  if (!approve) return;
  const boxes = $$('[data-batch-group]:enabled');
  const selected = new Set(boxes.filter(box => box.checked).map(box => Number(box.dataset.batchGroup)));
  const count = categoryBatchReview.page.groups.reduce((total, group) => total + (selected.has(group.id) ? group.count : 0), 0);
  $('#batch-select-all').disabled = !boxes.length || selected.size === boxes.length;
  $('#batch-clear').disabled = !selected.size;
  approve.disabled = !count;
  approve.textContent = count ? 'Approve ' + quantity(count, 'photo') : 'Approve selected';
}

async function showBatchHistory(offset = 0) {
  const review = categoryBatchReview;
  const request = ++review.request;
  batchModal('Batch history', '<p role="status">Loading approved batches…</p>');
  const data = await api('/review/batch-history?offset=' + offset);
  if (!isBatchOpen(review, request)) return;
  const rows = data.batches.map(batch => '<div class="batch-history-row"><div><strong>' + esc(batch.kind) + '</strong><small>'
    + quantity(batch.item_count, 'photo') + ' · ' + esc(dateLabel(batch.created_at))
    + (batch.remaining !== batch.item_count ? ' · ' + number(batch.remaining) + ' still reversible' : '')
    + '</small></div><button class="button outline small" data-batch-undo="' + esc(batch.id) + '"'
    + (!batch.remaining ? ' disabled' : '') + '>Undo batch</button></div>').join('');
  batchModal('Batch history', '<p>Undo returns photos to their automatic categories. Later manual edits are kept.</p>'
    + (rows || '<p>No approved batches on this page.</p>')
    + '<div class="modal-actions"><button class="button outline" id="batch-back">Back to preview</button>'
    + (offset ? '<button class="button outline" data-batch-history="' + Math.max(0, offset - 12) + '">Previous</button>' : '')
    + (data.next === null ? '' : '<button class="button outline" data-batch-history="' + data.next + '">Next</button>') + '</div>');
}

document.addEventListener('change', async event => {
  if (state.locked || !event.target.closest('#batch-review-panel')) return;
  try {
    if (event.target.id === 'batch-kind') await loadCategoryBatch(event.target.value);
    if (event.target.hasAttribute('data-batch-group')) updateBatchSelection();
  } catch (error) {modalError(error);}
});

document.addEventListener('click', async event => {
  const button = event.target.closest('#batch-review-panel button');
  if (state.locked || !button || button.disabled) return;
  const review = categoryBatchReview;
  const page = review.page;
  try {
    if (button.dataset.batchAnalyze) return await detectCategories({...review.scope, pending_categories:button.dataset.batchAnalyze});
    if (button.hasAttribute('data-batch-page')) return await loadCategoryBatch(page.kind, Number(button.dataset.batchPage));
    if (button.hasAttribute('data-batch-history')) return await showBatchHistory(Number(button.dataset.batchHistory));
    if (button.dataset.batchUndo) {
      button.disabled = true;
      const result = await post('/review/batch-undo', {batch_id:button.dataset.batchUndo});
      await updateGalleryAfterCategoryChange();
      toast('Restored ' + quantity(result.restored, 'category', 'categories') + (result.skipped ? ' · ' + number(result.skipped) + ' left as edited' : ''));
      if ($('#modal').open && $('#batch-review-panel')) await showBatchHistory();
    }
    if (button.id === 'batch-back' || button.id === 'batch-reload') return await loadCategoryBatch(page.kind, page.offset);
    if (button.id === 'batch-select-all' || button.id === 'batch-clear') {
      for (const box of $$('[data-batch-group]:enabled')) box.checked = button.id === 'batch-select-all';
      updateBatchSelection();
    }
    if (button.id === 'batch-approve') {
      button.disabled = true;
      const selected = new Set($$('[data-batch-group]:checked:enabled').map(box => Number(box.dataset.batchGroup)));
      const groups = page.groups.filter(group => selected.has(group.id)).map(({id, revision}) => ({id, revision}));
      const result = await post('/review/batch-approve', {filters:review.scope, kind:page.kind, groups});
      await updateGalleryAfterCategoryChange();
      toast('Saved ' + quantity(result.updated, 'category', 'categories') + '. Undo from Batch history.');
      if ($('#modal').open && $('#batch-review-panel')) await loadCategoryBatch(page.kind, page.offset);
    }
  } catch (error) {
    if (button.isConnected) button.disabled = false;
    modalError(error);
  }
});

$('#modal').addEventListener('close', () => {
  if (!$('#modal').open) categoryBatchReview = null;
});
