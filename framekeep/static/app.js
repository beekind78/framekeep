'use strict';

const $ = (selector, parent = document) => parent.querySelector(selector);
const $$ = (selector, parent = document) => [...parent.querySelectorAll(selector)];
const icon = name => '<svg aria-hidden="true"><use href="#i-' + name + '"/></svg>';
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
const number = value => Number(value || 0).toLocaleString();
const quantity = (value, unit) => number(value) + ' ' + unit + (Number(value) === 1 ? '' : 's');
const bytes = value => {
  if (!value) return '0 B';
  const unit = Math.min(3, Math.floor(Math.log(value) / Math.log(1024)));
  return (value / 1024 ** unit).toFixed(unit ? 1 : 0) + ' ' + ['B', 'KB', 'MB', 'GB'][unit];
};
const dateLabel = value => new Date(value).toLocaleDateString('en-GB', {day:'numeric', month:'short', year:'numeric'});
const monthLabel = value => new Date(value + '-01T12:00:00').toLocaleDateString('en-GB', {month:'long', year:'numeric'});
const state = {token:'', locked:false, summary:null, view:'albums', library:'', person:'', month:'', photos:[], total:0, selected:new Set(), ready:false, job:null, finished:null, request:0, detail:null};
state.selectionAnchor = null;
const pendingSelectionModifiers = new WeakMap();
function setGalleryDensity(value) {
  const density = value === 'compact' ? 'compact' : 'comfortable';
  document.body.dataset.galleryDensity = density;
  $$('[data-gallery-density]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.galleryDensity === density)));
  try {localStorage.setItem('framekeep.gallery-density', density);} catch { /* Preferences are optional. */ }
}
try {setGalleryDensity(localStorage.getItem('framekeep.gallery-density'));} catch {setGalleryDensity('comfortable');}
const categories = ['Photos', 'Screenshots', 'Illustrations', 'Videos', 'Needs review', 'Animated'];
state.kindCounts = []; state.categoriesReady = false; state.categoryFilters = null; state.categoryIds = [];
state.albumKind = ''; state.albumData = null; state.detailBusy = false;
state.photosLiveOnly = false;
state.duplicateMode = 'exact'; state.similarData = null; state.similarPairs = [];
state.reviewFilters = null; state.afterCategoryInstall = 'categories';
state.motionURL = null; state.motionRequest = 0;
state.playbackJob = null; state.playbackAbort = null;
let toastTimer, searchTimer, polling = false, previewPlan = null;

function lockApp() {
  stopMotion();
  if (typeof hideContextMenu === 'function') hideContextMenu();
  if (typeof clearEditor === 'function') clearEditor();
  state.token = ''; state.locked = true; state.photos = []; state.summary = null;
  state.selected.clear();
  $('#selection-toolbar').classList.add('hidden'); document.body.classList.remove('has-selection');
  sessionStorage.removeItem('framekeep.session');
  $('#gallery').innerHTML = ''; $('#people-view').innerHTML = ''; $('#stats').innerHTML = '';
  $('#source-list').innerHTML = ''; $('#nav-total').textContent = '0'; $('#nav-duplicates').textContent = '';
  $('#category-bar').classList.add('hidden'); $('#category-chips').innerHTML = '';
  $('#albums-view').innerHTML = ''; $('#albums-view').classList.add('hidden');
  state.albumData = null;
  state.similarData = null; state.similarPairs = [];
  state.matchPhotos = new Map();
  $('#review-tools').innerHTML = ''; $('#review-tools').classList.add('hidden'); $('#nav-review').textContent = '';
  $('#photo-details').innerHTML = ''; if ($('#detail-video')) $('#detail-video').innerHTML = '';
  $('#page-title').textContent = 'Open Framekeep securely.';
  $('#page-subtitle').textContent = 'Double-click Start Framekeep.cmd to unlock a private session on this PC.';
  $('#search').value = '';
  for (const selector of ['#toolbar','#gallery-top','#welcome','#demo-banner','#empty-state','#organize-button','#load-more','#job-dock']) $(selector).classList.add('hidden');
  if ($('#photo-modal').open) $('#photo-modal').close();
  if ($('#modal').open) $('#modal').close();
  $('#modal-content').innerHTML = '';
  state.photosLiveOnly = false;
  $('#detail-image').removeAttribute('src'); $('#detail-image').removeAttribute('data-media');
}

async function api(path, options = {}) {
  const response = await fetch('/api' + path, {
    ...options,
    headers:{'Content-Type':'application/json', 'X-Framekeep-Token':state.token, ...options.headers}
  });
  if (response.status === 401) {
    lockApp();
    throw new Error('Open Start Framekeep.cmd to unlock this session.');
  }
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Please check the fields and try again.');
  return data;
}

// Images use the same origin-and-port-bound header credential as JSON requests.
// No ambient localhost cookie or token-bearing image URL is used.
const media = new Map();
const videoMediaQueue = [];
let videoMediaActive = 0;
const videoMediaObserver = new IntersectionObserver(records => {
  for (const record of records) {
    if (!record.isIntersecting) continue;
    videoMediaObserver.unobserve(record.target);
    const entry = media.get(record.target);
    if (entry) videoMediaQueue.push([record.target, entry]);
  }
  pumpVideoMedia();
}, {rootMargin:'240px'});

function pumpVideoMedia() {
  while (videoMediaActive < 2 && videoMediaQueue.length) {
    const [element, entry] = videoMediaQueue.shift();
    if (!element.isConnected || media.get(element) !== entry || entry.controller.signal.aborted) continue;
    videoMediaActive++;
    loadMedia(element, entry).finally(() => {videoMediaActive--; pumpVideoMedia();});
  }
}

async function loadMedia(element, entry) {
  try {
    const response = await fetch(entry.path, {headers:{'X-Framekeep-Token':state.token}, signal:entry.controller.signal});
    if (response.status === 401) {lockApp(); throw new Error('Session expired.');}
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(typeof error.detail === 'string' ? error.detail : 'Thumbnail unavailable.');
    }
    if (!response.headers.get('Content-Type')?.startsWith('image/jpeg')) throw new Error('Image unavailable.');
    const blob = await response.blob();
    if (media.get(element) !== entry || !element.isConnected || !state.token) return;
    entry.url = URL.createObjectURL(blob);
    if (entry.video) {
      element.onload = () => {if (media.get(element) === entry) element.closest('.video-preview').dataset.thumbnail = 'ready';};
      element.onerror = () => {if (element.isConnected) element.closest('.video-preview').title = 'Video thumbnail unavailable. You can still open the video.';};
    }
    element.src = entry.url;
  } catch (error) {
    if (error.name === 'AbortError') return;
    if (entry.video && element.isConnected) {
      element.closest('.video-preview').title = error.message;
      element.closest('.video-preview').dataset.thumbnail = 'unavailable';
    } else element.title = 'Image unavailable. Rescan this folder or reopen the launcher.';
  }
}

