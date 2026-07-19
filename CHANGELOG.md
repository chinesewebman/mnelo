# Changelog

## v0.4.4 — 2026-07-19

test(memory): push memory.py coverage 92% → 93% (+10 branch tests)

- **memory.py** (92% → 93%): +10 tests for previously-uncovered branches:
  - `forget(entity)` path (line 381)
  - `_vector_recall_with_conn` exception path (lines 574-576, closed connection)
  - `_entity_recall_with_conn` skip empty name+summary (line 635)
  - Alias match boosts importance by +0.2 (line 648)
  - `_graph_recall` seed_entities / seed_chunks expansion (lines 669, 687)
  - `_graph_recall` empty new_chunks returns `[]` (line 692)
  - `graph_entity` hit for `identity_fact` / `canonical_fact` (line 706)
  - Chinese bigram tokenization (line 799, query "中文")
  - Single ASCII char token (line 799, query "a")
  - `_entity_recall` empty hits returns `[]` (line 807)
  - `_entity_recall` `seen_ids` dedup (line 833)
- Total: 291 → 301 passed (1 skipped, +10 tests).

## v0.4.3 — 2026-07-19

fix(validation): accept int IDs in validate_id + subprocess smoke tests

- **`validate_id`** now accepts `int` (relation_id from `Memory.relate()`) and coerces to `str`. Rejects `bool` explicitly (since `bool` is subclass of `int`). Unblocks `Memory.forget(rid_int)` where rid is the int returned by `relate()`.
- **+9 subprocess smoke tests** verify that `memory.py` / `entity_resolve.py` / `embedder.py` `__main__` blocks run end-to-end. These don't add line coverage (subprocess has its own coverage tracker), but they catch integration regressions in demo scripts.
- Test `test_forget_relation` updated: previously asserted `validate_id` rejects int (the broken behavior); now asserts `forget(rid_int)` succeeds.
- Total: 282 → 291 passed (1 skipped, +9 tests).

## v0.4.2 — 2026-07-19

test: push auth 92→100%, config 80→92%, validation 95→99% (+30 tests)

- **auth.py** (92% → 100%): +3 tests for `AUTH_TOKEN_FILE` with content / empty / nonexistent paths.
- **config.py** (80% → 92%): +10 tests for `tomllib` fallback, `_load_config_file` bad TOML / missing file, `_resolve_tz` (`None`/local/utc/IANA/whitespace), `describe()` method, `config_path` property.
- **validation.py** (95% → 99%): +17 tests for `validate_chunk_content` (non-str / empty-after-sanitize / with newlines), `validate_query` (non-str / empty-after-sanitize / newline stripping), `validate_holding_payload` (non-dict / NaN / +inf / -inf / string / zero / valid).
- Total: 252 → 282 passed (1 skipped, 30 new tests).

## v0.4.1 — 2026-07-19

test: push coverage 88% → 89% via 44 new tests across 4 modules

- **mnelo_locale.py** (0% fragmented → 100%): replaced `importlib.reload()` with cache reset (avoids coverage fragmentation).
- **entity_resolve.py** (76% → 84% LIVE): +25 tests for `normalize_text`, `alias_match_score`, `get_aliases` bad-JSON path, `find_duplicate_candidates` empty-name/alias-conflict branches, `merge_entities` same-id/missing-id, `find_duplicates_report` empty/with-candidates.
- **memory.py** (89% → 90%): +13 tests for `now()` tz fallback, warm-up disabled config path, recall strategy branches (`graph_only`/`meta_only`/`entity_only`/unknown), `_vector_recall` exception handling, `forget` unknown kind, `_entity_recall` empty content, `_graph_recall` empty seeds, `_meta_recall` with source filter.
- **embedder.py** (83% → 85%): +6 tests for `embed_batch`, `get_embedder` singleton, `EMBED_DIM` constant.
- Total: 208 → 252 passed (1 skipped, 44 new tests).

## v0.4.0 — 2026-07-19

test(mcp_server): add 13 tests for uncovered dispatcher + SSE paths

- Targets previously-uncovered lines in mcp_server.py: `_handle_simple` id_field wrap path (remember/relate/update), `_call_tool` unknown tool name branch, `run_sse` AuthError propagation + uvicorn.run dispatch, `_build_sse_app` + `BearerAuthMiddleware` wiring.
- Plus integration tests: ImportError fallback when MCP libs missing, `_resolve_server_defaults` config-fallback path, `_build_sse_app` /sse + /messages/ route registration.
- Total: 195 → 208 passed (1 skipped).

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
