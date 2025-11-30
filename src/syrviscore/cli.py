"""SyrvisCore CLI - Main entry point."""

import click
from syrviscore.__version__ import __version__


@click.group()
@click.version_option(version=__version__, prog_name="syrvis")
def cli():
    """SyrvisCore - Self-hosted infrastructure platform for Synology NAS."""
    pass


@cli.command()
def hello():
    """Hello World - Test command to verify installation."""
    click.echo("🎉 Hello from SyrvisCore!")
    click.echo(f"Version: {__version__}")
    click.echo("✓ CLI is working correctly")


if __name__ == "__main__":
    cli()
