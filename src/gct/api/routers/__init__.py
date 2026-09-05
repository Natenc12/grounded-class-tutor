"""One router module per route issue, mounted by `gct.api.app.create_app` (issue #104).

Each owns its own request/response models and its own status mapping, and edits only its own
module - which is what keeps the route issues' footprints disjoint. `health` is the skeleton's
own route; every other module names the issue that owns it in its own docstring.

This docstring deliberately does NOT list which routers are filled and which are still stubs.
That sentence has to be rewritten by every route ticket and has now been missed by three in a
row - it was already wrong about `files` before #108 touched it, and #107 and #108 each landed
without correcting the other's half. `create_app` is the writer of what is mounted; read it.
"""
