"""First-run setup: adding a Google account without a phone.

gplaydl downloads through a Google account added straight from this
machine. Setup is: sign in once in a browser, paste the resulting
oauth_token cookie, and gplaydl does the rest -- enrolling this machine's
own identity with the dispenser, minting a long-lived AAS token, and
syncing the account, all privately under this machine.
"""

from __future__ import annotations

import sys
from typing import Optional

import typer
from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from gplaydl.auth import (
    DEFAULT_DISPENSER,
    DispenserError,
    api_key_for,
    device_secret,
    dispenser_base,
    save_link,
)
from gplaydl.gaccount import (
    EMBEDDED_SETUP_URL,
    GoogleAuthError,
    device_label,
    enroll_device,
    extract_oauth_token,
    mint_aas_token,
    sync_account,
)


def ensure_linked(console: Console, dispenser_url: Optional[str] = None) -> None:
    """Walk a new user through adding an account before their first dispense.

    Runs only when the target dispenser is the linked or default one and no
    key exists for it yet. Someone pointing -d at a server we hold no key for
    gets a clean refusal from that server instead of a wizard for it.
    """
    if dispenser_url:
        return
    base = dispenser_base()
    if api_key_for(base):
        return

    if not _interactive():
        console.print(
            "[red]This machine has not added a Google account yet.[/red] "
            "Run [bold]gplaydl link[/bold] in a terminal once, or set "
            "GPLAYDL_API_KEY. Setup takes about two minutes:"
        )
        console.print(_steps_text(base))
        raise typer.Exit(code=1)

    link(console, base, first_run=True)


def link(
    console: Console,
    base: Optional[str] = None,
    oauth_token: Optional[str] = None,
    email: Optional[str] = None,
    first_run: bool = False,
) -> None:
    """Walk through sign-in, mint an AAS token, and sync it to the dispenser."""
    base = base or dispenser_base()

    console.print()
    console.print(_walkthrough_panel(console, base, first_run))
    console.print()

    if not oauth_token:
        try:
            raw = Prompt.ask("[bold]Paste the Cookie header or oauth_token value[/bold]", console=console)
        except (EOFError, KeyboardInterrupt):
            console.print()
            raise typer.Exit(code=1)
        oauth_token = extract_oauth_token(raw)
    else:
        oauth_token = extract_oauth_token(oauth_token)

    if not oauth_token:
        console.print("[red]No oauth_token found in what you pasted.[/red]")
        raise typer.Exit(code=1)

    if not email:
        try:
            email = Prompt.ask("[bold]Google account email you signed in as[/bold]", console=console)
        except (EOFError, KeyboardInterrupt):
            console.print()
            raise typer.Exit(code=1)
    email = email.strip()
    if not email:
        console.print("[red]No email given.[/red]")
        raise typer.Exit(code=1)

    try:
        with console.status("Minting a Play token from your sign-in..."):
            minted = mint_aas_token(email, oauth_token)
    except GoogleAuthError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)

    try:
        with console.status("Enrolling this machine with the dispenser..."):
            api_key = api_key_for(base) or enroll_device(base, device_secret(), device_label())
        with console.status(f"Syncing {minted.email}..."):
            sync_account(base, api_key, minted.email, minted.aas_token)
    except DispenserError as exc:
        console.print(f"[red]{exc.message}[/red]")
        raise typer.Exit(code=1)

    path = save_link(base, api_key)
    console.print(
        f"\n[bold green]Account added.[/bold green] {minted.email} can now download "
        f"through {base}.\n"
        f"[dim]Key saved to {path}[/dim]\n"
    )


def _walkthrough_panel(console: Console, base: str, first_run: bool) -> Panel:
    intro = (
        "gplaydl downloads through a Google account you add yourself, right from "
        "this machine. This is a one-time setup and takes about two minutes:"
        if first_run
        else "Adding an account re-syncs this machine's identity with the dispenser:"
    )
    warning = (
        "[yellow]Google may flag, lock, or restrict accounts used with unofficial "
        "clients. Please use a separate account and continue at your own risk.[/yellow]"
    )
    return Panel(
        Group(intro, "", warning, "", _steps_table()),
        title="[bold]Set up gplaydl (One-time)[/bold]" if first_run else "[bold]Link gplaydl[/bold]",
        title_align="left",
        border_style="bright_black",
        box=box.ROUNDED,
        padding=(1, 2),
        width=min(78, console.width),
    )


def _steps_table() -> Table:
    steps = Table(box=None, show_header=False, padding=(0, 1))
    steps.add_column(style="bold cyan", justify="right", width=1)
    steps.add_column()
    steps.add_row(
        "1",
        f"In a browser, sign in with a spare Google account at:\n[bold cyan]{EMBEDDED_SETUP_URL}[/bold cyan]",
    )
    steps.add_row(
        "2",
        "Open dev tools (F12) -> Application/Storage -> Cookies, and copy the "
        "[bold]oauth_token[/bold] cookie's value for accounts.google.com.",
    )
    steps.add_row("3", "Paste it here, along with the email you signed in as.")
    return steps


def _steps_text(base: str) -> str:
    return (
        "  Google may flag, lock, or restrict accounts used with unofficial clients.\n"
        "  Please use a separate account and continue at your own risk.\n\n"
        f"  1. Sign in with a spare Google account at: {EMBEDDED_SETUP_URL}\n"
        "  2. Copy the oauth_token cookie's value from dev tools.\n"
        "  3. Run gplaydl link and paste it in, with the email you signed in as.\n"
        f"  (Dispenser: {base})"
    )


def _interactive() -> bool:
    # Both directions have to be a terminal: the wizard prints a panel and
    # reads a cookie value back.
    return sys.stdin.isatty() and sys.stdout.isatty()
