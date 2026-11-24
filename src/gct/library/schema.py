from __future__ import annotations

APPLICATION_ID = 0x4743544C
VERSION = 2

STATEMENTS = (
    """
    CREATE TABLE classes (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL UNIQUE CHECK (name <> '' AND name = trim(name))
    ) STRICT
    """,
    """
    CREATE TABLE documents (
        id TEXT PRIMARY KEY,
        class_id TEXT NOT NULL REFERENCES classes (id) ON DELETE CASCADE,
        filename TEXT NOT NULL CHECK (trim(filename) <> ''),
        sha256 TEXT NOT NULL CHECK (length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
        page_count INTEGER NOT NULL CHECK (page_count >= 1),
        original BLOB NOT NULL CHECK (length(original) > 0),
        UNIQUE (class_id, sha256),
        UNIQUE (id, page_count),
        FOREIGN KEY (id, page_count) REFERENCES pages (document_id, page_or_slide)
            DEFERRABLE INITIALLY DEFERRED
    ) STRICT
    """,
    """
    CREATE TABLE pages (
        document_id TEXT NOT NULL,
        page_count INTEGER NOT NULL,
        page_or_slide INTEGER NOT NULL,
        previous INTEGER AS (nullif(page_or_slide - 1, 0)),
        text TEXT NOT NULL,
        PRIMARY KEY (document_id, page_or_slide),
        CHECK (page_or_slide BETWEEN 1 AND page_count),
        FOREIGN KEY (document_id, page_count) REFERENCES documents (id, page_count)
            ON DELETE CASCADE,
        FOREIGN KEY (document_id, previous) REFERENCES pages (document_id, page_or_slide)
    ) STRICT, WITHOUT ROWID
    """,
    """
    CREATE TABLE passages (
        id INTEGER PRIMARY KEY,
        stable_id TEXT NOT NULL UNIQUE CHECK (trim(stable_id) <> ''),
        document_id TEXT NOT NULL,
        page_or_slide INTEGER NOT NULL,
        ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
        text TEXT NOT NULL CHECK (trim(text) <> ''),
        search_text TEXT NOT NULL,
        UNIQUE (document_id, page_or_slide, ordinal),
        FOREIGN KEY (document_id, page_or_slide) REFERENCES pages (document_id, page_or_slide)
            ON DELETE CASCADE
    ) STRICT
    """,
    """
    CREATE VIRTUAL TABLE passages_fts USING fts5 (
        search_text,
        content = 'passages',
        content_rowid = 'id',
        tokenize = 'porter unicode61'
    )
    """,
    """
    CREATE TRIGGER passages_fts_insert AFTER INSERT ON passages BEGIN
        INSERT INTO passages_fts (rowid, search_text) VALUES (new.id, new.search_text);
    END
    """,
    """
    CREATE TRIGGER passages_fts_delete AFTER DELETE ON passages BEGIN
        INSERT INTO passages_fts (passages_fts, rowid, search_text)
            VALUES ('delete', old.id, old.search_text);
    END
    """,
    """
    CREATE TRIGGER passages_fts_update AFTER UPDATE ON passages BEGIN
        INSERT INTO passages_fts (passages_fts, rowid, search_text)
            VALUES ('delete', old.id, old.search_text);
        INSERT INTO passages_fts (rowid, search_text) VALUES (new.id, new.search_text);
    END
    """,
)
