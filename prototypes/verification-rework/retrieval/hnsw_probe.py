"""Free probe: does top-k stay exact when one small class sits in a table full of other owners' chunks?"""
import random, uuid, time
from gct.db import connect

conn = connect(); conn.autocommit = True
random.seed(7)
DIM = 1536
def vec():
    v = [random.gauss(0, 1) for _ in range(DIM)]
    n = sum(x * x for x in v) ** 0.5
    return [x / n for x in v]

def add(owner, n):
    class_id = str(uuid.uuid4()); file_id = str(uuid.uuid4())
    conn.execute("insert into classes (class_id, owner_id, name) values (%s::uuid,%s,%s)", (class_id, owner, "probe"))
    conn.execute("insert into files (file_id, owner_id, class_id, filename, status) values (%s::uuid,%s,%s::uuid,%s,'ready')", (file_id, owner, class_id, "f.pdf"))
    with conn.cursor() as cur:
        cur.executemany(
            "insert into chunks (file_id, owner_id, class_id, file, page_or_slide, text, embedding, embedding_model_id) "
            "values (%s::uuid,%s,%s::uuid,'f.pdf',%s,%s,%s::vector,'probe')",
            [(file_id, owner, class_id, str(i), f"t{i}", vec()) for i in range(n)])
    return class_id

t = time.time()
big = add("probe-other", 6000)
small = add("probe-me", 40)
conn.execute("analyze chunks")
print("seeded in", round(time.time() - t, 1), "s")
q = vec()
SQL = """select chunk_id, (embedding <=> %(q)s::vector) as distance from chunks
         where owner_id = %(o)s and class_id = %(c)s::uuid order by distance asc limit %(k)s"""
for label, owner, cls in [("small class (40 of 6040 rows)", "probe-me", small), ("big class (6000 of 6040 rows)", "probe-other", big)]:
    plan = conn.execute("explain " + SQL, {"q": q, "o": owner, "c": cls, "k": 5}).fetchall()
    rows = conn.execute(SQL, {"q": q, "o": owner, "c": cls, "k": 5}).fetchall()
    exact = conn.execute("set local enable_indexscan = off").statusmessage
    print(label, "-> rows:", len(rows), "| plan:", plan[0][0][:70], "/", plan[1][0].strip()[:80] if len(plan) > 1 else "")
# force the ANN index for the small class, which is what the planner may pick as the table grows
with conn.transaction():
    conn.execute("set local enable_seqscan = off"); conn.execute("set local enable_bitmapscan = off"); conn.execute("set local enable_sort = off")
    plan = conn.execute("explain " + SQL, {"q": q, "o": "probe-me", "c": small, "k": 5}).fetchall()
    rows = conn.execute(SQL, {"q": q, "o": "probe-me", "c": small, "k": 5}).fetchall()
    print("small class, ANN index forced -> rows:", len(rows), "| plan:", " / ".join(p[0].strip()[:60] for p in plan[:3]))
for owner in ("probe-other", "probe-me"):
    conn.execute("delete from chunks where owner_id=%s", (owner,)); conn.execute("delete from files where owner_id=%s", (owner,)); conn.execute("delete from classes where owner_id=%s", (owner,))
print("cleaned")
