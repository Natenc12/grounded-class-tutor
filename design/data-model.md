# Local library data model

ADR 0033 replaces the Postgres/vector schema as the target for the desktop product.
The old schema remains recoverable in Git; migration must not alter the old database.

| Entity | Identity and stored data | Required invariant |
| --- | --- | --- |
| Library schema | Explicit version in one SQLite database | Reject unknown newer versions; back up before future upgrades |
| Class | UUID, name, creation time | Every document/query is scoped to an existing class |
| Document | UUID, class UUID, original filename, SHA-256, bytes, page count, creation time | Original bytes and full parsed/indexed content publish together |
| Page | Document UUID and physical page/slide number, extracted text | Empty pages do not renumber later citations |
| Chunk | Stable digest ID, document/class identity, page number, text and ordinal | Never cross a physical page; filename alone is not identity |
| Full-text index | Rebuildable terms over chunks | Scope before ranking/limit; delete with the document |

Original document bytes are bounded to 10 MiB each and stored as BLOBs. Parse and
chunk before entering the publication transaction. A cancelled/crashed import
before commit leaves no visible ready document; SQLite commits the original,
metadata, pages and index atomically. Duplicate content within a class is idempotent.

Use parameterized SQL and safely constructed full-text terms. Read operations and
source opening require both class and document identities. Source export resolves
an owned original; never accept a renderer-supplied disk path. Deletion affects only
the owned library, never the user's original selected file.

A consistent SQLite backup contains the library and original documents. It excludes
the separate encrypted account store. Never silently reset a corrupt/unsupported
database or migrate the legacy Postgres corpus. Class and source durability must be
checked through a fresh connection/process, not only the writing transaction.

Library ownership is the local OS user, not an OAuth profile. Signing out does not
erase classes. A profile switch cancels in-flight generation and prevents stale
answers being published. Persisted answer/conversation history is a later feature.
