from __future__ import annotations

import argparse
import sys

import pydantic
import yaml

from .models import ScriptConfig
from .runner import ScriptRunner


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
        config = ScriptConfig(**config_dict)
    except pydantic.ValidationError as e:
        print("Validation errors:", file=sys.stderr)
        print(e, file=sys.stderr)
        sys.exit(1)

    cli_args = {}
    for item in args.arg:
        if "=" not in item:
            parser.error(f"--arg requires KEY=VALUE format, got: {item}")
        key, value = item.split("=", 1)
        cli_args[key] = value

    runner = ScriptRunner(config, cli_args)
    runner.run()


if __name__ == "__main__":
    main()
