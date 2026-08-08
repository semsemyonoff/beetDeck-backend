"""Entry point: `python -m beetdeck_mcp`.

The container runs this with no reloader — editing `beetdeck_mcp/` requires a
container restart, unlike the Flask app next to it.
"""

from __future__ import annotations

import logging
import sys

from beetdeck_mcp import server as server_module
from beetdeck_mcp.config import ConfigError, load_config


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"beetdeck_mcp: {exc}", file=sys.stderr)
        return 2

    logging.getLogger(__name__).info(
        "serving MCP on %s:%s (api=%s, readonly=%s)",
        config.host,
        config.port,
        config.api_url,
        config.readonly,
    )
    server_module.run(config)
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
