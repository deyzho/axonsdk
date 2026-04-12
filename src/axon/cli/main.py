"""Axon CLI built with Typer."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(
    name="axon",
    help="Axon — Provider-agnostic edge compute SDK for AI workload routing",
    add_completion=False,
)
console = Console()


@app.command()
def init(
    provider: Optional[str] = typer.Option(None, "--provider", "-p", help="Provider name"),
) -> None:
    """Interactive setup: create axon.json and .env for your project."""
    from axon.config import generate_config, generate_env_template
    from axon.types import ProviderName

    cwd = Path.cwd()
    config_path = cwd / "axon.json"

    if config_path.exists():
        overwrite = typer.confirm("axon.json already exists. Overwrite?", default=False)
        if not overwrite:
            raise typer.Abort()

    if not provider:
        provider = typer.prompt(
            "Provider",
            default="ionet",
            show_default=True,
        )

    project_name = typer.prompt("Project name", default=cwd.name)

    config_json = generate_config(
        project_name=project_name,
        provider=provider,  # type: ignore[arg-type]
    )
    config_path.write_text(config_json)
    console.print(f"[green]✓[/green] Created axon.json")

    env_path = cwd / ".env"
    if not env_path.exists():
        env_content = generate_env_template(provider)  # type: ignore[arg-type]
        env_path.write_text(env_content)
        console.print(f"[green]✓[/green] Created .env")

    console.print("\n[bold]Next steps:[/bold]")
    console.print(f"  1. Run [cyan]axon auth {provider}[/cyan] to configure credentials")
    console.print(f"  2. Run [cyan]axon deploy[/cyan] to deploy your workload")


@app.command()
def deploy(
    cwd: Optional[str] = typer.Option(None, "--cwd", help="Project directory"),
) -> None:
    """Bundle and deploy your workload to the configured provider."""
    from axon.client import AxonClient
    from axon.config import load_config

    project_dir = Path(cwd) if cwd else Path.cwd()

    with console.status("Loading axon.json..."):
        try:
            config = load_config(project_dir)
        except Exception as exc:
            console.print(f"[red]Error:[/red] {exc}")
            raise typer.Exit(1)

    secret_key = os.environ.get("AXON_SECRET_KEY")
    if not secret_key:
        console.print("[red]Error:[/red] AXON_SECRET_KEY not set. Run `axon auth` first.")
        raise typer.Exit(1)

    console.print(f"Deploying to [cyan]{config.provider}[/cyan]...")
    # TODO: implement full deploy flow
    console.print("[yellow]Deploy not yet implemented for Python SDK[/yellow]")


@app.command()
def status(
    cwd: Optional[str] = typer.Option(None, "--cwd", help="Project directory"),
) -> None:
    """List active deployments."""
    from axon.client import AxonClient
    from axon.config import load_config

    project_dir = Path(cwd) if cwd else Path.cwd()

    try:
        config = load_config(project_dir)
    except Exception as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1)

    secret_key = os.environ.get("AXON_SECRET_KEY")
    if not secret_key:
        console.print("[red]Error:[/red] AXON_SECRET_KEY not set.")
        raise typer.Exit(1)

    async def _run() -> None:
        async with AxonClient(provider=config.provider, secret_key=secret_key) as client:
            deployments = await client.list_deployments()
            if not deployments:
                console.print("No active deployments found.")
                return

            table = Table(title=f"Deployments on {config.provider}")
            table.add_column("ID", style="cyan")
            table.add_column("Name")
            table.add_column("Status", style="green")
            table.add_column("Created")

            for d in deployments:
                table.add_row(d.id, d.name, d.status, str(d.created_at))

            console.print(table)

    asyncio.run(_run())


@app.command()
def send(
    processor_id: str = typer.Argument(..., help="Processor/deployment ID"),
    message: str = typer.Argument(..., help="JSON payload to send"),
    cwd: Optional[str] = typer.Option(None, "--cwd"),
) -> None:
    """Send a test message to a running processor."""
    from axon.client import AxonClient
    from axon.config import load_config

    project_dir = Path(cwd) if cwd else Path.cwd()

    try:
        config = load_config(project_dir)
        payload = json.loads(message)
    except json.JSONDecodeError:
        console.print("[red]Error:[/red] Message must be valid JSON")
        raise typer.Exit(1)
    except Exception as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1)

    secret_key = os.environ.get("AXON_SECRET_KEY")

    async def _run() -> None:
        async with AxonClient(provider=config.provider, secret_key=secret_key) as client:
            await client.send(processor_id, payload)
            console.print(f"[green]✓[/green] Message sent to {processor_id}")

    asyncio.run(_run())


@app.command()
def auth(
    provider: str = typer.Argument(..., help="Provider to authenticate (ionet, akash, acurast, fluence, koii)"),
) -> None:
    """Configure credentials for a provider."""
    console.print(f"[bold]Authenticating with {provider}...[/bold]")

    env_path = Path.cwd() / ".env"
    lines: list[str] = []

    if env_path.exists():
        lines = env_path.read_text().splitlines()

    provider_prompts: dict[str, list[tuple[str, str]]] = {
        "ionet": [("IONET_API_KEY", "io.net API key")],
        "akash": [("AKASH_MNEMONIC", "Akash BIP-39 mnemonic")],
        "acurast": [("ACURAST_MNEMONIC", "Acurast substrate mnemonic")],
        "fluence": [("FLUENCE_PRIVATE_KEY", "Fluence hex private key")],
        "koii": [("KOII_WALLET_PATH", "Koii wallet.json path")],
    }

    if provider not in provider_prompts:
        console.print(f"[red]Unknown provider:[/red] {provider}")
        raise typer.Exit(1)

    for env_var, prompt_text in provider_prompts[provider]:
        value = typer.prompt(prompt_text, hide_input="KEY" in env_var or "MNEMONIC" in env_var)
        # Update or append to .env
        updated = False
        for i, line in enumerate(lines):
            if line.startswith(f"{env_var}="):
                lines[i] = f"{env_var}={value}"
                updated = True
                break
        if not updated:
            lines.append(f"{env_var}={value}")

    env_path.write_text("\n".join(lines) + "\n")
    console.print(f"[green]✓[/green] Credentials saved to .env")
    console.print(f"\nRun [cyan]axon deploy[/cyan] to deploy your workload.")


if __name__ == "__main__":
    app()
