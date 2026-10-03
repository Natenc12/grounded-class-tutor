// The Python peer owns grounding decisions. This boundary checks their wire
// shape and state coherence before any result enters application state. It does
// not independently decide whether a cited claim is supported by the source.
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const text = (value, max = 80000) => typeof value === 'string' && value.length <= max;
const strings = value => Array.isArray(value) && value.length <= 2000 && value.every(item => text(item));
const keys = (value, allowed) => object(value) && Object.keys(value).every(key => allowed.includes(key));
const filename = value => text(value, 1024) && value.length > 0 && !/[\x00/\\]/.test(value);

export function validDocument(value) {
  return keys(value, ['type', 'filename', 'page_count', 'pages', 'truncated', 'synthetic', 'suggested_question']) &&
    value.type === 'document' && filename(value.filename) &&
    Number.isInteger(value.page_count) && value.page_count >= 0 && value.page_count <= 500 &&
    typeof value.truncated === 'boolean' && typeof value.synthetic === 'boolean' &&
    (value.suggested_question === undefined || text(value.suggested_question, 4000)) &&
    Array.isArray(value.pages) && value.pages.length === value.page_count &&
    value.pages.every((page, index) => keys(page, ['page_or_slide', 'text', 'truncated']) &&
      page.page_or_slide === index + 1 && text(page.text) && typeof page.truncated === 'boolean') &&
    value.pages.reduce((total, page) => total + page.text.length, 0) <= 80000;
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
