#!/usr/bin/env python3
"""Unified wrapper for trading scripts to eliminate structural boilerplate."""

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Callable, Any

from scripts.trading.env_loader import load_oanda_env

ROOT = Path(__file__).resolve().parent.parent.parent


class CLIRunner:
    """Wrapper that deduplicates argparse setup, Redis/env parsing, and error catching."""

    def __init__(self, description: str, require_oanda: bool = False):
        self.parser = argparse.ArgumentParser(description=description)
        self.logger = logging.getLogger(Path(sys.argv[0]).stem)
        self.require_oanda = require_oanda

    def add_redis_arg(self, default: str = "redis://localhost:6379/0"):
        """Add standard Valkey/Redis URL argument."""
        self.parser.add_argument(
            "--redis", default=os.getenv("VALKEY_URL", default), help="Valkey/Redis URL"
        )

    def add_arg(self, *args: Any, **kwargs: Any):
        """Pass-through to add an argument."""
        self.parser.add_argument(*args, **kwargs)

    def run(self, main_func: Callable[[argparse.Namespace], int]) -> None:
        """Executes the standard lifecycle: loads envs, parses args, sets up logging, runs main, handles exits."""
        if self.require_oanda or Path(ROOT / "OANDA.env").exists():
            load_oanda_env(ROOT, override=True)
            if self.require_oanda and not os.getenv("OANDA_API_KEY"):
                print(
                    "WARNING: OANDA_API_KEY is missing from environment and OANDA.env"
                )

        args = self.parser.parse_args()

        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
        )

        try:
            sys.exit(main_func(args))
        except KeyboardInterrupt:
            self.logger.info("Interrupted by user. Exiting.")
            sys.exit(130)
        except Exception as e:
            self.logger.error(f"Script failed: {e}", exc_info=True)
            sys.exit(1)
