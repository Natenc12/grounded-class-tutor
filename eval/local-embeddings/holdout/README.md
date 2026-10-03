# Originally unseen local embedding holdout

This original synthetic fixture was authored by the independent governance agent
on October 3, 2026, before seeing either model's retrieval results. The lead froze
one candidate and configuration using the existing development suites before the
fixture was exposed. It was then used once for confirmation without tuning.
**It is now public regression material, not a continuing unseen holdout.** Future
model or ranking selection needs separately authored unseen cases.

The canonical source is `../../local-quality/embedding-holdout.json`, using the
existing local quality manifest format. `foreign-classes.json` supplies separate
class controls. `freeze-manifest.json` records their exact original SHA-256 values
and the identifier of the historical local freeze. All content is fictional and
newly authored; none comes from private course files or account data.

## What is measured

There are 34 questions: 28 answerable and six unsupported, with 34 required evidence
groups across seven documents and 34 physical pages. The eight paraphrases include
four with zero unstemmed content-word overlap between the question and its relevant
page body. That label excludes filenames and does not assert zero Porter-stem
overlap. Other cases cover exact terms and quantities, arithmetic, similar but
incorrect passages, attribution, version changes and six multi-source questions.
Tags overlap and must not be summed as disjoint counts.

Each source page fits in one production chunk. Every generated page was checked
against the authored source after whitespace normalization. Explicit line wrapping
keeps clock times intact through the PDF renderer. Scoring requires all groups for
a case; it cannot credit one relevant source when another required source is missing.
Several cases share source facts, so these are not 34 independent statistical samples.

The primary denominator is 28 answerable cases. Unsupported questions remain outside
recall denominators. Retrieving candidates for them is neither a correct answer nor
an incorrect refusal; no answer generation took place in this experiment.

## Cross-class controls

The control file has three foreign classes, each containing one document and one
page. Their filenames intentionally overlap filenames in the selected class.
Three unsupported questions have concrete answers exclusively in these other classes.
Import each control as its own class; never merge documents or scores by filename.
A separate empty class tests an empty admitted search scope.

Evaluate every question in the selected class before and after foreign imports,
then every question against the empty class. Reject any foreign result or any result
from the empty class. The experimental adapter preserves the primary class's vector
batch boundaries when adding other classes, isolating scope from quantization-related
padding changes. The existing FTS index has global BM25 statistics, so ranking changes
without foreign results are reported separately from leaks.

## Frozen confirmation rule

The selected candidate had to retain or improve all-required-groups recall at eight
passages, improve the paraphrase stratum by at least one case, and lose no previously
successful exact-detail or multi-source case. Source identity, complete source text,
class scope, the eight-passage limit and the 40,000-character labeled context bound
were independently checked. Failed cases and unsuccessful alternatives remain in
the evidence; the final report must not turn retrieval recall into answer accuracy.

The plain FTS fixture can be reproduced offline with the repository's existing dev
dependencies:

```sh
uv run python -m gct.eval.local_quality --suite eval/local-quality/embedding-holdout.json
```

For the full embedding and class-control experiment, use the local embedding study
runner documented in the parent directory. Only local computation and free public
model downloads are allowed. No API, ChatGPT generation, credits or cloud inference
are needed, and downloaded models remain outside Git.
