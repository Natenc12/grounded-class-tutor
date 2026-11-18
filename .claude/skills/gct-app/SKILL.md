---
name: gct-app
description: Find your way around the Grounded Class Tutor product - what each feature does, how to reach it, whether reaching it costs money, and where its code and tests start. Use when a request or bug report names a behaviour and you need to locate it, or before running any part of the app.
---

Read `feature-map.md` in this directory.

It is a map of the product from the outside, not of the code. Every entry's `Reach`, `Costs money`,
`Code` and `Tests` lines are checked against the real app by `tests/test_feature_map.py`, so trust
them. If you add, remove or rename a feature, that test fails until the map agrees.

Exact request and response fields are in `web/openapi.json`, which is pinned to the live API by its
own test. The map does not repeat them.
