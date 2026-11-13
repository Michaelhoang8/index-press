const $ = (id) => document.getElementById(id),
  el = (tag, text) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    return node;
  };
let listing = [],
  selected = null,
  dirty = false,
  editingBusy = false,
  openRevision = 0,
  searchRevision = 0,
  searchController = null,
  libraryRevision = 0,
  fileRevision = 0;
function setStatus(text) {
  $('status').textContent = text;
  $('library-status').textContent = text;
}
$('library-open').onclick = () => {
  $('library-status').textContent = '';
  $('library-panel').showModal();
};
$('library-close').onclick = () => $('library-panel').close();
async function api(path, data, signal) {
  const response = await fetch(path, {
    method: data ? 'POST' : 'GET',
    headers: data ? { 'Content-Type': 'application/json' } : undefined,
    body: data ? JSON.stringify(data) : undefined,
    signal,
  });
  const body = await response.json();
  if (!response.ok) throw Error(body.error || 'Request failed.');
  return body;
}
function invalidateSearch(message = 'Search settings changed. Search again to update results.') {
  searchRevision++;
  searchController?.abort();
  searchController = null;
  $('results').replaceChildren();
  setStatus(message);
}
function libraryView() {
  const archived = $('show-archive').checked;
  $('library').replaceChildren();
  for (const doc of listing.filter((d) => Boolean(d.archived) === archived)) {
    const button = el('button', doc.title);
    button.append(
      el('small', `#${doc.id} · ${doc.length} tokens${doc.archived ? ' · archived' : ''}`),
    );
    button.onclick = () => openDocument(doc.id);
    $('library').append(button);
  }
  if (!$('library').children.length)
    $('library').append(el('p', archived ? 'No archived documents.' : 'No documents yet.'));
  $('seed').hidden = listing.length > 0;
}
async function refresh() {
  const ticket = ++libraryRevision;
  const data = await api('/api/library');
  if (ticket !== libraryRevision) return;
  listing = data.documents;
  $('library-stats').textContent =
    `${data.active} active · ${data.terms} distinct terms · ${data.tokens} tokens`;
  libraryView();
}
$('show-archive').onchange = libraryView;
function editorBusy(value) {
  editingBusy = value;
  for (const control of $('document-form').elements) control.disabled = value;
}
function setDocument(doc) {
  selected = doc;
  dirty = false;
  fileRevision++;
  $('title').value = doc?.title ?? '';
  $('text').value = doc?.text ?? '';
  $('editor-heading').textContent = doc
    ? `Document #${doc.id} · version ${doc.version}`
    : 'New document';
  $('archive').hidden = !doc;
  $('archive').textContent = doc?.archived ? 'Restore' : 'Archive';
  $('discard').hidden = true;
  $('editor-status').textContent = doc?.archived
    ? 'Archived documents are excluded from search.'
    : '';
}
async function openDocument(id) {
  const ticket = ++openRevision;
  try {
    const doc = await api(`/api/document?id=${id}`);
    if (ticket !== openRevision) return;
    setDocument(doc);
    $('library-panel').close();
    $('editor').showModal();
  } catch (error) {
    setStatus(error.message);
  }
}
$('new').onclick = () => {
  openRevision++;
  setDocument(null);
  $('editor').showModal();
};
function closeEditor(force = false) {
  if (editingBusy) return;
  if (dirty && !force) {
    $('editor-status').textContent =
      'You have unsaved edits. Save them, or explicitly discard them.';
    $('discard').hidden = false;
    return;
  }
  openRevision++;
  fileRevision++;
  $('editor').close();
}
$('close').onclick = () => closeEditor();
$('discard').onclick = () => closeEditor(true);
$('editor').addEventListener('cancel', (event) => {
  event.preventDefault();
  closeEditor();
});
$('document-form').addEventListener('input', (event) => {
  if (['title', 'text'].includes(event.target.id)) {
    dirty = true;
    fileRevision++;
    $('editor-status').textContent = 'Unsaved edits.';
  }
});
async function afterMutation() {
  invalidateSearch('Library updated.');
  await refresh();
  if ($('query').value.trim()) await search();
  else setStatus('Library updated. Enter a query to search.');
}
$('document-form').onsubmit = async (event) => {
  event.preventDefault();
  if (editingBusy) return;
  editorBusy(true);
  const data = { title: $('title').value, text: $('text').value };
  try {
    let id = selected?.id;
    if (selected) await api('/api/save', { id, version: selected.version, document: data });
    else {
      id = (await api('/api/import', { documents: [data] })).ids[0];
    }
    setDocument(await api(`/api/document?id=${id}`));
    $('editor-status').textContent = 'Saved and indexed.';
    await afterMutation();
  } catch (error) {
    $('editor-status').textContent = error.message;
  } finally {
    editorBusy(false);
  }
};
$('archive').onclick = async () => {
  if (!selected || editingBusy) return;
  if (dirty) {
    $('editor-status').textContent = 'Save or discard your edits before changing archive status.';
    return;
  }
  editorBusy(true);
  try {
    await api('/api/archive', {
      id: selected.id,
      version: selected.version,
      archived: !selected.archived,
    });
    setDocument(await api(`/api/document?id=${selected.id}`));
    await afterMutation();
  } catch (error) {
    $('editor-status').textContent = error.message;
  } finally {
    editorBusy(false);
  }
};
$('text-file').onchange = async (event) => {
  const file = event.target.files[0];
  event.target.value = '';
  if (!file) return;
  const ticket = ++fileRevision;
  try {
    if (file.size > 160000) throw Error('Text file exceeds 160 KB.');
    const text = await file.text();
    if (text.length > 40000) throw Error('Text exceeds 40,000 characters.');
    if (ticket !== fileRevision) return;
    $('text').value = text;
    if (!$('title').value.trim()) $('title').value = file.name.replace(/\.txt$/i, '').slice(0, 120);
    dirty = true;
    $('editor-status').textContent = 'Text loaded. Save to add it to the index.';
  } catch (error) {
    if (ticket === fileRevision) $('editor-status').textContent = error.message;
  }
};
function showResults(data) {
  $('results').replaceChildren();
  $('status').textContent =
    `${data.total} matches · showing ${data.results.length} · ${data.corpus_size} indexed documents · average length ${data.average_length.toFixed(1)} tokens`;
  if (!data.results.length) {
    const empty = el('div');
    empty.className = 'empty';
    empty.append(
      el('h2', 'No matching documents.'),
      el('p', 'Try fewer terms, another match mode, or add documents to the collection.'),
    );
    $('results').append(empty);
    return;
  }
  for (const result of data.results) {
    const article = el('article'),
      heading = el('div'),
      title = el('button', result.title),
      score = el('span', `BM25 ${result.score.toFixed(4)}`);
    heading.className = 'result-heading';
    score.className = 'score';
    title.onclick = () => openDocument(result.id);
    heading.append(title, score);
    article.append(heading);
    const snippet = el('p');
    snippet.className = 'snippet';
    if (result.leading_ellipsis) snippet.append('…');
    for (const segment of result.snippet)
      snippet.append(
        segment.match ? el('mark', segment.text) : document.createTextNode(segment.text),
      );
    if (result.trailing_ellipsis) snippet.append('…');
    article.append(snippet);
    const details = el('details');
    details.append(el('summary', `Explain score · ${result.length} document tokens`));
    const scroll = el('div');
    scroll.className = 'scroll';
    const table = el('table'),
      head = el('tr');
    for (const label of ['Term', 'Term count', 'Documents', 'IDF', 'Contribution'])
      head.append(el('th', label));
    const thead = el('thead');
    thead.append(head);
    table.append(thead);
    const tbody = el('tbody');
    for (const term of result.explanation) {
      const row = el('tr');
      for (const value of [
        term.term,
        term.tf,
        term.df,
        term.idf.toFixed(4),
        term.contribution.toFixed(4),
      ])
        row.append(el('td', value));
      tbody.append(row);
    }
    table.append(tbody);
    scroll.append(table);
    details.append(
      scroll,
      el(
        'p',
        `Score is the sum of unrounded term contributions. k1 = ${data.k1}; b = ${data.b}. Repeated query terms count once in the score. Scores are not probabilities.`,
      ),
    );
    article.append(details);
    $('results').append(article);
  }
}
async function search() {
  invalidateSearch('Searching…');
  const ticket = searchRevision;
  searchController = new AbortController();
  try {
    const data = await api(
      '/api/search',
      {
        query: $('query').value,
        mode: $('mode').value,
        k1: Number($('k1').value),
        b: Number($('b').value),
        limit: Number($('limit').value),
      },
      searchController.signal,
    );
    if (ticket === searchRevision) showResults(data);
  } catch (error) {
    if (ticket === searchRevision && error.name !== 'AbortError') setStatus(error.message);
  } finally {
    if (ticket === searchRevision) searchController = null;
  }
}
$('search-form').onsubmit = (event) => {
  event.preventDefault();
  search();
};
$('search-form').addEventListener('input', () => {
  for (const id of ['k1', 'b']) document.querySelector(`output[for="${id}"]`).value = $(id).value;
  invalidateSearch();
});
$('seed').onclick = async () => {
  $('seed').disabled = true;
  try {
    await api('/api/import', await api('/api/samples'));
    await afterMutation();
  } catch (error) {
    setStatus(error.message);
  } finally {
    $('seed').disabled = false;
  }
};
function download(data, name) {
  const url = URL.createObjectURL(
      new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }),
    ),
    link = el('a');
  link.href = url;
  link.download = name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}
$('export').onclick = async () => {
  try {
    download(await api('/api/export'), 'index-press-library.json');
  } catch (error) {
    setStatus(error.message);
  }
};
$('import').onchange = async (event) => {
  const file = event.target.files[0];
  event.target.value = '';
  if (!file) return;
  $('import').disabled = true;
  try {
    if (file.size > 8500000) throw Error('Library file exceeds 8.5 MB.');
    const data = JSON.parse(await file.text());
    await api('/api/import', data);
    await afterMutation();
  } catch (error) {
    setStatus(`Import failed: ${error.message}`);
  } finally {
    $('import').disabled = false;
  }
};
try {
  await refresh();
  setStatus('Ready. Search your collection or add a document.');
} catch (error) {
  setStatus(`Library unavailable: ${error.message}`);
}
