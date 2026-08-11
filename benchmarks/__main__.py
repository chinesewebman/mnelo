"""`python -m benchmarks` — mnelo evaluation harness 入口.

子命令分发. 当前支持:

    python -m benchmarks latency [--chunks N] [--queries N] [--top-k K] [--json PATH]
    python -m benchmarks locomo  [--top-k K] [--json PATH]

无子命令/未知子命令 → 打印 usage, exit 2.

[8/11 P3-followup] 在 chinesewebman/mnelo#11 (P3-benchmarks) 的 `latency` 之上
加 `locomo` 子命令 — LoCoMo 风格召回质量 smoke (3 内置 scenario: 光伏装机 /
美联储利率 / 比亚迪销量). 完整 10-conversation LoCoMo dataset 接入留作后续
单独 PR.
"""

import sys

_SUBCOMMANDS = ("latency", "locomo")


def main(argv: list[str] | None = None) -> int:
    """Dispatch to the requested subcommand. Returns process exit code."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in _SUBCOMMANDS:
        print(
            "mnelo evaluation harness\n\n"
            "usage: python -m benchmarks <command> [options]\n\n"
            "commands:\n"
            "  latency   recall 延迟 benchmark (--chunks N --queries N --top-k K --json PATH)\n"
            "  locomo    LoCoMo 风格召回质量 smoke (--top-k K --json PATH)\n",
            file=sys.stderr,
        )
        return 2

    cmd = argv.pop(0)
    if cmd == "latency":
        from benchmarks.latency import main as latency_main

        return latency_main(argv)
    if cmd == "locomo":
        from benchmarks.locomo import main as locomo_main

        return locomo_main(argv)
    return 2


if __name__ == "__main__":
    sys.exit(main())
