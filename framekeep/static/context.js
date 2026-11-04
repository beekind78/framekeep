'use strict';

let contextItem = null, contextOrigin = null, pendingRemoval = null, imageEditor = null, shortcutCopying = false;
const contextMenu = $('#context-menu');

function selectedContextItem() {
  const ids = [...state.selected], photo = state.photos.find(row => row.id === ids[0]);
  return photo ? {type:ids.length > 1 ? 'photos' : 'photo', value:photo, ids} : null;
}

function hideContextMenu(restoreFocus = false) {
  if (contextMenu.matches(':popover-open')) contextMenu.hidePopover();
  if (restoreFocus && contextOrigin?.isConnected) contextOrigin.focus();
  contextItem = null;
}

function contextTarget(target) {
  const source = target.closest('[data-library],[data-rescan]');
  if (source) {
    const library = state.summary?.libraries.find(row => row.id === Number(source.dataset.library || source.dataset.rescan));
    if (library) return {type:'source', value:library, origin:source};
  }
  const card = target.closest('[data-media-id]');
  const id = card ? Number(card.dataset.mediaId) : target.closest('#photo-modal .photo-stage') ? state.detail : null;
  const photo = state.photos.find(row => row.id === id);
  if (!photo) return null;
  if (card && !state.selected.has(id)) {
    state.selected = new Set([id]); state.selectionAnchor = id; selectionUI();
  }
  const ids = card ? [...state.selected] : [id];
  return {type:ids.length > 1 ? 'photos' : 'photo', value:photo, ids,
          origin:card?.querySelector('.photo-open') || $('#detail-image')};
}

function showContextMenu(target, x, y) {
  if (state.locked) return;
  const item = contextTarget(target);
  if (!item) return false;
  hideContextMenu(); contextItem = item; contextOrigin = item.origin;
  const action = (name, label, symbol, disabled = false) => '<button role="menuitem" data-context-action="' + name + '"' + (disabled ? ' disabled' : '') + '>' + icon(symbol) + '<span>' + label + '</span></button>';
  const count = item.ids?.length || 0, multiple = count > 1;
  const title = item.type === 'source' ? item.value.name : multiple ? number(count) + ' selected items' : item.value.filename;
  const options = item.type === 'source'
    ? action('open-source', 'Open in Framekeep', 'folder') + action('rescan', 'Rescan folder', 'refresh') + action('reveal-source', 'Show folder in Explorer', 'folder')
    : multiple
      ? action('copy-files', 'Copy ' + number(count) + ' selected items', 'copy') + action('copy-path', 'Copy selected file paths', 'copy') + action('copy-folder', 'Copy selected to folder…', 'folder')
      : action('open', 'Open', 'photo') + action('copy-files', item.value.live_motion_id || item.value.live_still_id ? 'Copy original files (Live Photo)' : 'Copy original file', 'copy') + action('copy-path', 'Copy file path', 'copy') + action('copy-folder', 'Copy to folder…', 'folder') + '<hr>' + action('edit-image', 'Edit image copy…', 'sliders', item.value.media_type !== 'image') + action('edit-details', 'Edit tags / category…', 'sliders') + action('favorite', item.value.favorite ? 'Remove from favorites' : 'Add to favorites', 'heart') + action('reveal', 'Show original in folder', 'folder');
  contextMenu.innerHTML = '<div class="context-title">' + esc(title) + '</div>' + options + '<hr>' + action('remove', item.type === 'source' ? 'Remove folder from Framekeep…' : multiple ? 'Remove ' + number(count) + ' selected from Framekeep…' : 'Remove from Framekeep…', 'close');
  contextMenu.showPopover();
  const rect = contextMenu.getBoundingClientRect();
  contextMenu.style.left = Math.max(8, Math.min(x, innerWidth - rect.width - 8)) + 'px';
  contextMenu.style.top = Math.max(8, Math.min(y, innerHeight - rect.height - 8)) + 'px';
  contextMenu.querySelector('button:not(:disabled)').focus();
  return true;
}

