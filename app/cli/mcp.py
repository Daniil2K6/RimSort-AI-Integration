"""MCP server subcommand for the RimSort CLI."""

import click

from app.mcp.server import run_stdio


@click.command()
@click.option(
    "--log-level",
    default="INFO",
    show_default=True,
    help="Stderr log level (stdout carries the MCP protocol).",
)
def mcp(log_level: str) -> None:
    """Run the local RimSort MCP (Model Context Protocol) server over stdio.

    Exposes RimWorld mod-list management tools for AI assistants:
    inspect instances, list/detail mods, build and validate load orders,
    sort, save/load modpacks and launch the game.

    \b
    Examples:
        rimsort mcp
        rimsort mcp --log-level DEBUG
    """
    run_stdio(log_level)
