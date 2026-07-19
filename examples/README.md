# mnelo Examples

Runnable walkthroughs of the mnelo memory API. Each example is a self-contained
Python script you can read top-to-bottom, then run with `python examples/0X_*.py`.

## Order to read

| # | File | What it teaches | Time |
|---|------|-----------------|------|
| 1 | [`01_basic_remember_recall.py`](01_basic_remember_recall.py) | `Memory.remember()` + `Memory.recall()` — write & read | 2 min |
| 2 | [`02_entities_and_relations.py`](02_entities_and_relations.py) | `entities=[]` parameter + `Memory.relate()` — knowledge graph | 3 min |
| 3 | [`03_4_lane_recall.py`](03_4_lane_recall.py) | vector / graph / meta / entity lanes + RRF fusion | 4 min |
| 4 | [`04_update_and_forget.py`](04_update_and_forget.py) | `update()` + `forget()` + write-time vector cleanup | 3 min |
| 5 | [`05_identity_facts.py`](05_identity_facts.py) | `identity_fact_manager.py` CLI walkthrough | 3 min |

## Running

Each script:
- Uses LIVE mnelo DB (`~/.hermes/memory/memory.db` by default).
- Tags all writes with `source='example_NN:<purpose>'` so cleanup is easy.
- Cleans up its own data on exit (even on Ctrl-C).
- Prints expected output for each step so you know it worked.

```bash
cd /Users/apple/projects/mnelo   # or wherever you cloned mnelo
python examples/01_basic_remember_recall.py
python examples/02_entities_and_relations.py
python examples/03_4_lane_recall.py
python examples/04_update_and_forget.py
python examples/05_identity_facts.py
```

## If something's broken

All examples clean up after themselves. If a script crashes mid-way:

```python
import sys
sys.path.insert(0, '/Users/apple/projects/mnelo')
from memory import Memory
m = Memory()
m._conn.execute("DELETE FROM chunks WHERE source LIKE 'example_%'")
m._conn.execute("DELETE FROM entities WHERE source LIKE 'example_%'")
m._conn.commit()
m.close()
```

## See also

- [`docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md) — system design
- [`docs/SCHEMA.md`](../docs/SCHEMA.md) — table structure
- [`docs/RUNBOOK.md`](../docs/RUNBOOK.md) — operational playbook
- [Main README](../README.md) — installation + feature overview