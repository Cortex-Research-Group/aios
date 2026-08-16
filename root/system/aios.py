#!/usr/bin/env python3
"""aiOS entrypoint.

    aios            the interactive shell
    aios schedd     the scheduling daemon, in the foreground

schedd is a separate process on purpose: the shell is a TTY session that ends
when you close the terminal, and a schedule that only runs while someone is
looking is not a schedule. It reads its secrets from its own environment, the
same way a hosted deployment does -- the shell passes the unsealed vault in when
it starts one, and nothing is written to disk.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from kernel import schedule  # noqa: E402
from shell.tui import main  # noqa: E402

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "schedd":
        sys.exit(schedule.serve())
    sys.exit(main())