document.addEventListener('contextmenu', event => {
  if (showContextMenu(event.target, event.clientX, event.clientY)) event.preventDefault();
});
document.addEventListener('pointerdown', event => {if (!contextMenu.contains(event.target)) hideContextMenu();});
window.addEventListener('resize', () => hideContextMenu());
document.addEventListener('scroll', event => {if (!contextMenu.contains(event.target)) hideContextMenu();}, true);
document.addEventListener('keydown', event => {
  if (event.key === 'ContextMenu' || (event.shiftKey && event.key === 'F10')) {
    const rect = event.target.getBoundingClientRect();
    if (showContextMenu(event.target, rect.left + 20, rect.top + 20)) event.preventDefault();
  }
  if (!contextMenu.matches(':popover-open')) return;
  if (['Escape','Tab'].includes(event.key)) {event.preventDefault(); hideContextMenu(true);}
  if (['ArrowDown','ArrowUp','Home','End'].includes(event.key)) {
    event.preventDefault();
    const buttons = $$('button:not(:disabled)', contextMenu), index = buttons.indexOf(document.activeElement);
    buttons[event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1 : (index + (event.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length].focus();
  }
});

document.addEventListener('keydown', async event => {
  if (event.defaultPrevented || event.isComposing || event.keyCode === 229 || state.locked || event.altKey) return;
  const control = event.target.closest?.('input,textarea,select,[contenteditable]:not([contenteditable="false"]),[role="textbox"],[role="searchbox"],[role="combobox"],video');
  if (event.target.isContentEditable || (control && !control.matches('#gallery .photo-select'))) return;
  if (document.querySelector('dialog[open]') || contextMenu.matches(':popover-open') || $('#gallery').classList.contains('hidden')) return;
  const command = (event.ctrlKey || event.metaKey) && !event.shiftKey;
  const key = event.key.toLowerCase();
  const selectAll = command && key === 'a', copy = command && key === 'c';
  const remove = key === 'delete' && !event.ctrlKey && !event.metaKey && !event.shiftKey;
  if ((!selectAll && !copy && !remove) || !galleryOrder().length) return;
  if (!selectAll && !state.selected.size) return;
  event.preventDefault();
  if (event.repeat) return;
  if (selectAll) return selectDisplayed();
  const item = selectedContextItem();
  if (!item || (copy && shortcutCopying)) return;
  try {
    if (copy) shortcutCopying = true;
    await contextAction(copy ? 'copy-files' : 'remove', item);
  } catch (error) {modalError(error);}
  finally {if (copy) shortcutCopying = false;}
});

function confirmRemoval(item) {
  pendingRemoval = item;
  const multiple = item.ids?.length > 1;
  const name = item.type === 'source' ? item.value.name : multiple ? number(item.ids.length) + ' selected items' : item.value.filename;
  const names = multiple ? '<ul class="remove-preview">' + item.ids.slice(0,6).map(id => '<li>' + esc(state.photos.find(photo => photo.id === id)?.filename || 'Selected item') + '</li>').join('') + (item.ids.length > 6 ? '<li>and ' + number(item.ids.length - 6) + ' more…</li>' : '') + '</ul>' : '';
  modal('Remove from Framekeep?', '<p><strong>' + esc(name) + '</strong> will be removed from your Framekeep view. Original files and folders stay in File Explorer.</p>' + names + '<p>' + (item.type === 'source' ? 'Its photo categories, tags and people matches are kept for when you restore the source.' : multiple ? 'Your tags and categories are kept. Live Photos include both original resources; each item can be restored later.' : item.value.live_motion_id || item.value.live_still_id ? 'Both parts of this Live Photo will stay together when removed or restored.' : 'Your tags and category are kept.') + ' Use Removed items in the sidebar to restore later.</p>' + actions('confirm-context-remove', multiple ? 'Remove ' + number(item.ids.length) + ' selected items' : 'Remove from Framekeep'));
}

async function showRemoved(offset = 0) {
  const data = await api('/removed?offset=' + offset);
  const sourceRows = data.sources.map(row => '<div class="removed-row"><div><strong>' + esc(row.name) + '</strong><small>' + esc(row.path) + '</small></div><button class="button outline" data-restore-source="' + row.id + '">Restore folder</button></div>').join('');
  const photoRows = data.photos.map(row => '<div class="removed-row"><div><strong>' + esc(row.filename) + '</strong><small>' + esc(row.source) + (row.files > 1 ? ' · ' + row.files + ' Live Photo files' : '') + '</small></div><button class="button outline" data-restore-photo="' + row.id + '">Restore</button></div>').join('');
  modal('Removed from Framekeep', '<p>Original files stay where they are. Restoring reconnects the source and rescans it; your saved tags, categories and people matches are kept.</p>' + (sourceRows ? '<h3>Source folders</h3>' + sourceRows : '') + (photoRows ? '<h3>Photos & videos</h3>' + photoRows : '') + (!sourceRows && !photoRows ? '<p>No removed items.</p>' : '') + '<div class="modal-actions">' + (offset ? '<button class="button outline" data-removed-page="' + Math.max(0, offset - data.limit) + '">Previous</button>' : '') + (offset + data.photos.length < data.total ? '<button class="button outline" data-removed-page="' + (offset + data.limit) + '">Next</button>' : '') + '<button class="button primary" data-action="close-modal">Done</button></div>');
}

async function contextAction(action, item) {
  const photo = item.value, id = photo.id;
  if (item.ids?.length > 5000) throw new Error('Select up to 5,000 items for one action.');
  switch (action) {
    case 'open': return openPhoto(id);
    case 'copy-files': {const result = await post('/photos/clipboard', {ids:item.ids, mode:'files'}); return toast(result.copied + ' original file(s) copied. Paste in File Explorer.');}
    case 'copy-path': await post('/photos/clipboard', {ids:item.ids, mode:'path'}); return toast(item.ids.length > 1 ? 'Selected file paths copied.' : 'File path copied.');
    case 'copy-folder': state.selected = new Set(item.ids); selectionUI(); return organize();
    case 'edit-details': openPhoto(id); $('#photo-tags').focus(); return $('#photo-tags').scrollIntoView({block:'center'});
    case 'edit-image': return editImage(photo);
    case 'favorite': return favorite(id, $('#photo-modal').open);
    case 'reveal': return post('/reveal', {photo_id:id});
    case 'open-source': return navigate('albums', String(id));
    case 'rescan': return startJob('/libraries/' + id + '/rescan');
    case 'reveal-source': return post('/libraries/' + id + '/reveal');
    case 'remove': return confirmRemoval(item);
  }
}

function clearEditor() {if (imageEditor?.bitmap) imageEditor.bitmap.close(); imageEditor = null;}
$('#modal').addEventListener('close', clearEditor);

async function editImage(photo) {
  clearEditor();
  if ($('#photo-modal').open) $('#photo-modal').close();
  const editor = {id:photo.id, turns:0, crop:[0,0,1,1], bitmap:null, dragging:null};
  imageEditor = editor;
  modal('Edit a copy', '<p><strong>' + esc(photo.filename) + '</strong> · Rotate, then drag on the picture to crop. Your original stays untouched.</p>' + (photo.live_motion_id ? '<p class="field-hint">This saves a still JPEG copy. The original Live Photo and its motion stay together.</p>' : '') + '<div class="editor-stage"><canvas id="edit-canvas" aria-label="Image crop preview"></canvas></div><div class="editor-tools"><button class="button outline" data-edit-turn="-1">Rotate left</button><button class="button outline" data-edit-turn="1">Rotate right</button><button class="button outline" data-edit-reset>Reset crop</button><span id="edit-selection" role="status">Loading preview…</span></div><label class="field-label" for="edit-output">Save edited JPEG to a separate folder</label><div class="input-row"><input id="edit-output" placeholder="Choose an output folder"><button class="button outline" data-browse="edit-output">Browse</button></div><p class="field-hint">The full-resolution source is used when saving. Existing files are kept; name conflicts get a numbered suffix. Source GPS metadata is omitted.</p><div class="modal-actions"><button class="button outline" data-action="close-modal">Cancel</button><button class="button primary" id="save-edited-copy" disabled>Save edited copy</button></div>');
  try {
    const response = await fetch('/api/photos/' + photo.id + '/preview', {headers:{'X-Framekeep-Token':state.token}});
    if (response.status === 401) {lockApp(); throw new Error('Session expired. Reopen Framekeep.');}
    if (!response.ok) {const error = await response.json(); throw new Error(error.detail || 'Preview unavailable.');}
    const bitmap = await createImageBitmap(await response.blob());
    if (imageEditor !== editor || !$('#modal').open || state.locked) {bitmap.close(); return;}
    editor.bitmap = bitmap; drawEditor(); $('#save-edited-copy').disabled = false;
  } catch (error) {if (imageEditor === editor && !state.locked) modalError(error);}
}

function drawEditor() {
  const editor = imageEditor, canvas = $('#edit-canvas');
  if (!editor?.bitmap || !canvas) return;
  const rotated = editor.turns % 2, width = rotated ? editor.bitmap.height : editor.bitmap.width, height = rotated ? editor.bitmap.width : editor.bitmap.height;
  canvas.width = width; canvas.height = height;
  const ctx = canvas.getContext('2d');
  ctx.save(); ctx.translate(width / 2, height / 2); ctx.rotate(editor.turns * Math.PI / 2); ctx.drawImage(editor.bitmap, -editor.bitmap.width / 2, -editor.bitmap.height / 2); ctx.restore();
  const [left, top, right, bottom] = editor.crop;
  ctx.fillStyle = '#102a2390'; ctx.beginPath(); ctx.rect(0,0,width,height); ctx.rect(left*width,top*height,(right-left)*width,(bottom-top)*height); ctx.fill('evenodd');
  ctx.strokeStyle = '#fff'; ctx.lineWidth = Math.max(2, width / 400); ctx.strokeRect(left*width,top*height,(right-left)*width,(bottom-top)*height);
  $('#edit-selection').textContent = 'Crop: ' + Math.round((right-left)*100) + '% × ' + Math.round((bottom-top)*100) + '%';
}

function editorPoint(event) {
  const rect = event.target.getBoundingClientRect();
  return [Math.max(0,Math.min(1,(event.clientX-rect.left)/rect.width)), Math.max(0,Math.min(1,(event.clientY-rect.top)/rect.height))];
}
document.addEventListener('pointerdown', event => {
  if (event.target.id !== 'edit-canvas' || !imageEditor?.bitmap) return;
  event.preventDefault(); imageEditor.dragging = editorPoint(event); event.target.setPointerCapture(event.pointerId);
});
document.addEventListener('pointermove', event => {
  if (event.target.id !== 'edit-canvas' || !imageEditor?.dragging) return;
  const [x,y] = editorPoint(event), [startX,startY] = imageEditor.dragging;
  imageEditor.crop = [Math.min(x,startX),Math.min(y,startY),Math.max(x,startX),Math.max(y,startY)]; drawEditor();
});
function endCrop(event) {
  if (event.target.id !== 'edit-canvas' || !imageEditor?.dragging) return;
  imageEditor.dragging = null;
  const [l,t,r,b] = imageEditor.crop;
  if (r-l < .01 || b-t < .01) imageEditor.crop = [0,0,1,1];
  drawEditor();
}
document.addEventListener('pointerup', endCrop);
document.addEventListener('pointercancel', endCrop);

document.addEventListener('click', async event => {
  const button = event.target.closest('button');
  if (!button || state.locked) return;
  try {
    if (button.dataset.selectionAction) {
      const item = selectedContextItem();
      if (!item) return;
      button.disabled = true;
      try {return await contextAction(button.dataset.selectionAction, item);}
      finally {button.disabled = false;}
    }
    if (button.dataset.contextAction) {const item = contextItem; hideContextMenu(); if (item) return await contextAction(button.dataset.contextAction, item);}
    if (button.id === 'confirm-context-remove' && pendingRemoval) {
      const item = pendingRemoval; button.disabled = true;
      if (item.type === 'photos') await post('/photos/remove', {ids:item.ids});
      else await api('/' + (item.type === 'source' ? 'libraries' : 'photos') + '/' + item.value.id, {method:'DELETE'});
      if ($('#photo-modal').open) $('#photo-modal').close();
      $('#modal').close(); state.selected.clear();
      if (item.type === 'source' && String(state.library) === String(item.value.id)) await navigate('albums');
      else {await summary(); await refresh();}
      return toast('Removed from Framekeep. Originals kept. Use Removed items to restore.');
    }
    if (button.dataset.action === 'removed-items') return await showRemoved();
    if (button.hasAttribute('data-removed-page')) return await showRemoved(Number(button.dataset.removedPage));
    if (button.dataset.restoreSource) return await startJob('/libraries/' + button.dataset.restoreSource + '/restore');
    if (button.dataset.restorePhoto) return await startJob('/photos/' + button.dataset.restorePhoto + '/restore');
    if (button.hasAttribute('data-edit-turn') && imageEditor?.bitmap) {imageEditor.turns = (imageEditor.turns + Number(button.dataset.editTurn) + 4) % 4; imageEditor.crop = [0,0,1,1]; return drawEditor();}
    if (button.hasAttribute('data-edit-reset') && imageEditor) {imageEditor.crop = [0,0,1,1]; return drawEditor();}
    if (button.id === 'save-edited-copy' && imageEditor?.bitmap) {
      button.disabled = true;
      try {await startJob('/photos/' + imageEditor.id + '/edit', {destination:$('#edit-output').value, turns:imageEditor.turns, crop:imageEditor.crop});}
      finally {if (button.isConnected) button.disabled = false;}
    }
  } catch (error) {button.disabled = false; modalError(error);}
});
