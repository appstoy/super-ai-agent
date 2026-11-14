"""Allow the package module to use the CLI."""

from super_agent.cli import main

if __name__ == "__main__":
    raise SystemExit(main())

