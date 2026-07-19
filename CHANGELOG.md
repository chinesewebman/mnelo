# Changelog

## v0.3.9 — 2026-07-19

test(locale): add 24 tests for mnelo_locale (0% → 100% coverage)

- Covers previously-untested locale module: get_locale() detection chain (HERMES_MEMORY_LANG > LC_ALL > LANG > system locale > en), _normalize() POSIX parsing (zh_CN/zh_TW/en_US/hyphen forms), current_locale() lazy caching + reload() refresh, t() message resolver with zh/en fallback + format kwargs.
- Edge cases: _syslocale.getlocale() exception path, format positional IndexError.
- Total: 171 → 195 passed (1 skipped).

## v0.3.8 — 2026-07-19

fix(tests): rebind ValidationError via `gc.get_objects()` scan for orphan module dicts

- Round 4 cross-test pollution completion. Earlier `_force_repo_validation` + `pytest_collection_finish` only rebinded test module attrs and `sys.modules['validation']`, but multiple `_load_from_repo` calls left ORPHANED module dicts held alive by function `__globals__` (e.g., `Memory._upsert_entity.__globals__` pointed to OLD memory module whose `__dict__['ValidationError']` was still OLD).
- New autouse fixture `_rebind_test_validation_error` walks `gc.get_objects()` to find all function objects with `__globals__['__name__'] in ('validation', 'memory')` and rebinds their `__dict__['ValidationError']` to `repo_ve`.
- Result: **171 passed, 1 skipped, 0 failed** (was 165/172 with 6 cross-test pollution failures).

## v0.3.7 — 2026-07-19

cleanup: drop 实战 pollution from tests + docs (188 occurrences)

## v0.3.6 — 2026-07-19

cleanup: drop 实战 pollution from production code (167 occurrences)

## v0.3.5 — 2026-07-19

refactor: remove redundant hermes_memory_client alias file

## v0.3.4 — 2026-07-19

refactor: rename hermes_memory identifiers to mnelo (logger / JOB_ID / filename)

## v0.3.3 — 2026-07-19

docs: rename plist Label to `ai.mnelo.mcp` + port via env var

## v0.3.2 — 2026-07-19

refactor(memory): remove implicit `sys.path.insert(0, /Users/apple/.hermes/memory)`

## v0.3.1 — 2026-07-18

test(coverage_gaps): SSE e2e (TestClient) + Round 2 extras (P0-2 SSE auth, etc.)

## v0.3.0 — 2026-07-18

feat(quality): 2-round quality audit + coverage upgrade (memory 89% / mcp_server 79% / entity_resolve 76%)

---

## Earlier versions

- **v0.2.2** — P0-2 SSE auth (Bearer token, 401 on missing)
- **v0.2.1** — security: 20 audit findings fixed
- **v0.1.1** — embedder: configurable model + multilingual support
- **v0.1** — initial release (2026-07-17)
