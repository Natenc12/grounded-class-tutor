// The Python peer owns grounding decisions. This boundary checks their wire
// shape and state coherence before any result enters application state. It does
// not independently decide whether a cited claim is supported by the source.
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
export function characterCount(value) {
  let count = 0;
  for (const _character of value) count++;
  return count;
}
// Python bounds Unicode code points, while JS length counts UTF-16 units. Check
// a cheap raw ceiling before counting, without allocating an expanded array.
const text = (value, max = 80000) => typeof value === 'string' && value.length <= 2 * max &&
  (value.length <= max || characterCount(value) <= max);
const strings = value => Array.isArray(value) && value.length <= 2000 && value.every(item => text(item));
const keys = (value, allowed) => object(value) && Object.keys(value).every(key => allowed.includes(key));
const filename = value => text(value, 1024) && value.length > 0 && !/[\x00/\\]/.test(value);
const identifier = value => typeof value === 'string' && /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test(value);
const timestamp = value => text(value, 64) && Number.isFinite(Date.parse(value));
const validClass = value => keys(value, ['id', 'name', 'created_at']) && identifier(value.id) &&
  text(value.name, 200) && value.name.trim().length > 0 && timestamp(value.created_at);
const validStoredDocument = value => keys(value, ['id', 'class_id', 'filename', 'sha256', 'byte_count', 'page_count', 'status', 'created_at']) &&
  identifier(value.id) && identifier(value.class_id) && filename(value.filename) &&
  typeof value.sha256 === 'string' && /^[a-f0-9]{64}$/.test(value.sha256) &&
  Number.isInteger(value.byte_count) && value.byte_count > 0 && value.byte_count <= 10 * 1024 * 1024 &&
  Number.isInteger(value.page_count) && value.page_count > 0 && value.page_count <= 500 &&
  value.status === 'ready' && timestamp(value.created_at);

export function validSearchStatus(value) {
  return keys(value, ['state', 'mode', 'message']) && Object.keys(value).length === 3 &&
    ['unavailable', 'missing', 'stale', 'ready', 'limited', 'failed'].includes(value.state) &&
    value.mode === (value.state === 'ready' ? 'hybrid' : 'lexical') &&
    text(value.message, 500) && value.message.trim().length > 0;
}

export function validLibraryEvent(event, request) {
  if (!keys(event, ['type', 'operation', 'value']) || event.type !== 'library' || event.operation !== request.operation) return false;
  const value = event.value;
  switch (request.operation) {
    case 'search_status': case 'prepare_search': return validSearchStatus(value);
    case 'list_classes': return Array.isArray(value) && value.length <= 10000 && value.every(validClass) && new Set(value.map(row => row.id)).size === value.length;
    case 'create_class': return validClass(value);
    case 'list_documents': return Array.isArray(value) && value.length <= 10000 &&
      value.every(row => validStoredDocument(row) && row.class_id === request.class_id) && new Set(value.map(row => row.id)).size === value.length;
    case 'import_document': return validStoredDocument(value) && value.class_id === request.class_id;
    case 'get_document': return keys(value, ['document', 'preview']) && validStoredDocument(value.document) &&
      value.document.id === request.document_id && value.document.class_id === request.class_id &&
      validDocument(value.preview) && !value.preview.synthetic &&
      value.preview.filename === value.document.filename && value.preview.page_count === value.document.page_count;
    case 'citation': return keys(value, ['document_id', 'filename', 'page_or_slide', 'text']) &&
      identifier(value.document_id) && filename(value.filename) && text(value.text, 30000) &&
      Number.isInteger(value.page_or_slide) && value.page_or_slide > 0 && value.page_or_slide <= 500;
    case 'delete_document': case 'delete_class': case 'backup': return value === null;
    default: return false;
  }
}

export function validDocument(value) {
  return keys(value, ['type', 'filename', 'page_count', 'pages', 'truncated', 'synthetic', 'suggested_question']) &&
    value.type === 'document' && filename(value.filename) &&
    Number.isInteger(value.page_count) && value.page_count >= 0 && value.page_count <= 500 &&
    typeof value.truncated === 'boolean' && typeof value.synthetic === 'boolean' &&
    (value.suggested_question === undefined || text(value.suggested_question, 4000)) &&
    Array.isArray(value.pages) && value.pages.length === value.page_count &&
    value.pages.every((page, index) => keys(page, ['page_or_slide', 'text', 'truncated']) &&
      page.page_or_slide === index + 1 && text(page.text) && typeof page.truncated === 'boolean') &&
    value.pages.reduce((total, page) => total + characterCount(page.text), 0) <= 80000;
}

export function validResult(value) {
  return keys(value, ['state', 'answer_prose', 'citations', 'coverage', 'integrity', 'error']) &&
    ['GROUNDED', 'PARTIAL', 'REFUSAL', 'INTEGRITY_FLAGGED', 'ERROR'].includes(value.state) &&
    (value.answer_prose === null || text(value.answer_prose)) &&
    Array.isArray(value.citations) && value.citations.length <= 2000 &&
    value.citations.every(citation => keys(citation, ['label', 'file', 'page_or_slide', 'chunk_id']) &&
      text(citation.label, 20) && /^S[1-9][0-9]*$/.test(citation.label) && filename(citation.file) &&
      Number.isInteger(citation.page_or_slide) && citation.page_or_slide > 0 && citation.page_or_slide <= 500 &&
      text(citation.chunk_id, 64) && /^[a-f0-9]{64}$/.test(citation.chunk_id)) &&
    keys(value.coverage, ['complete', 'gaps']) && typeof value.coverage.complete === 'boolean' && strings(value.coverage.gaps) &&
    keys(value.integrity, ['ok', 'reasons']) && typeof value.integrity.ok === 'boolean' && strings(value.integrity.reasons) &&
    (value.error === null || (keys(value.error, ['kind', 'message']) &&
      ['provider_transient', 'provider_terminal'].includes(value.error.kind) && text(value.error.message))) &&
    coherentResult(value);
}

function coherentResult(value) {
  const { state, answer_prose: prose, citations, coverage, integrity, error } = value;
  if (error !== null) return state === 'ERROR' && prose === null && citations.length === 0 &&
    !coverage.complete && coverage.gaps.length === 0 && integrity.ok && integrity.reasons.length === 0;
  if (state === 'ERROR') return false;
  if (state === 'INTEGRITY_FLAGGED') return !integrity.ok && integrity.reasons.length > 0;
  if (!integrity.ok || integrity.reasons.length) return false;
  // Grounder deliberately preserves even an empty 'COVERAGE: complete' refusal.
  if (state === 'REFUSAL') return prose === null && citations.length === 0;
  return typeof prose === 'string' && prose.trim().length > 0 && citations.length > 0 &&
    (state === 'GROUNDED' ? coverage.complete && coverage.gaps.length === 0 :
      !coverage.complete && coverage.gaps.length > 0);
}
