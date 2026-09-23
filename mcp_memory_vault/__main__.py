"""``python -m mcp_memory_vault``: same as the ``mcp-memory-vault`` command."""

import sys

from .cli import main

sys.exit(main())
