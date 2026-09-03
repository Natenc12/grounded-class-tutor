"""One router module per route issue, mounted by `gct.api.app.create_app` (issue #104).

`classes` (#107), `files` (#110) and `ask` (#108) are EMPTY stubs here on purpose: each owns its
own request/response models and its own status mapping, and each edits only its own module.
`health` is the skeleton's own route.
"""
