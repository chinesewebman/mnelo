#!/usr/bin/env python3
"""task_states.py — facade re-exporting all split modules for backward compat.

[refactor 2026-08-12] 原 1830 行 monolithic 拆为 4 个子模块 (跟 memory.py 9b809d8
拆分同模式, 区别: task_states 是 module-level functions, 不需要 MRO class
composition):

  task_states_core.py    异常族 + helpers + Task CRUD + transition (largest)
  task_states_loop.py    Loop CRUD + loop_tick
  task_states_stale.py  Stale proposal pipeline (propose/apply/list)
  task_states_digest.py digest block rendering (list_active_tasks_and_loops +
                         render_digest_block4)

向后兼容: 所有 import path 不变
  - `import task_states` → 全局 namespace 仍含全部 names
  - `from task_states import X` → X 从 facade re-export
  - `task_states.X` → facade attribute (含 28+ tests + mcp_server + digest_mixin)
"""

from typing import List  # noqa: F401  re-export (test_review_fixes.py::test_rf7_list_typing_imported contract: task_states namespace 含 typing.List)

from task_states_core import (  # noqa: F401  re-export
    # 常量
    ALL_STATES,  # noqa: F401  re-export
    LOOP_STATES,  # noqa: F401  re-export
    TASK_STATES,  # noqa: F401  re-export
    EvidenceNotFoundError,
    InvalidTransitionError,
    LoopNotFoundError,
    NotCurrentStateError,
    ReasonRequiredError,
    # 异常族
    TaskLoopError,
    TaskNotFoundError,
    TerminalLoopError,
    # helpers
    _default_now,  # noqa: F401  re-export
    _slugify,  # noqa: F401  re-export
    forget_task,
    list_tasks,
    replay_task,
    task_create,
    # Task CRUD
    transition,
)
from task_states_digest import (  # noqa: F401  re-export
    list_active_tasks_and_loops,
    render_digest_block4,
)
from task_states_loop import (  # noqa: F401  re-export
    forget_loop,
    list_loops,
    loop_create,
    loop_tick,
    loop_update,
)
from task_states_stale import (  # noqa: F401  re-export
    apply_stale_proposal,
    list_stale_proposals,
    propose_stale_tasks,
)

__all__ = [
    # 异常族
    "TaskLoopError",
    "TaskNotFoundError",
    "InvalidTransitionError",
    "NotCurrentStateError",
    "EvidenceNotFoundError",
    "ReasonRequiredError",
    "TerminalLoopError",
    "LoopNotFoundError",
    # 常量
    "ALL_STATES",
    "TASK_STATES",
    "LOOP_STATES",
    # Task CRUD
    "transition",
    "list_tasks",
    "replay_task",
    "task_create",
    "forget_task",
    # Loop CRUD
    "loop_tick",
    "loop_create",
    "loop_update",
    "list_loops",
    "forget_loop",
    # Stale pipeline
    "propose_stale_tasks",
    "apply_stale_proposal",
    "list_stale_proposals",
    # Digest
    "list_active_tasks_and_loops",
    "render_digest_block4",
]
