"""AxonSDK CLI built with Typer."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(
    name="axon",
    help="AxonSDK — Provider-agnostic edge compute SDK for AI workload routing",
    add_completion=False,
)
console = Console()


@app.command()
def init(
    provider: str | None = typer.Option(None, "--provider", "-p", help="Provider name"),
) -> None:
    """Interactive setup: create axon.json and .env for your project."""
    from axon.config import generate_config, generate_env_template

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
    console.print("[green]✓[/green] Created axon.json")

    env_path = cwd / ".env"
    if not env_path.exists():
        env_content = generate_env_template(provider)  # type: ignore[arg-type]
        env_path.write_text(env_content)
        console.print("[green]✓[/green] Created .env")

    console.print("\n[bold]Next steps:[/bold]")
    console.print(f"  1. Run [cyan]axon auth {provider}[/cyan] to configure credentials")
    console.print("  2. Run [cyan]axon deploy[/cyan] to deploy your workload")


@app.command()
def deploy(
    cwd: str | None = typer.Option(None, "--cwd", help="Project directory"),
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

    from axon.types import DeploymentConfig

    # Convert AxonConfig → DeploymentConfig
    deploy_config = DeploymentConfig(
        name=config.project_name,
        entry_point=config.entry_point,
        runtime=config.runtime,
        env=config.env,
        metadata=config.metadata,
    )

    async def _run() -> None:
        async with AxonClient(provider=config.provider, secret_key=secret_key) as client:
            with console.status(f"Deploying to [cyan]{config.provider}[/cyan]..."):
                deployment = await client.deploy(deploy_config)

            table = Table(title="Deployment")
            table.add_column("Field", style="bold")
            table.add_column("Value", style="cyan")
            table.add_row("ID", deployment.id)
            table.add_row("Status", deployment.status)
            table.add_row("Provider", deployment.provider)
            table.add_row("Endpoint", deployment.endpoint or "(pending)")
            console.print(table)

    try:
        asyncio.run(_run())
    except Exception as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1)


@app.command()
def status(
    cwd: str | None = typer.Option(None, "--cwd", help="Project directory"),
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
    cwd: str | None = typer.Option(None, "--cwd"),
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
def teardown(
    deployment_id: str = typer.Argument(..., help="Deployment ID to remove"),
    cwd: str | None = typer.Option(None, "--cwd", help="Project directory"),
) -> None:
    """Delete a deployment from the provider."""
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
            msg = f"Removing [cyan]{deployment_id}[/cyan] from [cyan]{config.provider}[/cyan]..."
            with console.status(msg):
                await client.teardown(deployment_id)
            console.print(f"[green]✓[/green] Deployment [cyan]{deployment_id}[/cyan] removed.")

    try:
        asyncio.run(_run())
    except Exception as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1)


@app.command()
def auth(
    provider: str = typer.Argument(
        ..., help="Provider to authenticate (ionet, akash, acurast, fluence, koii)"
    ),
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
        "aws": [
            ("AWS_ACCESS_KEY_ID", "AWS Access Key ID"),
            ("AWS_SECRET_ACCESS_KEY", "AWS Secret Access Key"),
            ("AWS_DEFAULT_REGION", "AWS region (e.g. us-east-1)"),
        ],
        "gcp": [
            ("GOOGLE_APPLICATION_CREDENTIALS", "Path to GCP service account JSON"),
            ("GCP_PROJECT_ID", "GCP project ID"),
        ],
        "azure": [
            ("AZURE_CLIENT_ID", "Azure client ID"),
            ("AZURE_CLIENT_SECRET", "Azure client secret"),
            ("AZURE_TENANT_ID", "Azure tenant ID"),
            ("AZURE_SUBSCRIPTION_ID", "Azure subscription ID"),
        ],
        "cloudflare": [
            ("CLOUDFLARE_API_TOKEN", "Cloudflare API token"),
            ("CLOUDFLARE_ACCOUNT_ID", "Cloudflare account ID"),
        ],
        "fly": [("FLY_API_TOKEN", "Fly.io API token")],
    }

    if provider not in provider_prompts:
        console.print(f"[red]Unknown provider:[/red] {provider}")
        console.print(f"Supported: {', '.join(sorted(provider_prompts))}")
        raise typer.Exit(1)

    def _sanitize_value(v: str) -> str:
        """Strip characters that could inject new lines into the .env file."""
        return v.replace("\r", "").replace("\n", "")

    for env_var, prompt_text in provider_prompts[provider]:
        sensitive = (
            "KEY" in env_var or "MNEMONIC" in env_var or "SECRET" in env_var or "TOKEN" in env_var
        )
        raw_value = typer.prompt(prompt_text, hide_input=sensitive)
        value = _sanitize_value(raw_value)
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
    # Restrict .env permissions to owner-read/write only (prevents other OS users
    # from reading credentials stored in plain text).
    os.chmod(str(env_path), 0o600)
    console.print("[green]✓[/green] Credentials saved to .env")
    console.print("\nRun [cyan]axon deploy[/cyan] to deploy your workload.")


if __name__ == "__main__":
    app()
