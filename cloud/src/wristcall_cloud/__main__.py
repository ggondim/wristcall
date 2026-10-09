import os
import sys

import uvicorn

from .app import create_app
from .config import ConfigError, config_from_env


def main() -> None:
    try:
        config = config_from_env(os.environ)
    except ConfigError as e:
        print(f"wristcall-cloud: {e}", file=sys.stderr)
        raise SystemExit(2) from None
    uvicorn.run(create_app(config), host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))


if __name__ == "__main__":
    main()
