"""One router module per route issue, mounted by `gct.api.app.create_app` (issue #104).

Each owns its own request/response models and its own status mapping, and edits only its own
module - which is what keeps the route issues' footprints disjoint. `classes` (#107) is still an
EMPTY stub; `files` (#110) and `ask` (#108) are filled in. `health` is the skeleton's own route.
"""
