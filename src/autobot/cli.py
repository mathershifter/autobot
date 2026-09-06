from __future__ import annotations

import argparse
import sys

import pydantic
import yaml
from rich.console import Console

from .models import Config
from .runner import Runner

console = Console(stderr=True)

def main():
    parser = argparse.ArgumentParser(description="Execute an autobot script.")
    parser.add_argument("script", help="Path to the YAML script file")
    parser.add_argument(
        "-a",
        "--arg",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Pass arguments to the script (e.g. --arg console_host=10.0.0.1)",
    )
    args = parser.parse_args()

    with open(args.script) as f:
        config_dict = yaml.safe_load(f)

    try:
        config = Config(**config_dict)
    except pydantic.ValidationError as e:
        console.print("Validation errors:")
        console.print(e.json(indent=2))

        sys.exit(1)

    cli_args = {}
    for item in args.arg:
        if "=" not in item:
            parser.error(f"--arg requires KEY=VALUE format, got: {item}")
        key, value = item.split("=", 1)
        cli_args[key] = value

    runner = Runner(config, cli_args)
    runner.run()


if __name__ == "__main__":
    main()
