import os
import sys

import uvicorn

from .app import create_app
from .config import ConfigError, config_from_env


def _port(raw: str) -> int:
    if not (raw.isascii() and raw.isdigit()) or not 1 <= int(raw) <= 65535:
        raise ConfigError("PORT must be a TCP port number")
    return int(raw)


def main() -> None:
    try:
        config = config_from_env(os.environ)
        port = _port(os.environ.get("PORT", "").strip() or "8080")
    except ConfigError as e:
        print(f"wristcall-cloud: {e}", file=sys.stderr)
        raise SystemExit(2) from None
    uvicorn.run(create_app(config), host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