function updateMedia() {
  for (const [element, entry] of media) {
    if (!element.isConnected || element.dataset.media !== entry.path || !state.token) {
      entry.controller.abort();
      videoMediaObserver.unobserve(element);
      if (entry.url) URL.revokeObjectURL(entry.url);
      element.removeAttribute('src');
      media.delete(element);
    }
  }
  if (!state.token) return;
  for (const element of $$('img[data-media]')) {
    if (media.has(element)) continue;
    const path = element.dataset.media;
    if (!path.startsWith('/api/')) continue;
    const entry = {path, controller:new AbortController(), url:null, video:element.hasAttribute('data-video-thumbnail')};
    media.set(element, entry);
    if (entry.video) videoMediaObserver.observe(element);
    else loadMedia(element, entry);
  }
}
new MutationObserver(updateMedia).observe(document.body, {childList:true, subtree:true, attributes:true, attributeFilter:['data-media']});
const post = (path, body = {}) => api(path, {method:'POST', body:JSON.stringify(body)});
function toast(message) {
  $('#toast').textContent = message;
  $('#toast').classList.remove('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $('#toast').classList.add('hidden'), 4200);
}
function modal(title, body) {
  const dialog = $('#modal');
  $('#modal-content').innerHTML = '<div class="modal-head"><h2>' + esc(title) + '</h2><button class="icon-button" data-action="close-modal" aria-label="Close dialog">' + icon('close') + '</button></div><div class="modal-body">' + body + '<div id="modal-error" class="inline-error" role="alert"></div></div>';
  if (!dialog.open) dialog.showModal();
}
function modalError(error) {
  const target = $('#modal-error');
  if (target && $('#modal').open) target.textContent = error.message;
  else toast(error.message);
}
function actions(primary, label, secondary = 'Cancel') {
  return '<div class="modal-actions"><button class="button outline" data-action="close-modal">' + esc(secondary) + '</button><button class="button primary" id="' + primary + '">' + esc(label) + icon('arrow') + '</button></div>';
}
function filters() {
  return {q:$('#search').value, year:$('#filter-year').value, type:$('#filter-type').value, kind:state.view === 'review' ? 'Needs review' : state.view === 'albums' ? state.albumKind : $('#filter-kind').value, sort:$('#sort').value, library:state.library, person:state.person, month:state.month, favorite:state.view === 'favorites', duplicates:state.view === 'duplicates' && state.duplicateMode === 'exact', collapse_live:state.view !== 'duplicates', live_only:state.view === 'albums' && state.albumKind === 'Photos' && state.photosLiveOnly};
}
function queryParams() {
  const parameters = new URLSearchParams();
  for (const [key, value] of Object.entries(filters())) {
    if (value) parameters.set(key, typeof value === 'boolean' ? '1' : value);
  }
  return parameters;
}
function optionList(selector, rows, label) {
  const select = $(selector), current = select.value;
  select.innerHTML = '<option value="">' + label + '</option>' + rows.map(row => '<option value="' + esc(row.value) + '">' + esc(row.value) + ' · ' + number(row.count) + '</option>').join('');
  select.value = rows.some(row => row.value === current) ? current : '';
}
async function summary() {
  state.summary = await api('/summary');
  const s = state.summary;
  $('#nav-total').textContent = number(s.total);
  $('#nav-duplicates').textContent = s.duplicates.extra_copies ? number(s.duplicates.extra_copies) : '';
  $('#nav-review').textContent = number(s.kinds.find(row => row.value === 'Needs review')?.count || 0);
  $('#stats').innerHTML = [
    ['photo', number(s.total), 'Photos and videos', bytes(s.bytes)],
    ['calendar', number(s.years.length), 'Years of memories', 'A personal timeline'],
    ['people', number(s.people.length), 'Familiar faces', 'On-device grouping'],
    ['copy', number(s.duplicates.extra_copies), 'Extra exact copies', bytes(s.duplicates.bytes)]
  ].map(([image, value, label, note]) => '<div class="stat"><div class="stat-icon">' + icon(image) + '</div><div><div class="stat-value">' + value + '</div><div class="stat-label">' + label + '</div></div><span class="stat-note">' + note + '</span></div>').join('');
  optionList('#filter-year', s.years, 'All dates');
  optionList('#filter-type', s.types, 'All formats');
  optionList('#filter-kind', s.kinds, 'All kinds');
  $('#source-list').innerHTML = s.libraries.length ? s.libraries.map(source => '<div class="source-item"><button class="source-button ' + (String(source.id) === String(state.library) ? 'active' : '') + '" data-library="' + source.id + '" title="' + esc(source.path) + '">' + icon('folder') + '<span>' + esc(source.demo ? 'Sample library' : source.name) + '</span><small>' + number(source.count) + '</small></button><button class="tiny rescan" data-rescan="' + source.id + '" title="Rescan folder" aria-label="Rescan ' + esc(source.name) + '">' + icon('refresh') + '</button></div>').join('') : '<p class="source-empty">Your photo folders will live here.</p>';
}
function setHeading() {
  const person = state.summary.people.find(p => String(p.id) === String(state.person));
  const source = state.summary.libraries.find(l => String(l.id) === String(state.library));
  const headings = {
    albums:['Your albums', 'Browse by category. Find a month, open a memory.', 'Albums'],
    all:['All media', 'Your photos and videos, together in one view.', 'All media'],
    timeline:['Timeline', 'Browse your library month by month.', 'Timeline'],
    people:['People', 'Review suggested face groups and add names.', 'People'],
    duplicates:['Duplicates', 'Compare exact copies and similar photos before deciding what to keep.', 'Duplicates'],
    review:['Needs review', 'Confirm a suggested category or choose your own.', 'Needs review'],
    favorites:['Favorites', 'The photos and videos you have saved to favorites.', 'Favorites']
  };
  let [title, subtitle, crumb] = headings[state.view];
  if (person) {title = person.name; subtitle = 'Suggested face matches. Review the group to correct any mix-ups.'; crumb = 'People / ' + person.name;}
  else if (source) {title = source.demo ? 'A glimpse of what’s possible.' : source.name; subtitle = source.demo ? 'A sample library to explore, with original landscape illustrations.' : 'Connected locally · ' + source.path; crumb = source.demo ? 'Sample library' : source.name;}
  if (state.view === 'albums') {
    title = state.albumKind || 'Your albums';
    subtitle = state.albumKind ? 'Browse by month. Open an item for a closer look.' : 'Browse by category. Find a month, open a memory.';
    if (state.albumKind === 'Photos') subtitle = 'Still photos and Live Photos, organized by month.';
    crumb = (source ? (source.demo ? 'Sample library' : source.name) + ' / ' : '') + 'Albums' + (state.albumKind ? ' / ' + state.albumKind : '') + (state.month ? ' / ' + monthLabel(state.month) : '');
  }
  $('#page-title').textContent = title;
  $('#view-label').textContent = source ? 'CONNECTED FOLDER' : state.albumKind ? 'YOUR ALBUMS' : 'YOUR COLLECTION';
  $('#page-subtitle').textContent = subtitle;
  $('#breadcrumb').textContent = crumb;
  $$('.nav-item').forEach(button => button.classList.toggle('active', button.dataset.view === state.view && !state.library));
  const onlyDemo = state.summary.total && state.summary.libraries.filter(l => l.count).every(l => l.demo);
  $('#demo-banner').classList.toggle('hidden', !(source ? source.demo : onlyDemo));
  const peoplePage = state.view === 'people' && !state.person;
  const albumRoot = state.view === 'albums' && !state.albumKind;
  const similar = state.view === 'duplicates' && state.duplicateMode === 'similar';
  $('#stats').classList.toggle('hidden', state.view === 'albums');
  $('#albums-view').classList.toggle('hidden', state.view !== 'albums' || !state.summary.total);
  $('#gallery').classList.toggle('hidden', peoplePage || albumRoot);
  $('#filter-kind').classList.toggle('hidden', ['albums','review'].includes(state.view));
  $('#toolbar').classList.toggle('hidden', peoplePage || !state.summary.total);
  $('#gallery-top').classList.toggle('hidden', peoplePage || albumRoot || !state.summary.total);
  $('#organize-button').classList.toggle('hidden', peoplePage || albumRoot || similar || !state.summary.total);
  $('#welcome').classList.toggle('hidden', !!state.summary.total || !['all','albums'].includes(state.view));
  $('#people-view').classList.toggle('hidden', !peoplePage);
  $('#category-bar').classList.toggle('hidden', peoplePage || albumRoot || ['review','duplicates'].includes(state.view) || !state.summary.total);
  $('#review-tools').classList.toggle('hidden', !['review','duplicates'].includes(state.view) && !(state.view === 'albums' && state.albumKind === 'Needs review'));
  $('#select-loaded').classList.toggle('hidden', similar);
  $('.sort-label').classList.toggle('hidden', similar);
  $('#gallery').classList.toggle('similar-view', similar);
  selectionUI();
  renderReviewTools();
}

function renderReviewTools() {
  if (!['review','duplicates'].includes(state.view) && !(state.view === 'albums' && state.albumKind === 'Needs review')) return;
  const similar = state.duplicateMode === 'similar';
  $('#review-tools').innerHTML = (state.view === 'duplicates' ? '<div class="duplicate-tabs" role="group" aria-label="Duplicate type"><button data-duplicate-mode="exact" aria-pressed="' + !similar + '" class="' + (!similar ? 'active' : '') + '">Exact duplicates</button><button data-duplicate-mode="similar" aria-pressed="' + similar + '" class="' + (similar ? 'active' : '') + '">Similar photos</button></div>' : '')
    + '<div class="review-intro"><p>' + (state.view !== 'duplicates' ? 'AI suggests Photos, Screenshots or Illustrations. Close calls stay here for you to decide. Open a picture to compare it with possible copies.' : similar ? 'Find possible resized or recompressed copies using visual fingerprints. Matches need your review and are never used to skip or delete files.' : 'Exact copies have identical file contents, verified with SHA-256. Videos are included.') + '</p><button class="button primary small" data-action="analyze-review">' + icon('spark') + 'Analyze photos</button></div>';
}

async function analyzeReview() {
  state.reviewFilters = {...filters(), kind:'', duplicates:false};
  if (state.categoriesReady) return startJob('/review/analyze', {filters:state.reviewFilters});
  state.afterCategoryInstall = 'review';
  modal('Private AI, on this PC.', '<p>Download the category model once (about 85 MB), then suggest Photos, Screenshots and Illustrations locally. Close calls still need your review.</p><p>This also builds visual fingerprints for possible resized or recompressed copies. Exact copies use the existing SHA-256 check. Manual categories are kept and no photo is uploaded.</p>' + actions('install-categories', 'Download & analyze photos'));
}

async function acceptSuggestion(id) {
  const photo = state.photos.find(row => row.id === Number(id));
  if (!photo || !['Photos','Screenshots','Illustrations'].includes(photo.kind_suggestion)) return;
  await api('/photos/' + photo.id, {method:'PATCH', body:JSON.stringify({kind:photo.kind_suggestion})});
  if ($('#photo-modal').open) $('#photo-modal').close();
  await summary(); await refresh(); toast('Category confirmed. Your choice is saved.');
}
function renderCategories() {
  const counts = new Map(state.kindCounts.map(row => [row.value, row.count]));
  const total = state.kindCounts.reduce((sum, row) => sum + row.count, 0);
  const active = state.view === 'albums' ? state.albumKind : $('#filter-kind').value;
  $('#category-chips').innerHTML = [['', state.view === 'albums' ? 'All albums' : 'All media', total], ...categories.map(kind => [kind, kind, counts.get(kind) || 0])]
    .map(([value, label, count]) => '<button class="category-chip' + (active === value ? ' active' : '') + '" data-kind="' + esc(value) + '" aria-pressed="' + (active === value) + '"' + (!count && value ? ' disabled' : '') + '>' + esc(label) + '<span>' + number(count) + '</span></button>').join('');
}
function categoryBadge(photo) {
  return '<span class="category-badge" data-category="' + esc(photo.kind) + '" title="' + (photo.kind_override ? 'Set manually' : 'Automatic suggestion') + '">' + esc(photo.kind) + '</span>';
}
function albumCover(album) {
  if (!album.covers.length) return '<span class="album-cover album-cover-empty">' + icon('folder') + '</span>';
  return '<span class="album-cover" data-count="' + album.covers.length + '">' + album.covers.map(photo => photo.media_type === 'video'
    ? videoThumbnail(photo)
    : '<img data-media="/api/photos/' + photo.id + '/thumbnail" alt="" loading="lazy">').join('') + '</span>';
}
function renderAlbums() {
  const data = state.albumData || {total:0,photos:0,videos:0,live:0,albums:[]};
  const albums = new Map(data.albums.map(album => [album.kind, album]));
  if (!state.albumKind) {
    const visible = categories.filter((kind, index) => index < 4 || albums.has(kind));
    $('#albums-view').innerHTML = '<div class="albums-intro"><p><strong>' + quantity(data.total, 'item') + '</strong> · ' + quantity(data.photos, 'photo') + ' · ' + quantity(data.videos, 'video') + '</p><div><button class="text-button" data-action="browse-flat">View all media ' + icon('arrow') + '</button><button class="button outline small" id="album-detect">Detect categories</button></div></div>'
      + (data.live ? '<p class="albums-explainer">' + quantity(data.live, 'Live Photo') + ' included with your photos. Their motion clips stay with the image; Videos shows separate videos only.</p>' : '')
      + (!data.total ? '<p class="album-empty">No matching items. Clear your search or reset the filters to see your albums.</p>' : '')
      + '<div class="albums-grid">' + visible.map(kind => {
        const album = albums.get(kind) || {kind,count:0,months:[],covers:[]};
        return '<article class="album-card" data-category="' + esc(kind) + '"><button class="album-open" data-open-album="' + esc(kind) + '" aria-label="Open ' + esc(kind) + ' album, ' + quantity(album.count, 'item') + '"' + (!album.count ? ' disabled' : '') + '>' + albumCover(album) + '<span class="album-card-title"><strong>' + esc(kind) + '</strong><span>' + number(album.count) + '</span></span>' + (album.live ? '<span class="album-live-note">◉ Includes ' + quantity(album.live, 'Live Photo') + '</span>' : '') + '<span class="album-card-note">' + (album.count ? 'View all ' + quantity(album.months.length, 'month') + ' ' : 'No items yet') + (album.count ? '↗' : '') + '</span></button><div class="album-shortcuts">'
          + album.months.slice(0,3).map(month => '<button data-open-album="' + esc(kind) + '" data-album-month="' + esc(month.value) + '">' + esc(monthLabel(month.value)) + '<span>' + number(month.count) + '</span></button>').join('') + '</div></article>';
      }).join('') + '</div>';
    return;
  }
  const album = albums.get(state.albumKind) || {years:[],months:[]};
  const year = $('#filter-year').value;
  const months = album.months.filter(month => !year || month.value.startsWith(year + '-'));
  $('#albums-view').innerHTML = '<div class="album-navigation"><div class="album-breadcrumb"><button class="text-button" data-album-home>' + icon('folder') + 'All albums</button><span>›</span><strong>' + esc(state.albumKind) + '</strong><span class="album-item-count">' + quantity(state.total, 'item') + '</span></div><details class="date-browser"' + (year || state.month ? ' open' : '') + '><summary>' + icon('calendar') + 'Browse by date' + (state.month ? ' · ' + esc(monthLabel(state.month)) : year ? ' · ' + esc(year) : '') + '</summary><div class="album-years" aria-label="Album years">'
    + [['','All years'], ...album.years.map(row => [row.value,row.value + ' · ' + number(row.count)])].map(([value,label]) => '<button class="album-date' + (year === value ? ' active' : '') + '" data-album-year="' + value + '" aria-pressed="' + (year === value) + '">' + esc(label) + '</button>').join('') + '</div><div class="album-months" aria-label="Album months"><button class="album-date' + (!state.month ? ' active' : '') + '" data-album-month="" aria-pressed="' + !state.month + '">All months</button>'
    + months.map(month => '<button class="album-date' + (state.month === month.value ? ' active' : '') + '" data-album-month="' + esc(month.value) + '" aria-pressed="' + (state.month === month.value) + '">' + esc(monthLabel(month.value)) + '<span>' + number(month.count) + '</span></button>').join('') + '</div></details></div>';
}
function renderPhotoTypes(album) {
  if (state.albumKind !== 'Photos') return;
  $('#albums-view .album-navigation').insertAdjacentHTML('beforeend', '<div class="album-photo-types" role="group" aria-label="Photo types"><button class="album-date' + (!state.photosLiveOnly ? ' active' : '') + '" data-photos-live="0" aria-pressed="' + !state.photosLiveOnly + '">All photos · ' + number(album.count) + '</button><button class="album-date' + (state.photosLiveOnly ? ' active' : '') + '" data-photos-live="1" aria-pressed="' + state.photosLiveOnly + '">◉ Live Photos only · ' + number(album.live) + '</button><button class="text-button" data-action="link-live">Find Live Photos</button></div>');
}
async function openAlbum(kind, month = '') {
  state.photosLiveOnly = false;
  state.albumKind = kind; state.month = month; state.selected.clear();
  $('#filter-kind').value = kind;
  if (month) $('#filter-year').value = month.slice(0,4);
  await refresh();
  window.scrollTo({top:0, behavior:'instant'});
}
async function albumHome() {
  state.photosLiveOnly = false;
  state.albumKind = ''; state.month = ''; state.selected.clear();
  $('#filter-kind').value = ''; $('#filter-year').value = '';
  await refresh();
  window.scrollTo({top:0, behavior:'instant'});
}
function durationLabel(seconds) {
  const value = Math.max(0, Math.floor(seconds || 0));
  const minutes = Math.floor(value / 60), remainder = String(value % 60).padStart(2, '0');
  return minutes >= 60 ? Math.floor(minutes / 60) + ':' + String(minutes % 60).padStart(2, '0') + ':' + remainder : minutes + ':' + remainder;
}
function mediaPreview(photo) {
  if (photo.media_type === 'video') return videoThumbnail(photo);
  return '<img data-media="/api/photos/' + photo.id + '/thumbnail?v=' + photo.mtime_ns + '" alt="' + esc(photo.filename) + '" loading="lazy">';
}
function videoThumbnail(photo) {
  return '<span class="video-preview"><img class="video-thumbnail" data-video-thumbnail data-media="/api/photos/' + photo.id + '/thumbnail?v=' + (photo.mtime_ns || 0) + '" alt="Video frame from ' + esc(photo.filename) + '" loading="lazy"><span class="video-play" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M8 5 L19 12 L8 19 Z" fill="currentColor"/></svg></span><span class="video-duration">Video · ' + durationLabel(photo.duration) + '</span></span>';
}
function categoryOptions(current, automaticLabel = 'Use automatic suggestion') {
  return '<option value=""' + (!current ? ' selected' : '') + '>' + esc(automaticLabel) + '</option>' + categories.filter(kind => kind !== 'Videos').map(kind => '<option value="' + esc(kind) + '"' + (current === kind ? ' selected' : '') + '>' + esc(kind) + '</option>').join('');
}
function categoryEditor(photo) {
  if (photo.media_type === 'video') return '';
  const hint = photo.kind_override ? 'Your manual category is kept. You can change it or return to the automatic suggestion.' : photo.kind_source === 'pending' ? 'Use Analyze photos or Detect categories for a local visual suggestion.'
    : photo.kind_source === 'camera' ? 'Camera metadata suggests Photos. You can correct it.'
    : photo.kind_source === 'filename' ? 'The filename suggests Screenshots. You can correct it.'
    : photo.automatic_kind === 'Needs review' && photo.kind_suggestion ? 'Uncertain suggestion: ' + photo.kind_suggestion + '. Please review.'
    : 'Automatic suggestion: ' + photo.automatic_kind + '. You can correct it.';
  return '<div class="category-editor"><label for="photo-kind">Category</label><select class="text-input" id="photo-kind">' + categoryOptions(photo.kind_override, 'Automatic: ' + photo.automatic_kind) + '</select><p class="field-hint">' + esc(hint) + '</p>' + (photo.kind === 'Needs review' && ['Photos','Screenshots','Illustrations'].includes(photo.kind_suggestion) ? '<button class="button primary" data-accept-suggestion="' + photo.id + '">Confirm AI suggestion: ' + esc(photo.kind_suggestion) + '</button>' : '') + '<button class="button outline" id="save-photo-kind">Save category</button></div>';
}
async function detectCategories() {
  state.afterCategoryInstall = 'categories';
  state.categoryFilters = state.selected.size ? {ids:[...state.selected]} : {...filters(), kind:''};
  if (state.categoriesReady) return startJob('/categories/analyze', {filters:state.categoryFilters});
  modal('A little help sorting the view.', '<p>Suggest Photos, Screenshots and Illustrations using a visual model on this PC. Unclear matches stay in Needs review. You can correct any suggestion.</p><p>Download the category model once (about 85 MB) from Hugging Face. Detection then works offline. No photo is uploaded.</p><p class="field-hint">Your manual categories are kept. Original files stay untouched.</p>' + actions('install-categories', 'Download & detect categories'));
}
function setSelectedCategory() {
  state.categoryIds = [...state.selected];
  if (!state.categoryIds.length) return;
  modal('Put these in the right category.', '<p>Set a category for ' + number(state.categoryIds.length) + ' selected image' + (state.categoryIds.length === 1 ? '' : 's') + '. Your choice stays in Framekeep when you rescan the same files.</p><label class="field-label" for="bulk-kind">Category</label><select class="text-input" id="bulk-kind">' + categoryOptions('Photos') + '</select>' + actions('save-bulk-kind', 'Save category'));
}
function photoCard(photo) {
  return '<article class="photo-card" data-media-id="' + photo.id + '"><input type="checkbox" class="photo-select" data-select="' + photo.id + '" aria-label="Select ' + esc(photo.filename) + '"' + (state.selected.has(photo.id) ? ' checked' : '') + '><button class="photo-open" data-photo="' + photo.id + '" aria-label="Open ' + esc(photo.filename) + '">' + mediaPreview(photo) + '</button><button class="favorite-toggle ' + (photo.favorite ? 'is-favorite' : '') + '" data-favorite="' + photo.id + '" aria-label="' + (photo.favorite ? 'Remove from' : 'Add to') + ' favorites">' + icon('heart') + '</button>' + (photo.copies > 1 ? '<span class="duplicate-badge">' + icon('copy') + photo.copies + ' copies</span>' : '') + '<div class="photo-caption"><span class="photo-name" title="' + esc(photo.filename) + '">' + esc(photo.filename) + '</span><span class="photo-format">' + esc(photo.extension) + '</span></div><div class="photo-date">' + dateLabel(photo.captured_at) + (photo.date_source === 'File modified' ? ' · file date' : '') + '</div>' + categoryBadge(photo) + '</article>';
}
function selectionUI() {
  for (const card of $$('#gallery [data-media-id]')) {
    const selected = state.selected.has(Number(card.dataset.mediaId));
    card.classList.toggle('is-selected', selected);
    const checkbox = $('.photo-select', card);
    if (checkbox) checkbox.checked = selected;
  }
  if (!state.selected.size) state.selectionAnchor = null;
  const hasSelection = !!state.selected.size && !state.locked && !$('#gallery').classList.contains('hidden');
  $('#selection-toolbar').classList.toggle('hidden', !hasSelection);
  $('#selection-toolbar-count').textContent = number(state.selected.size) + ' selected';
  document.body.classList.toggle('has-selection', hasSelection);
  $('#selection-count').textContent = state.selected.size ? '· ' + number(state.selected.size) + ' selected' : '';
  $('#clear-selection').classList.toggle('hidden', !state.selected.size);
  $('#set-category').classList.toggle('hidden', !state.selected.size || state.photos.some(p => state.selected.has(p.id) && p.media_type === 'video'));
  $('#gallery').classList.toggle('selecting', !!state.selected.size);
  $('#organize-button').innerHTML = icon('folder') + (state.selected.size ? 'Organize ' + number(state.selected.size) + ' selected' : 'Organize copies') + icon('arrow');
}

function galleryOrder() {
  return [...new Set($$('#gallery [data-media-id]').map(card => Number(card.dataset.mediaId)))];
}

function selectDisplayed() {
  const ids = galleryOrder();
  state.selected = new Set(ids);
  if (!state.selected.has(state.selectionAnchor)) state.selectionAnchor = ids[0] ?? null;
  selectionUI();
}

function selectWithModifiers(id, event) {
  const order = galleryOrder(), target = order.indexOf(id), anchor = order.indexOf(state.selectionAnchor);
  if (target < 0) return;
  if (event.shiftKey && anchor >= 0) {
    if (!event.ctrlKey && !event.metaKey) state.selected.clear();
    for (const selectedId of order.slice(Math.min(anchor,target), Math.max(anchor,target) + 1)) state.selected.add(selectedId);
  } else {
    if (state.selected.has(id) && !event.shiftKey) state.selected.delete(id);
    else state.selected.add(id);
    state.selectionAnchor = id;
  }
  selectionUI();
}
function reviewCard(photo) {
  const card = photoCard(photo).replace('</article>', (photo.live_motion_id ? '<span class="live-badge">◉ LIVE</span>' : '') + '</article>');
  if (photo.kind !== 'Needs review' || !['Photos','Screenshots','Illustrations'].includes(photo.kind_suggestion)) return card;
  return card.replace('</article>', '<div class="review-suggestion"><span>AI suggests ' + esc(photo.kind_suggestion) + '</span><button class="text-button" data-accept-suggestion="' + photo.id + '">Confirm category ' + icon('check') + '</button></div></article>');
}
function renderGallery() {
  const groups = new Map();
  const f = filters();
  for (const photo of state.photos) {
    const key = state.view === 'duplicates' ? photo.sha256 : state.view === 'albums' || ['newest','oldest'].includes(f.sort) ? photo.captured_at.slice(0,7) : 'all';
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(photo);
  }
  let prefix = '';
  if (state.person) prefix = '<div class="person-filter-bar"><span>' + icon('people') + ' Suggested matches · ' + number(state.total) + ' photos</span><button class="text-button" data-manage-person="' + esc(state.person) + '">Review & name this group ' + icon('arrow') + '</button></div>';
  if (state.view === 'timeline') prefix += '<div class="timeline-months">' + state.summary.months.map(m => '<button class="button small ' + (state.month === m.value ? 'primary' : 'outline') + '" data-month="' + m.value + '">' + esc(monthLabel(m.value)) + '</button>').join('') + '</div>';
  if (state.view === 'duplicates') prefix += '<div class="callout">' + icon('copy') + '<span>These files match by SHA-256. Similar-looking photos have their own tab. Originals are never deleted.</span></div>';
  $('#gallery').innerHTML = prefix + [...groups].map(([key, photos]) => '<section class="gallery-group"><div class="group-heading"><h2>' + (key === 'all' ? 'All moments' : state.view === 'duplicates' ? 'Matching copies · ' + esc(key.slice(0,8)) : monthLabel(key)) + '</h2><span>' + photos.length + ' displayed</span></div><div class="photo-grid">' + photos.map(reviewCard).join('') + '</div></section>').join('');
  $('#result-count').textContent = number(state.total) + ' item' + (state.total === 1 ? '' : 's');
  $('#load-more').textContent = 'Load more photos';
  $('#load-more').classList.toggle('hidden', state.photos.length >= state.total);
  const empty = !state.total && !(state.view === 'people' && !state.person);
  $('#empty-state').classList.toggle('hidden', !empty);
  $('#empty-title').textContent = state.summary.total ? 'No moments found here. Yet.' : 'A little space for your story.';
  $('#empty-description').textContent = state.summary.total ? 'Try a different search or clear your filters to see more photos.' : 'Add a folder to see your photos here. Nothing is uploaded.';
  if (state.view === 'review') {
    $('#empty-title').textContent = 'No images waiting for review.';
    $('#empty-description').textContent = 'No matching images currently have the Needs review category. Analyze photos to check the current collection, or clear your filters.';
  }
  $('#empty-state [data-action="add"]').classList.toggle('hidden', state.view === 'review');
  if (state.photosLiveOnly && state.view === 'albums' && state.albumKind === 'Photos') {
    $('#empty-title').textContent = 'No matching Live Photos.';
    $('#empty-description').textContent = 'Choose All photos to see still images too, or Find Live Photos to link existing Apple photo and motion pairs.';
    $('#empty-state [data-action="add"]').classList.add('hidden');
  }
  selectionUI();
  renderCategories();
}

function renderSimilarGallery() {
  const data = state.similarData, lookup = new Map(state.photos.map(photo => [photo.id, photo]));
  $('#gallery').innerHTML = '<p class="analysis-coverage">Visual fingerprints ready for ' + number(data.analyzed) + ' of ' + number(data.image_total) + ' matching images. ' + (data.analyzed < data.image_total ? 'Choose Analyze photos to include the remaining images.' : 'Very plain images are excluded from visual matching.') + '</p>'
    + (data.limited ? '<div class="callout warning">' + icon('info') + '<span>Search limit reached. Use a source, year or month filter for a complete smaller search.</span></div>' : '')
    + state.similarPairs.map((pair, index) => '<section class="similar-pair"><div class="group-heading"><h2>Possible match ' + number(index + 1) + '</h2><span>Review both originals</span></div><div class="comparison-grid">' + [pair.left,pair.right].map(id => photoCard(lookup.get(id))).join('') + '</div></section>').join('');
  $('#result-count').textContent = number(data.total_pairs) + ' possible match pairs';
  $('#selection-count').textContent = '';
  $('#load-more').textContent = 'Load more pairs';
  $('#load-more').classList.toggle('hidden', state.similarPairs.length >= data.total_pairs);
  $('#clear-selection').classList.add('hidden'); $('#set-category').classList.add('hidden');
  $('#empty-state').classList.toggle('hidden', !!data.total_pairs);
  $('#empty-title').textContent = data.analyzed ? 'No possible visual matches found.' : 'Ready to look for similar photos.';
  $('#empty-description').textContent = data.analyzed < data.image_total ? 'Choose Analyze photos to build fingerprints for the remaining images.' : 'This conservative check looks for close resized or recompressed copies. Other similar images may not match.';
  $('#empty-state [data-action="add"]').classList.add('hidden');
  selectionUI();
}
async function refresh(append = false) {
  if (state.locked) return;
  if (!state.summary) await summary();
  setHeading();
  if (state.view === 'people' && !state.person) {
    $('#gallery').innerHTML = '';
    $('#empty-state').classList.add('hidden');
    $('#load-more').classList.add('hidden');
    renderPeople();
    return;
  }
  const sequence = ++state.request;
  const params = queryParams();
  if (state.view === 'duplicates' && state.duplicateMode === 'similar') {
    params.set('offset', append ? state.similarPairs.length : 0);
    const data = await api('/duplicates/similar?' + params);
    if (sequence !== state.request) return;
    state.similarData = data;
    state.similarPairs = append ? state.similarPairs.concat(data.pairs) : data.pairs;
    const photos = new Map((append ? state.photos : []).map(photo => [photo.id,photo]));
    for (const photo of data.photos) photos.set(photo.id, photo);
    state.photos = [...photos.values()]; state.total = state.photos.length;
    renderSimilarGallery(); return;
  }
  if (state.view === 'albums' && !append) {
    const albumParams = new URLSearchParams(params);
    albumParams.delete('kind'); albumParams.delete('month');
    albumParams.delete('live_only');
    if (state.albumKind) albumParams.delete('year');
    const data = await api('/albums?' + albumParams);
    if (sequence !== state.request) return;
    state.albumData = data;
    if (!state.albumKind) {
      state.photos = []; state.total = data.total;
      $('#gallery').innerHTML = ''; $('#empty-state').classList.add('hidden'); $('#load-more').classList.add('hidden');
      renderAlbums(); return;
    }
  }
  params.set('offset', append ? state.photos.length : 0);
  const result = await api('/photos?' + params);
  if (sequence !== state.request) return;
  state.photos = append ? state.photos.concat(result.photos) : result.photos;
  state.total = result.total;
  state.kindCounts = result.kind_counts || [];
  renderGallery();
  if (state.view === 'albums') {
    renderAlbums();
    renderPhotoTypes((state.albumData?.albums || []).find(album => album.kind === 'Photos') || {count:0, live:0});
  }
}
async function navigate(view, library = '', person = '') {
  state.request++;
  state.view = view; state.library = library; state.person = person; state.month = '';
  state.albumKind = ''; state.albumData = null;
  state.photosLiveOnly = false;
  state.similarData = null; state.similarPairs = []; state.duplicateMode = 'exact';
  if (view === 'albums') $('#sort').value = 'newest';
  state.selected.clear();
  $('#search').value = ''; $('#filter-year').value = ''; $('#filter-type').value = ''; $('#filter-kind').value = '';
  await summary(); await refresh();
  window.scrollTo({top:0, behavior:'instant'});
}
async function resetFilters() {
  state.photosLiveOnly = false;
  $('#search').value = ''; $('#filter-year').value = ''; $('#filter-type').value = ''; $('#filter-kind').value = '';
  state.month = ''; state.selected.clear(); await refresh();
}
function addFolder() {
  modal('A new home for old memories.', '<p>Connect a photo backup folder from this PC or an external drive. Framekeep reads the files in place and builds a local thumbnail library.</p><label class="field-label" for="source-path">Photo folder</label><div class="input-row"><input id="source-path" placeholder="C:\\Users\\you\\Pictures\\Phone backup" autocomplete="off"><button class="button outline" data-browse="source-path">' + icon('folder') + 'Browse</button></div><div class="field-hint">Includes subfolders · JPG, PNG, HEIC, HEIF, WebP, GIF, TIFF, BMP & AVIF</div><div class="callout">' + icon('shield') + '<span>Your originals stay untouched. Photos, thumbnails, and face data are stored on this computer. No account needed.</span></div>' + actions('start-scan', 'Connect & scan'));
}
async function browse(inputId, button) {
  button.disabled = true;
  const input = $('#' + inputId);
  const old = button.innerHTML;
  button.textContent = 'Choosing…';
  try {
    const result = await post('/pick-folder');
    if (result.path && input.isConnected) {input.value = result.path; input.dispatchEvent(new Event('input', {bubbles:true}));}
  } finally {button.disabled = false; button.innerHTML = old;}
}
async function startJob(path, body) {
  const result = await post(path, body);
  state.job = result.job_id;
  state.finished = null;
  $('#modal').close();
  await pollJob();
}
function organize() {
  previewPlan = null;
  const count = state.selected.size || state.total;
  if (!count) return toast('Choose some photos or videos first.');
  modal('Everything in its right place.', '<p>Create an organized copy of <strong>' + number(count) + ' ' + (state.selected.size ? 'selected' : 'matching') + ' items</strong>. Preview the folder structure before anything is copied.</p><label class="field-label">Folder structure</label><div class="layout-grid">' + [
    ['kind_date','Category → year → month','Photos / 2026 / April / image.jpg'],
    ['date','By date','2025 / 06 / photo.jpg'], ['date_type','Date & format','2025 / 06 / JPG / photo.jpg'], ['type','By format','HEIC / photo.heic'], ['people','By people','People / Alex + Sam / photo.jpg'], ['kind','By category','Screenshots / image.png'], ['date_kind','Date & category','2025 / 06 / Illustrations / image.jpg']
  ].map(([value,label,example],i) => '<label class="layout-option"><input type="radio" name="layout" value="' + value + '"' + (i === 0 ? ' checked' : '') + '><span><strong>' + label + '</strong><small>' + example + '</small></span></label>').join('') + '</div><label class="field-label" for="output-path">Copy to a separate folder</label><div class="input-row"><input id="output-path" placeholder="Choose an output folder" autocomplete="off"><button class="button outline" data-browse="output-path">' + icon('folder') + 'Browse</button></div><div class="field-hint">Category → year → month creates Photos, Screenshot, Illustration and Video folders, with English month names. Needs review and Animated keep their own folders. Only selected items or current filters are included. Linked Live Photos include both original resources together.</div><label class="check-label"><input type="checkbox" id="create-run-folder">Put this run in a new Framekeep folder</label><div class="field-hint">Unchecked: copy directly into the chosen folder. Existing files are kept; name conflicts get a numbered suffix. A unique CSV manifest records each run.</div><label class="check-label"><input type="checkbox" id="skip-duplicates" checked>Copy only one of each exact duplicate</label><div id="plan-result"></div><div class="modal-actions"><button class="button outline" id="preview-plan">Preview folders</button><button class="button primary" id="copy-plan" disabled>Create verified copies ' + icon('arrow') + '</button></div>');
  for (const input of $$('input', $('#modal'))) input.addEventListener('input', () => {
    previewPlan = null; $('#copy-plan').disabled = true; $('#plan-result').innerHTML = '';
  });
  for (const input of $$('input[name="layout"]', $('#modal'))) input.addEventListener('change', () => {
    $('#create-run-folder').checked = input.value !== 'kind_date';
  });
}
async function plan() {
  const button = $('#preview-plan');
  button.disabled = true;
  try {
    const f = state.selected.size ? {ids:[...state.selected],sort:$('#sort').value} : filters();
    previewPlan = await post('/organize/preview', {destination:$('#output-path').value,layout:$('input[name="layout"]:checked').value,filters:f,skip_duplicates:$('#skip-duplicates').checked,create_run_folder:$('#create-run-folder').checked});
    $('#plan-result').innerHTML = '<div class="plan-preview"><strong>' + quantity(previewPlan.photos, 'photo') + ' · ' + quantity(previewPlan.videos, 'video') + ' · ' + bytes(previewPlan.bytes) + '</strong><p>' + previewPlan.skipped + ' extra duplicates skipped. Original files stay untouched.</p><pre>' + esc(previewPlan.destination + (previewPlan.create_run_folder ? '/Framekeep-[run]/' : '/') + '\n' + previewPlan.sample.map(path => '  ' + path).join('\n')) + '</pre>' + (previewPlan.folders.length ? '<p>Main folders: ' + esc(previewPlan.folders.join(', ')) + '.</p>' : '') + (previewPlan.fallback_dates ? '<p>Check dates: ' + previewPlan.fallback_dates + ' items use file-modified dates because no capture or filename date was available.</p>' : '') + '</div>';
    $('#copy-plan').disabled = false;
    $('#modal-error').textContent = '';
  } finally {button.disabled = false;}
}
function renderPeople() {
  const people = state.summary.people;
  $('#people-view').innerHTML = '<div class="people-intro"><div><h2>A familiar face goes a long way.</h2><p>Find faces locally and suggest groups of the same person. Review, name, merge, or separate matches yourself. Results can be wrong, especially for children, profiles, and low-resolution images.</p></div><button class="button primary" id="' + (state.ready ? 'analyze-people' : 'setup-people') + '">' + icon('spark') + (state.ready ? 'Find people' : 'Set up people') + '</button></div>' + (people.length ? '<div class="people-grid">' + people.map(person => '<article class="person-card"><button class="person-open" data-person="' + person.id + '"><img data-media="/api/faces/' + person.face_id + '" alt="Face preview for ' + esc(person.name) + '"><strong>' + esc(person.name) + '</strong></button><p>' + number(person.count) + ' photos</p><button class="text-button" data-manage-person="' + person.id + '">Review & name group ' + icon('arrow') + '</button></article>').join('') + '</div>' : '<div class="empty-state"><div class="empty-symbol">' + icon('people') + '</div><h2>Good company starts here.</h2><p>' + (state.summary.total ? 'Choose “' + (state.ready ? 'Find people' : 'Set up people') + '” to look for faces in your library. Sample illustrations contain no faces.' : 'Connect a folder with photos of people to get started.') + '</p></div>');
}
function setupPeople() {
  modal('Familiar faces. On your device.', '<p>Download two OpenCV models (about 39 MB) from the official OpenCV Zoo repository. This is a one-time download. Photo analysis runs on your PC, including when you are offline.</p><div class="callout">' + icon('shield') + '<span>No photo or face data is sent to a recognition service. Face previews and similarity vectors are saved only in your local Framekeep data folder.</span></div><p>Groups are suggestions. You can correct mismatches, name a group, or merge two groups later.</p>' + actions('install-people','Download & find people'));
}
async function managePerson(id) {
  const person = state.summary.people.find(p => p.id === Number(id));
  if (!person) return;
  const result = await api('/people/' + id + '/faces');
  modal('A name to a familiar face.', '<label class="field-label" for="person-name">Group name</label><input class="text-input" id="person-name" maxlength="80" value="' + esc(person.name) + '"><button class="button outline" id="save-person" data-id="' + id + '">Save name</button><label class="field-label">Same person in another group?</label><div class="input-row"><select id="merge-target"><option value="">Choose a group to merge into</option>' + state.summary.people.filter(p => p.id !== Number(id)).map(p => '<option value="' + p.id + '">' + esc(p.name) + '</option>').join('') + '</select><button class="button outline" id="merge-person" data-id="' + id + '">Merge groups</button></div><label class="field-label">Review suggested matches</label><p>Separate a photo if it belongs to someone else. This puts its matches into a new group for you to review.</p><div class="face-review">' + result.faces.map(face => '<div class="face-review-item"><img data-media="/api/faces/' + face.id + '" alt="' + esc(face.filename) + '" title="' + esc(face.filename) + '"><button class="text-button" data-split="' + face.photo_id + '" data-group="' + id + '">Separate photo</button></div>').join('') + '</div>');
}
function openPhoto(id) {
  const photo = state.photos.find(p => p.id === Number(id));
  if (!photo) return;
  stopMotion();
  state.detail = photo.id;
  const video = photo.media_type === 'video';
  $('#detail-image').classList.toggle('hidden', video);
  if (!$('#detail-video')) $('#detail-image').insertAdjacentHTML('afterend', '<div id="detail-video" class="video-details hidden"></div>');
  $('#detail-video').classList.toggle('hidden', !video);
  if (video) {
    delete $('#detail-image').dataset.media; $('#detail-image').removeAttribute('src');
    $('#detail-video').innerHTML = '<button class="video-start" id="play-video" aria-label="Play video: ' + esc(photo.filename) + '" aria-describedby="video-status" aria-busy="false">' + mediaPreview(photo) + '</button><p id="video-status" role="status">Click the play icon to watch here with sound. A private playback copy is prepared on this PC the first time.</p>';
  } else $('#detail-image').dataset.media = '/api/photos/' + photo.id + '/preview';
  $('#detail-image').alt = photo.filename;
  const people = state.summary.people.filter(p => photo.person_ids.includes(p.id));
  $('#photo-details').innerHTML = '<h2>' + esc(photo.filename) + '</h2><div class="detail-date">' + dateLabel(photo.captured_at) + '</div><dl><dt>Format</dt><dd>' + esc(photo.extension) + '</dd><dt>Dimensions</dt><dd>' + (photo.width && photo.height ? photo.width + ' × ' + photo.height : 'Not available') + '</dd><dt>Size</dt><dd>' + bytes(photo.size) + '</dd><dt>Date source</dt><dd>' + esc(photo.date_source) + '</dd><dt>Camera</dt><dd>' + esc(photo.camera || 'Not available') + '</dd><dt>Exact copies</dt><dd>' + photo.copies + '</dd></dl>' + (photo.date_source === 'File modified' ? '<div class="callout warning">' + icon('info') + '<span>This date comes from the file, and may differ from the day the photo was taken.</span></div>' : '') + '<div>' + people.map(p => '<span class="detail-person">' + esc(p.name) + '</span>').join('') + '</div><label for="photo-tags">Tags (comma separated)</label><input class="text-input" id="photo-tags" value="' + esc(photo.tags) + '" maxlength="1000" placeholder="holiday, family, 2024"><button class="button outline" id="save-tags">Save tags</button><button class="button outline" id="detail-favorite">' + icon('heart') + (photo.favorite ? 'Remove from favorites' : 'Add to favorites') + '</button><button class="button outline" id="reveal-photo">' + icon('folder') + 'Show original in folder</button><div class="detail-source">' + esc(photo.path) + '</div>';
  $('#photo-prev').disabled = state.photos[0].id === photo.id;
  $('label[for="photo-tags"]', $('#photo-details')).insertAdjacentHTML('beforebegin', categoryEditor(photo));
  $('#photo-details').insertAdjacentHTML('beforeend', '<section id="detail-matches" class="detail-matches"><h3>Copies & similar photos</h3><p>Checking indexed matches…</p></section>');
  if (photo.live_motion_id) {
    $('#photo-details h2').insertAdjacentHTML('afterend', '<div class="live-controls"><span>◉ Apple Live Photo</span><button class="button primary" id="play-live">Play Live Photo</button><button class="button outline hidden" id="show-still">Show still photo</button><button class="text-button" data-reveal-motion="' + photo.live_motion_id + '">Show original motion file</button><p id="motion-status" role="status">Motion preview is created locally when you press Play. Originals stay untouched.</p></div>');
  }
  if (video) {
    $('dl', $('#photo-details')).insertAdjacentHTML('beforeend', '<dt>Duration</dt><dd>' + durationLabel(photo.duration) + '</dd>');
    $('.detail-date', $('#photo-details')).insertAdjacentHTML('afterend', '<section class="playback-options"><label for="video-speed">Playback speed</label><select id="video-speed" class="text-input"><option value="0.5">0.5×</option><option value="1" selected>Normal</option><option value="1.5">1.5×</option><option value="2">2×</option></select><p id="playback-status" role="status">Use the player controls to pause, seek, change volume or enter fullscreen.</p><p class="field-hint">Optimized local playback, up to 720p. Originals keep their full quality. Videos up to 30 minutes / 2 GB; preparation may take up to 3 minutes.</p></section>');
  }
  $('#photo-next').disabled = state.photos.findIndex(p => p.id === photo.id) >= state.total - 1;
  if (!$('#photo-position')) $('.photo-stage').insertAdjacentHTML('beforeend', '<span id="photo-position" class="photo-position" aria-live="polite"></span>');
  $('#photo-position').textContent = (state.photos.findIndex(p => p.id === photo.id) + 1) + ' of ' + number(state.total);
  if (!$('#photo-modal').open) $('#photo-modal').showModal();
  loadPhotoMatches(photo.id);
}

function stopMotion() {
  state.motionRequest++;
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  if (state.playbackAbort) state.playbackAbort.abort();
  state.playbackAbort = null;
  if (state.playbackJob && state.token) fetch('/api/playback/' + state.playbackJob, {method:'DELETE', headers:{'X-Framekeep-Token':state.token}, keepalive:true}).catch(() => {});
  state.playbackJob = null;
  for (const video of $$('#live-video,#playback-video')) {video.pause(); video.removeAttribute('src'); video.load(); video.classList.add('hidden');}
  if (state.motionURL) URL.revokeObjectURL(state.motionURL);
  state.motionURL = null;
}

async function playVideo() {
  const photo = state.photos.find(row => row.id === state.detail);
  if (photo?.media_type !== 'video' || $('#play-video')?.disabled) return;
  const request = ++state.motionRequest, id = photo.id;
  const controller = new AbortController(); state.playbackAbort = controller;
  const current = () => request === state.motionRequest && !state.locked && state.detail === id && $('#photo-modal').open;
  const status = message => {if (current()) {$('#video-status').textContent = message; $('#playback-status').textContent = message;}};
  $('#play-video').disabled = true;
  $('#play-video').setAttribute('aria-busy', 'true');
  status('Preparing the full video on this PC… You can close this view to cancel.');
  let jobId = null;
  try {
    // Receive the small preparation receipt even after closing, so its worker can be cancelled.
    let job = await api('/photos/' + id + '/playback', {method:'POST'});
    jobId = job.id;
    if (!current()) return;
    state.playbackJob = jobId;
    while (job.state === 'preparing') {
      await new Promise(resolve => setTimeout(resolve, 500));
      if (!current()) return;
      job = await api('/playback/' + jobId, {signal:controller.signal});
    }
    if (job.state !== 'ready') throw new Error(job.message || 'Video could not be prepared.');
    if (!current()) return;
    status('Loading local playback…');
    const response = await fetch('/api/photos/' + id + '/playback', {headers:{'X-Framekeep-Token':state.token}, signal:controller.signal});
    if (response.status === 401) {lockApp(); throw new Error('Open Framekeep again to unlock this session.');}
    if (!response.ok) {const error = await response.json(); throw new Error(error.detail || 'Playback unavailable.');}
    if (!response.headers.get('Content-Type')?.startsWith('video/mp4')) throw new Error('Playback unavailable.');
    const blob = await response.blob();
    if (!current()) return;
    if (state.motionURL) URL.revokeObjectURL(state.motionURL);
    state.motionURL = URL.createObjectURL(blob);
    if (!$('#playback-video')) $('#detail-image').insertAdjacentHTML('afterend', '<video id="playback-video" class="live-video" controls playsinline controlslist="nodownload noremoteplayback" disableremoteplayback aria-label="Video player"></video>');
    const video = $('#playback-video'); video.src = state.motionURL; video.playbackRate = Number($('#video-speed').value);
    video.classList.remove('hidden'); $('#detail-video').classList.add('hidden');
    video.onerror = () => status('This playback copy could not be played. Use Show original in folder to open it with another player.');
    status('Full video · Local playback · Original unchanged.');
    try {await video.play();} catch {status('Video ready. Press Play on the player controls.');}
  } catch (error) {
    if (error.name !== 'AbortError') status(error.message);
  } finally {
    if (!current() && jobId && state.token) fetch('/api/playback/' + jobId, {method:'DELETE', headers:{'X-Framekeep-Token':state.token}, keepalive:true}).catch(() => {});
    if (current()) {state.playbackJob = null; state.playbackAbort = null; $('#play-video').disabled = false; $('#play-video').setAttribute('aria-busy', 'false');}
  }
}

async function playMotion() {
  const photo = state.photos.find(row => row.id === state.detail);
  if (!photo?.live_motion_id) return;
  const request = ++state.motionRequest, id = photo.id;
  $('#play-live').disabled = true; $('#motion-status').textContent = 'Preparing a private motion preview…';
  try {
    const response = await fetch('/api/photos/' + id + '/motion', {method:'POST', headers:{'X-Framekeep-Token':state.token}});
    if (response.status === 401) {lockApp(); throw new Error('Open Start Framekeep.cmd to unlock this session.');}
    if (!response.ok) {const error = await response.json(); throw new Error(error.detail || 'Motion preview unavailable.');}
    if (!response.headers.get('Content-Type')?.startsWith('video/mp4')) throw new Error('Motion preview unavailable.');
    const blob = await response.blob();
    if (request !== state.motionRequest || state.locked || state.detail !== id || !$('#photo-modal').open) return;
    if (state.motionURL) URL.revokeObjectURL(state.motionURL);
    state.motionURL = URL.createObjectURL(blob);
    if (!$('#live-video')) $('#detail-image').insertAdjacentHTML('afterend', '<video id="live-video" class="live-video" controls playsinline muted loop></video>');
    const video = $('#live-video'); video.src = state.motionURL; video.classList.remove('hidden');
    $('#detail-image').classList.add('hidden'); $('#show-still').classList.remove('hidden');
    $('#motion-status').textContent = 'Live Photo motion · Local preview, up to 6 seconds. Unmute for sound if available.';
    await video.play();
  } catch (error) {
    if (!state.locked && request === state.motionRequest && state.detail === id && $('#motion-status')) $('#motion-status').textContent = error.message;
  } finally {
    if (!state.locked && request === state.motionRequest && $('#play-live')) $('#play-live').disabled = false;
  }
}

async function loadPhotoMatches(id) {
  try {
    const data = await api('/photos/' + id + '/matches');
    if (state.locked || state.detail !== id || !$('#photo-modal').open) return;
    state.matchPhotos = new Map([...data.exact, ...data.similar].map(photo => [photo.id, photo]));
    const items = (photos, label) => photos.map(photo => '<button class="match-photo" data-compare-photo="' + photo.id + '">' + mediaPreview(photo) + '<span>' + esc(photo.filename) + '</span><small>' + label + '</small></button>').join('');
    $('#detail-matches').innerHTML = '<h3>Copies & similar photos</h3><p>' + number(data.exact_total) + ' other exact copies · ' + number(data.similar_total) + ' possible visual matches</p><div class="match-photos">' + items(data.exact, 'Exact copy') + items(data.similar, 'Possible match') + '</div><p class="field-hint">' + (!data.analyzed ? 'Choose Analyze photos in Needs review or Duplicates to check visual matches.' : 'Visual matches are suggestions among analyzed images. Compare before deciding.') + '</p>';
  } catch (error) {
    if (!state.locked && state.detail === id && $('#detail-matches')) $('#detail-matches').innerHTML = '<p>' + esc(error.message) + '</p>';
  }
}

function comparePhoto(id) {
  const original = state.photos.find(photo => photo.id === state.detail), other = state.matchPhotos?.get(Number(id));
  if (!original || !other) return;
  const exact = original.sha256 === other.sha256;
  modal(exact ? 'Identical file contents.' : 'Compare possible copies.', '<p>' + (exact ? 'These files have the same SHA-256 hash.' : 'These images have similar visual fingerprints. Check both images; this does not prove they are duplicates.') + ' Your originals stay untouched.</p><div class="comparison-grid comparison-preview">' + [original,other].map(photo => '<figure>' + (photo.media_type === 'video' ? mediaPreview(photo) : '<img data-media="/api/photos/' + photo.id + '/preview" alt="' + esc(photo.filename) + '">') + '<figcaption><strong>' + esc(photo.filename) + '</strong><span>' + photo.width + ' × ' + photo.height + ' · ' + bytes(photo.size) + '</span><small>' + esc(photo.path) + '</small></figcaption></figure>').join('') + '</div><div class="modal-actions"><button class="button primary" data-action="close-modal">Done</button></div>');
}
async function nextPhoto(direction) {
  if (state.detailBusy) return;
  const current = state.detail;
  let index = state.photos.findIndex(p => p.id === current);
  try {
    if (direction > 0 && index === state.photos.length - 1 && state.photos.length < state.total) {
      state.detailBusy = true; $('#photo-next').disabled = true;
      await refresh(true);
      if (!$('#photo-modal').open || state.detail !== current) return;
      index = state.photos.findIndex(p => p.id === current);
    }
    if (state.photos[index + direction]) openPhoto(state.photos[index + direction].id);
  } catch (error) {toast(error.message);}
  finally {
    state.detailBusy = false;
    if ($('#photo-modal').open) $('#photo-next').disabled = state.photos.findIndex(p => p.id === state.detail) >= state.total - 1;
  }
}
async function favorite(id, detail = false) {
  const photo = state.photos.find(p => p.id === Number(id));
  if (!photo) return;
  await api('/photos/' + id, {method:'PATCH',body:JSON.stringify({favorite:!photo.favorite})});
  photo.favorite = !photo.favorite;
  if (detail) openPhoto(id);
  await summary();
  if (state.view === 'favorites' && !detail) await refresh(); else renderGallery();
}
function report(job) {
  const result = job.result || {};
  const errors = result.errors || [];
  const isExport = typeof result.copied === 'number';
  const isScan = typeof result.found === 'number';
  const isCategories = typeof result.categorized === 'number';
  const isReview = typeof result.reviewed === 'number';
  const isLive = typeof result.live_photos === 'number';
  const skipped = result.skipped_media || {};
  const skippedText = [[skipped.videos, 'videos'], [skipped.metadata, 'metadata files'], [skipped.other, 'other non-photo files']]
    .filter(([count]) => count).map(([count, label]) => number(count) + ' ' + label).join(', ');
  const description = result.edited_copy ? 'Your edited JPEG copy was saved and verified. Your original stays untouched.' : isExport
    ? quantity(result.copied_photos ?? result.copied, 'photo') + ' and ' + quantity(result.copied_videos, 'video') + ' copied and verified. ' + number(result.skipped) + ' extra duplicates skipped.'
    : isLive ? number(result.live_photos) + ' verified Live Photos linked. ' + number(result.metadata_checked) + ' file metadata records checked. Each pair is shown as one photo; both originals are kept.'
    : isReview ? number(result.reviewed) + ' visual fingerprints built or refreshed. ' + number(result.category_updates) + ' category suggestions checked; ' + number(result.kept_manual) + ' manual categories kept. Close calls still need review.'
    : isCategories ? (result.categorized ? number(result.categorized) + ' image categories suggested.' : result.error_count ? 'Category detection finished.' : 'Categories are up to date.') + ' ' + number(result.kept_manual) + ' manual choices and ' + number(result.already_classified) + ' existing suggestions kept.'
    : isScan ? number(result.found) + ' photos and ' + number(result.videos) + ' videos found; ' + number(result.indexed) + ' items added or updated.' : esc(job.label) + ' finished.';
  const scanDetails = (isScan && skippedText ? '<div class="callout">' + icon('info') + '<span>Skipped ' + skippedText + '. Videos use their container metadata; unsupported files stay in the source. Original files stay untouched.</span></div>' : '')
    + (result.extension_mismatches ? '<p>' + number(result.extension_mismatches) + ' files have a filename extension that does not match their contents. Organized copies use the detected extension; originals keep their names.</p>' : '')
    + (isCategories ? '<div class="category-report">' + categories.filter(kind => result.category_counts[kind]).map(kind => '<span>' + esc(kind) + ' <strong>' + number(result.category_counts[kind]) + '</strong></span>').join('') + '</div><p>Suggestions can be wrong. Open an image or select several, then use Set category to correct them. Your originals stay untouched.</p>' : '');
  modal(isExport ? 'Your organized copies are ready.' : 'Task report', '<p>' + description + (result.cancelled ? ' The task was cancelled; completed work has been kept.' : '') + '</p>' + scanDetails + (result.edited_copy ? '<div class="path-box">' + esc(result.edited_copy) + '</div>' : '') + (result.destination ? '<label class="field-label">Output folder</label><div class="path-box">' + esc(result.destination) + '</div><p class="field-hint">A CSV manifest in this folder records every verified copy and any skipped errors.</p>' : '') + (errors.length ? '<div class="callout warning">' + icon('info') + '<span>' + number(result.error_count || errors.length) + ' files could not be processed. First ' + errors.length + ' shown below.</span></div><div class="report-list">' + errors.map(error => '<p><strong>' + esc(error.file) + '</strong><br>' + esc(error.reason) + '</p>').join('') + '</div>' : '') + '<div class="modal-actions"><button class="button primary" data-action="close-modal">Done ' + icon('check') + '</button></div>');
}
async function pollJob() {
  if (polling || state.locked) return;
  polling = true;
  try {
    const {job} = await api('/job');
    if (!job) return;
    const active = ['queued','running'].includes(job.state);
    $('#job-dock').classList.toggle('hidden', !active);
    if (active) {
      state.job = job.id;
      $('#job-label').textContent = job.label;
      $('#job-message').textContent = job.message;
      $('#job-count').textContent = job.total ? number(job.done) + ' / ' + number(job.total) : '';
      if (job.total) $('#job-progress').value = job.done / job.total * 100;
      else $('#job-progress').removeAttribute('value');
    } else if (state.job === job.id && state.finished !== job.id) {
      state.finished = job.id;
      const session = await api('/session');
      state.ready = session.people_ready; state.categoriesReady = session.categories_ready;
      await summary();
      if (job.label === 'Indexing photos') {
        state.library = String(state.summary.libraries.filter(l => !l.demo).at(-1)?.id || '');
        state.view = 'albums'; state.albumKind = ''; state.person = ''; state.month = ''; state.selected.clear();
      }
      if (job.label === 'Opening sample library') {
        state.library = String(state.summary.libraries.find(l => l.demo)?.id || '');
        state.view = 'albums'; state.albumKind = ''; state.person = ''; state.month = '';
      }
      await refresh();
      if (job.state === 'failed') toast(job.message);
      else if (job.label === 'Downloading people models' && state.ready && job.state !== 'cancelled') {
        polling = false;
        await startJob('/people/analyze');
        return;
      } else if (job.label === 'Downloading category model' && state.categoriesReady && job.state !== 'cancelled') {
        polling = false;
        await startJob(state.afterCategoryInstall === 'review' ? '/review/analyze' : '/categories/analyze', {filters:state.afterCategoryInstall === 'review' ? state.reviewFilters || {} : state.categoryFilters || {}});
        return;
      } else if (['Indexing photos','Rescanning library','Detecting image categories','Reviewing categories and similar photos','Linking Live Photos'].includes(job.label) || job.result?.edited_copy || job.result?.error_count || typeof job.result?.copied === 'number') report(job);
      else toast(job.state === 'cancelled' ? 'Task cancelled. Completed work has been kept.' : job.label === 'Finding familiar faces' ? number(job.result?.faces) + ' faces found. Review the suggested groups.' : 'Your library is up to date.');
    }
  } catch (error) {
    if (!state.summary) toast('Framekeep is unavailable. Reopen the launcher to reconnect.');
  } finally {polling = false;}
}
function about() {
  modal('Your memories, back in order.', '<p>Framekeep is a private photo organizer built for years of old phone backups. It runs on this computer in its own Windows desktop window. Your library stays local.</p><ul class="feature-list"><li>Browse automatic albums by category, year and month.</li><li>View iPhone Live Photos as one memory and play their motion locally.</li><li>Recognize videos from real frame thumbnails in albums and galleries.</li><li>View pictures in the app with continuous next/previous navigation.</li><li>Review local AI category suggestions and compare possible visual copies.</li><li>Find exact duplicates using SHA-256.</li><li>Suggest people groups with OpenCV YuNet & SFace.</li><li>Preview, copy, and verify an organized collection.</li></ul><div class="callout">' + icon('info') + '<span>Dates use camera metadata, then a filename date, then file-modified time. Categories use local visual suggestions and available metadata. You can correct them manually. People matches need review. Linked Live Photos support local motion playback with FFmpeg. Ordinary MOV/MP4 videos play here with FFmpeg, including HEVC iPhone recordings. First playback prepares a local copy; the original keeps its full quality. Playback supports videos up to 30 minutes and 2 GiB. Camera RAW files are not indexed.</span></div><p>Photo files, thumbnails, face previews, SQLite data, and export manifests belong on your PC. Publish only the application source and sample illustrations to GitHub.</p><p class="field-hint">Version 1.13.0 · No accounts. No telemetry. No photo uploads.</p>');
}
document.addEventListener('click', async event => {
  const card = event.target.closest('#gallery [data-media-id]');
  if (card && (event.ctrlKey || event.metaKey || event.shiftKey)) {
    if (event.target.matches('.photo-select')) {
      // Let the checkbox finish toggling, then apply the range in its change event.
      pendingSelectionModifiers.set(event.target, {ctrlKey:event.ctrlKey, metaKey:event.metaKey, shiftKey:event.shiftKey});
      return;
    }
    event.preventDefault();
    selectWithModifiers(Number(card.dataset.mediaId), event);
    return;
  }
  const target = event.target.closest('button');
  if (!target) return;
  try {
    if (state.locked && target.dataset.action !== 'about' && target.dataset.action !== 'close-modal') return toast('Open Start Framekeep.cmd to unlock this session.');
    if (target.dataset.view) return await navigate(target.dataset.view);
    if (target.dataset.duplicateMode) {
      state.duplicateMode = target.dataset.duplicateMode; state.selected.clear();
      state.similarPairs = []; state.similarData = null; return await refresh();
    }
    if (target.dataset.acceptSuggestion) return await acceptSuggestion(target.dataset.acceptSuggestion);
    if (target.dataset.galleryDensity) return setGalleryDensity(target.dataset.galleryDensity);
    if (target.dataset.comparePhoto) return comparePhoto(target.dataset.comparePhoto);
    if (target.dataset.revealMotion) {await post('/reveal', {photo_id:Number(target.dataset.revealMotion)}); return;}
    if (target.dataset.library) return await navigate('albums', target.dataset.library);
    if (target.hasAttribute('data-open-album')) return await openAlbum(target.dataset.openAlbum, target.dataset.albumMonth || '');
    if (target.hasAttribute('data-photos-live')) {
      state.photosLiveOnly = target.dataset.photosLive === '1'; state.selected.clear(); return await refresh();
    }
    if (target.hasAttribute('data-album-home')) return await albumHome();
    if (target.hasAttribute('data-album-year')) {
      $('#filter-year').value = target.dataset.albumYear; state.month = ''; state.selected.clear(); return await refresh();
    }
    if (target.hasAttribute('data-album-month')) {
      state.month = target.dataset.albumMonth; state.selected.clear();
      if (state.month) $('#filter-year').value = state.month.slice(0,4);
      return await refresh();
    }
    if (target.hasAttribute('data-kind')) {
      if (state.view === 'albums') return target.dataset.kind ? await openAlbum(target.dataset.kind, state.month) : await albumHome();
      $('#filter-kind').value = target.dataset.kind; state.selected.clear(); return await refresh();
    }
    if (target.dataset.person) return await navigate('people', '', target.dataset.person);
    if (target.dataset.rescan) return await startJob('/libraries/' + target.dataset.rescan + '/rescan');
    if (target.dataset.photo) return openPhoto(target.dataset.photo);
    if (target.dataset.favorite) return await favorite(target.dataset.favorite);
    if (target.dataset.browse) return await browse(target.dataset.browse, target);
    if (target.dataset.managePerson) return await managePerson(target.dataset.managePerson);
    if (target.dataset.month) {state.month = state.month === target.dataset.month ? '' : target.dataset.month; state.selected.clear(); return await refresh();}
    if (target.dataset.split) {
      await post('/people/' + target.dataset.group + '/split', {photo_id:Number(target.dataset.split)});
      await summary(); await refresh(); await managePerson(target.dataset.group);
      return;
    }
    switch (target.dataset.action) {
      case 'add': return addFolder();
      case 'demo': return await startJob('/demo');
      case 'organize': return organize();
      case 'close-modal': return $('#modal').close();
      case 'close-photo': return $('#photo-modal').close();
      case 'about': return about();
      case 'browse-flat': return await navigate('all', state.library);
      case 'clear-selected': state.selected.clear(); return selectionUI();
      case 'analyze-review': return await analyzeReview();
      case 'link-live': return await startJob('/live-photos/analyze', {filters:{library:state.library}});
    }
    switch (target.id) {
      case 'start-scan': return await startJob('/scan',{path:$('#source-path').value});
      case 'reset-filters': return await resetFilters();
      case 'load-more': return await refresh(true);
      case 'select-loaded': return selectDisplayed();
      case 'clear-selection': state.selected.clear(); return renderGallery();
      case 'detect-categories': return await detectCategories();
      case 'album-detect': return await detectCategories();
      case 'install-categories': return await startJob('/categories/install');
      case 'set-category': return setSelectedCategory();
      case 'save-bulk-kind':
        await post('/categories/assign', {ids:state.categoryIds,kind:$('#bulk-kind').value || null});
        $('#modal').close(); state.selected.clear(); await summary(); await refresh(); return toast('Categories saved.');
      case 'save-photo-kind':
        await api('/photos/' + state.detail, {method:'PATCH',body:JSON.stringify({kind:$('#photo-kind').value || null})});
        $('#photo-modal').close(); await summary(); await refresh(); return toast('Category saved.');
      case 'preview-plan': return await plan();
      case 'copy-plan':
        if (previewPlan) return await startJob('/organize/copy', {token:previewPlan.token});
        return;
      case 'setup-people': return setupPeople();
      case 'install-people': return await startJob('/people/install');
      case 'analyze-people': return await startJob('/people/analyze');
      case 'job-cancel': await post('/job/cancel'); return toast('Stopping after the current file…');
      case 'save-person':
        await api('/people/' + target.dataset.id,{method:'PATCH',body:JSON.stringify({name:$('#person-name').value})});
        await summary(); await refresh(); $('#modal').close(); return toast('Group name saved.');
      case 'merge-person':
        if (!$('#merge-target').value) throw new Error('Choose a group to merge into.');
        await post('/people/' + target.dataset.id + '/merge',{target:Number($('#merge-target').value)});
        state.person = ''; await summary(); await refresh(); $('#modal').close(); return toast('People groups merged.');
      case 'save-tags':
        await api('/photos/' + state.detail,{method:'PATCH',body:JSON.stringify({tags:$('#photo-tags').value})});
        state.photos.find(p => p.id === state.detail).tags = $('#photo-tags').value;
        return toast('Tags saved.');
      case 'detail-favorite': return await favorite(state.detail, true);
      case 'reveal-photo': await post('/reveal',{photo_id:state.detail}); return;
      case 'photo-prev': return nextPhoto(-1);
      case 'photo-next': return nextPhoto(1);
      case 'play-live': return await playMotion();
      case 'play-video': return await playVideo();
      case 'show-still': stopMotion(); $('#detail-image').classList.remove('hidden'); $('#show-still').classList.add('hidden'); $('#play-live').disabled = false; return;
    }
  } catch (error) {modalError(error);}
});
document.addEventListener('change', async event => {
  if (event.target.id === 'video-speed' && $('#playback-video')) $('#playback-video').playbackRate = Number(event.target.value);
  if (event.target.matches('.photo-select')) {
    const id = Number(event.target.dataset.select);
    const modifiers = pendingSelectionModifiers.get(event.target);
    pendingSelectionModifiers.delete(event.target);
    if (modifiers) selectWithModifiers(id, modifiers);
    else {
      if (event.target.checked) state.selected.add(id); else state.selected.delete(id);
      state.selectionAnchor = id;
      selectionUI();
    }
  }
  if (event.target.matches('#filter-year,#filter-type,#filter-kind,#sort')) {
    if (event.target.id === 'filter-year') state.month = '';
    state.selected.clear();
    try {await refresh();} catch (error) {toast(error.message);}
  }
});
$('#search').addEventListener('input', () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(async () => {state.selected.clear(); try {await refresh();} catch (error) {toast(error.message);}}, 220);
});
document.addEventListener('keydown', event => {
  if ($('#context-menu')?.matches(':popover-open')) return;
  if (event.key === '/' && !event.target.matches('input,textarea,select') && !$('#modal').open && !$('#photo-modal').open) {event.preventDefault(); $('#search').focus();}
  if ($('#photo-modal').open && !event.target.matches('input,textarea,select,video') && !$('#playback-video:not(.hidden)')) {
    if (event.key === 'ArrowLeft') nextPhoto(-1);
    if (event.key === 'ArrowRight') nextPhoto(1);
  }
});
$('#photo-modal').addEventListener('close', () => {
  stopMotion();
  $('#detail-image').removeAttribute('src');
  $('#detail-image').removeAttribute('data-media');
  refresh().catch(error => toast(error.message));
});
async function init() {
  try {
    const ticket = new URLSearchParams(location.hash.slice(1)).get('launch');
    // Erase the single-use capability before making any API request.
    if (location.hash) history.replaceState(null, '', location.pathname + location.search);
    state.token = sessionStorage.getItem('framekeep.session') || '';
    if (ticket) {
      const bootstrap = await post('/bootstrap', {ticket});
      state.token = bootstrap.token;
      sessionStorage.setItem('framekeep.session', state.token);
    }
    if (!state.token) {lockApp(); return;}
    const session = await api('/session');
    state.ready = session.people_ready; state.locked = false;
    state.categoriesReady = session.categories_ready;
    await summary(); await refresh(); await pollJob();
    setInterval(pollJob, 1100);
  } catch (error) {lockApp(); toast('Open Start Framekeep.cmd to unlock this session.');}
}
init();
