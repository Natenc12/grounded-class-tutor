"""After experiment (a): try to rebuild the ADR 0025 'SAVEPOINT publishes nothing' trap."""
import uuid, psycopg
from gct.db import connect
from gct.classes import create_class
from gct.config import load_settings

def visible(owner):
    with connect() as c:
        return c.execute("select count(*) from classes where owner_id=%s", (owner,)).fetchone()[0]

def scenario(name, make, prelude):
    owner = f"trap-{uuid.uuid4()}"
    conn = make()
    try:
        prelude(conn)
        cid = create_class(conn, owner_id=owner, name="x")   # returns successfully
        status = conn.info.transaction_status.name
    finally:
        conn.close()   # no commit, like a script that just exits
    n = visible(owner)
    print(f"{name:55s} returned ok, status after={status:8s} published rows={n}  -> {'OK' if n==1 else 'TRAP (silent loss)'}")
    with connect() as c: c.execute("delete from classes where owner_id=%s", (owner,))

def nested(conn):
    pass
raw = lambda: psycopg.connect(load_settings().database_url)
def nonauto():
    c = connect(); c.autocommit = False; return c
scenario("A connect(); bare SELECT first", connect, lambda c: c.execute("select 1").fetchone())
scenario("B connect(); raw execute('begin') first", connect, lambda c: c.execute("begin"))
scenario("C connect(); caller sets autocommit=False; SELECT", nonauto, lambda c: c.execute("select 1").fetchone())
scenario("E raw psycopg.connect() (bypasses connect()); SELECT", raw, lambda c: c.execute("select 1").fetchone())
# D: writer called inside a caller's own transaction block, on the default connection
owner = f"trap-{uuid.uuid4()}"; conn = connect()
with conn.transaction():
    conn.execute("select 1"); create_class(conn, owner_id=owner, name="x")
conn.close(); n = visible(owner)
print(f"{'D connect(); writer nested in caller transaction()':55s} published rows={n}  -> {'OK' if n==1 else 'TRAP'}")
with connect() as c: c.execute("delete from classes where owner_id=%s", (owner,))
# F: failed statement no longer poisons the connection
conn = connect()
try: conn.execute("select 'urn:uuid:nope'::uuid")
except psycopg.Error as e: print("F bad cast raised", type(e).__name__, "; status now", conn.info.transaction_status.name, "; next statement:", conn.execute("select 1").fetchone())
conn.close()
