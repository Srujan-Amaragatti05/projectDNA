"""ProjectDNA CLI - Local-first Python project intelligence system."""

import os
import sqlite3
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from projectdna import __version__
from projectdna.db.connection import get_ro_connection
from projectdna.db.repository import ProjectRepository, IndexRepository, normalize_path
from projectdna.models import ProjectRecord
from projectdna.service.ipc import (
    send_ipc_request,
    ServiceUnavailableError,
)
from projectdna.service.manager import (
    is_service_running,
    start_service_detached,
    stop_service,
    ensure_service_running,
)

app = typer.Typer(
    name="dna",
    help="ProjectDNA - Local-first Python project intelligence system",
    add_completion=False,
)
service_app = typer.Typer(name="service", help="Manage ProjectDNA background service.")
app.add_typer(service_app)

console = Console()
err_console = Console(stderr=True)


def _resolve_project(conn: sqlite3.Connection, identifier: str) -> Optional[ProjectRecord]:
    """Resolve a project by ID, canonical root path, or unique display name."""
    if identifier.isdigit():
        proj = ProjectRepository.get_project_by_id(conn, int(identifier))
        if proj:
            return proj

    norm_root = normalize_path(identifier)
    proj = ProjectRepository.get_project_by_root(conn, norm_root)
    if proj:
        return proj

    # Check by display name
    all_projects = ProjectRepository.list_projects(conn)
    matches = [p for p in all_projects if p.name.lower() == identifier.lower()]
    if len(matches) == 1:
        return matches[0]
    elif len(matches) > 1:
        err_console.print(
            f"[red]Error:[/red] Multiple projects found with name '{identifier}'. "
            "Please specify the project ID or canonical root path."
        )
        raise typer.Exit(code=1)
    return None


def version_callback(value: bool):
    if value:
        console.print(f"ProjectDNA CLI version: {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Optional[bool] = typer.Option(
        None,
        "--version",
        "-v",
        help="Show the ProjectDNA version and exit.",
        callback=version_callback,
        is_eager=True,
    ),
):
    """ProjectDNA - Local-first Python project intelligence system."""
    pass


@app.command(name="list")
def list_cmd():
    """List all registered Python projects."""
    try:
        conn = get_ro_connection()
    except FileNotFoundError:
        console.print("[dim]No ProjectDNA database found. No projects are currently registered.[/dim]")
        raise typer.Exit(code=0)

    try:
        projects = ProjectRepository.list_projects(conn)
        if not projects:
            console.print("[dim]No registered projects found.[/dim]")
            raise typer.Exit(code=0)

        table = Table(title="Registered Projects", show_lines=False)
        table.add_column("ID", justify="right", style="cyan", no_wrap=True)
        table.add_column("Name", style="bold green")
        table.add_column("Root Path", style="dim")
        table.add_column("Status", justify="center")
        table.add_column("Last Scanned", style="magenta")

        for p in projects:
            status_style = "green" if p.status == "active" else "yellow"
            last_scan = str(p.last_scanned_at) if p.last_scanned_at else "Never"
            table.add_row(
                str(p.id),
                p.name,
                p.root_path,
                f"[{status_style}]{p.status}[/{status_style}]",
                last_scan,
            )

        console.print(table)
    finally:
        conn.close()


@app.command(name="status")
def status_cmd(
    project: str = typer.Argument(..., help="Project ID, name, or canonical root path"),
):
    """Show project and indexing health."""
    try:
        conn = get_ro_connection()
    except FileNotFoundError:
        err_console.print("[red]Error:[/red] No ProjectDNA database found. No projects are registered.")
        raise typer.Exit(code=1)

    try:
        proj = _resolve_project(conn, project)
        if not proj:
            err_console.print(f"[red]Error:[/red] Project '{project}' not found.")
            raise typer.Exit(code=1)

        files_map = IndexRepository.get_files_by_project(conn, proj.id)
        total_files = len(files_map)
        syntax_errors = [f for f in files_map.values() if f.has_syntax_error]

        # Check on-disk root status
        root_path = Path(proj.root_path)
        root_exists = root_path.exists() and root_path.is_dir()

        # Check modified files on disk
        modified_count = 0
        if root_exists:
            for f in files_map.values():
                p = Path(f.abs_path)
                try:
                    if not p.exists() or p.stat().st_mtime != f.mtime:
                        modified_count += 1
                except OSError:
                    modified_count += 1

        table = Table(show_header=False, box=None)
        table.add_column("Key", style="bold cyan")
        table.add_column("Value")

        table.add_row("Project ID", str(proj.id))
        table.add_row("Name", proj.name)
        table.add_row("Root Path", proj.root_path)
        table.add_row(
            "Root on Disk",
            "[green]Exists[/green]" if root_exists else "[red]Missing[/red]",
        )
        table.add_row("Status", f"[green]{proj.status}[/green]")
        table.add_row(
            "Last Scanned",
            str(proj.last_scanned_at) if proj.last_scanned_at else "Never",
        )
        table.add_row("Indexed Files", str(total_files))

        if syntax_errors:
            table.add_row(
                "Syntax Errors",
                f"[yellow]{len(syntax_errors)} file(s) with errors[/yellow]",
            )
        else:
            table.add_row("Syntax Errors", "[green]0[/green]")

        if modified_count > 0:
            table.add_row(
                "Pending Changes",
                f"[yellow]{modified_count} file(s) modified on disk since last scan[/yellow]",
            )
        else:
            table.add_row("Pending Changes", "[green]Up to date[/green]")

        console.print(Panel(table, title=f"Project Status: [bold]{proj.name}[/bold]", expand=False))

        if syntax_errors:
            err_table = Table(title="Files with Syntax Errors", show_lines=True)
            err_table.add_column("Relative Path", style="yellow")
            err_table.add_column("Error Message", style="red")
            for err_file in syntax_errors:
                err_table.add_row(err_file.rel_path, err_file.error_message or "Unknown syntax error")
            console.print(err_table)

    finally:
        conn.close()


@app.command(name="search")
def search_cmd(
    query: str = typer.Argument(..., help="Search query (full text or symbol)"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Filter by project ID, name, or root"),
    limit: int = typer.Option(20, "--limit", "-n", help="Maximum number of results"),
):
    """Search indexed code and symbols."""
    try:
        conn = get_ro_connection()
    except FileNotFoundError:
        console.print("[dim]No ProjectDNA database found. No projects are registered to search.[/dim]")
        raise typer.Exit(code=0)

    try:
        project_id = None
        if project:
            proj = _resolve_project(conn, project)
            if not proj:
                err_console.print(f"[red]Error:[/red] Project '{project}' not found.")
                raise typer.Exit(code=1)
            project_id = proj.id

        code_results = IndexRepository.search_code(conn, query, project_id=project_id, limit=limit)
        sym_results = IndexRepository.search_symbols(conn, query, project_id=project_id, limit=limit)

        if not code_results and not sym_results:
            console.print(f"[dim]No matches found for query '[bold]{query}[/bold]'.[/dim]")
            raise typer.Exit(code=0)

        # Display symbol results
        if sym_results:
            console.print(f"\n[bold green]Symbol Matches ({len(sym_results)}):[/bold green]")
            sym_table = Table(show_lines=False)
            sym_table.add_column("Symbol", style="bold cyan")
            sym_table.add_column("Kind", style="magenta")
            sym_table.add_column("Project", style="green")
            sym_table.add_column("File:Line", style="dim")
            sym_table.add_column("Docstring", style="italic")

            for s in sym_results:
                loc = f"{s['rel_path']}:{s['start_line']}-{s['end_line']}"
                parent = f"{s['parent_symbol']}." if s.get("parent_symbol") else ""
                full_name = f"{parent}{s['name']}"
                doc = (s["docstring"] or "").strip().split("\n")[0][:60]
                sym_table.add_row(full_name, s["kind"], s["project_name"], loc, doc)
            console.print(sym_table)

        # Display code results with source freshness validation
        if code_results:
            from rich.markup import escape
            console.print(f"\n[bold green]Code Matches ({len(code_results)}):[/bold green]")
            for c in code_results:
                p = Path(c["abs_path"])
                freshness_tag = ""
                if not p.exists():
                    freshness_tag = " [red][Stale: file deleted on disk][/red]"
                else:
                    try:
                        stat = p.stat()
                        if stat.st_mtime != c.get("mtime"):
                            freshness_tag = " [yellow][Modified on disk since indexing][/yellow]"
                    except OSError:
                        freshness_tag = " [yellow][Unreadable on disk][/yellow]"

                syntax_tag = ""
                if c.get("has_syntax_error"):
                    syntax_tag = f" [yellow][Syntax Warning: {c.get('error_message')}][/yellow]"

                console.print(
                    f"\n[bold cyan]{c['project_name']}[/bold cyan] : [bold]{c['rel_path']}[/bold]{freshness_tag}{syntax_tag}"
                )
                raw_snippet = c.get("snippet", "")
                formatted_snippet = (
                    escape(raw_snippet)
                    .replace(escape("[MATCH]"), "[bold yellow]")
                    .replace(escape("[/MATCH]"), "[/bold yellow]")
                )
                console.print(f"  {formatted_snippet}")

    finally:
        conn.close()


@app.command(name="add")
def add_cmd(
    path: str = typer.Argument(..., help="Path to project directory to register and monitor"),
):
    """Register project, initialize index, and automatically start monitoring."""
    target_path = Path(path).resolve()
    if not target_path.exists():
        err_console.print(f"[red]Error:[/red] Path does not exist: {target_path}")
        raise typer.Exit(code=1)
    if not target_path.is_dir():
        err_console.print(f"[red]Error:[/red] Path is not a directory: {target_path}")
        raise typer.Exit(code=1)

    try:
        ensure_service_running()
        result = send_ipc_request("add_project", {"path": str(target_path)})
    except ServiceUnavailableError as e:
        err_console.print(f"[red]Service Error:[/red] {e}")
        raise typer.Exit(code=1)
    except Exception as e:
        err_console.print(f"[red]Error registering project:[/red] {e}")
        raise typer.Exit(code=1)

    indexing = result.get("indexing", {})
    console.print(f"[bold green]Project registered successfully:[/bold green] {result.get('name')}")
    console.print(f"  Project ID: {result.get('project_id')}")
    console.print(f"  Root Path:  {result.get('root_path')}")
    console.print(
        f"  Initial Indexing: {indexing.get('added', 0)} added, "
        f"{indexing.get('unchanged', 0)} unchanged"
    )
    console.print("[dim]Continuous filesystem monitoring is now active for this project.[/dim]")


@app.command(name="scan")
def scan_cmd(
    project: str = typer.Argument(..., help="Project ID, name, or canonical root path to scan"),
):
    """Request manual reconciliation."""
    try:
        ensure_service_running()
        result = send_ipc_request("scan_project", {"identifier": project})
    except ServiceUnavailableError as e:
        err_console.print(f"[red]Service Error:[/red] {e}")
        raise typer.Exit(code=1)
    except Exception as e:
        err_console.print(f"[red]Scan failed:[/red] {e}")
        raise typer.Exit(code=1)

    changes = result.get("changes", {})
    console.print(f"[bold green]Reconciliation scan completed:[/bold green] {result.get('name')}")
    console.print(
        f"  Changes: {changes.get('added', 0)} added, {changes.get('modified', 0)} modified, "
        f"{changes.get('deleted', 0)} deleted, {changes.get('unchanged', 0)} unchanged"
    )


@app.command(name="remove")
def remove_cmd(
    project: str = typer.Argument(..., help="Project ID, name, or canonical root path to remove"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm removal without interactive prompt"),
):
    """Require confirmation before deleting the registration and associated indexed records. Never delete original source files."""
    proj_name = project
    root_path = None
    try:
        conn = get_ro_connection()
        try:
            proj = _resolve_project(conn, project)
            if proj:
                proj_name = proj.name
                root_path = proj.root_path
        finally:
            conn.close()
    except Exception:
        pass

    if not yes:
        confirmed = typer.confirm(
            f"Are you sure you want to remove project '{proj_name}' from ProjectDNA? "
            "(Original source files will NOT be deleted)",
            default=False,
        )
        if not confirmed:
            console.print("[dim]Removal cancelled.[/dim]")
            raise typer.Exit(code=0)

    try:
        ensure_service_running()
        result = send_ipc_request("remove_project", {"identifier": project})
    except ServiceUnavailableError as e:
        err_console.print(f"[red]Service Error:[/red] {e}")
        raise typer.Exit(code=1)
    except Exception as e:
        err_console.print(f"[red]Removal failed:[/red] {e}")
        raise typer.Exit(code=1)

    console.print(f"[bold green]Project removed from ProjectDNA:[/bold green] {result.get('name')}")
    if root_path:
        console.print(f"[dim]Original source files at '{root_path}' remain untouched.[/dim]")
    else:
        console.print("[dim]Original source files remain untouched.[/dim]")


@service_app.command(name="status")
def service_status():
    """Check background service status."""
    if is_service_running():
        try:
            data = send_ipc_request("status")
            console.print(
                f"[bold green]ProjectDNA service is running[/bold green] "
                f"(PID: {data.get('pid')}, Port: {data.get('port')})"
            )
            console.print(f"  Uptime: {int(data.get('uptime', 0))}s")
            console.print(f"  Monitored Projects: {len(data.get('projects', []))}")
            for p in data.get("projects", []):
                console.print(f"    - {p['name']} ({p['root_path']})")
        except Exception as e:
            err_console.print(f"[yellow]Service responded with error:[/yellow] {e}")
    else:
        console.print("[yellow]ProjectDNA background service is stopped.[/yellow]")


@service_app.command(name="start")
def service_start():
    """Start background service daemon."""
    if is_service_running():
        console.print("[cyan]ProjectDNA background service is already running.[/cyan]")
        return
    console.print("Starting ProjectDNA background service...")
    if start_service_detached():
        console.print("[bold green]ProjectDNA background service started successfully.[/bold green]")
    else:
        err_console.print("[red]Failed to start ProjectDNA background service.[/red]")
        raise typer.Exit(code=1)


@service_app.command(name="stop")
def service_stop():
    """Stop background service daemon."""
    if not is_service_running():
        console.print("[dim]ProjectDNA background service is not running.[/dim]")
        return
    console.print("Stopping ProjectDNA background service...")
    if stop_service():
        console.print("[bold green]ProjectDNA background service stopped successfully.[/bold green]")
    else:
        err_console.print("[red]Failed to stop ProjectDNA background service.[/red]")
        raise typer.Exit(code=1)


@app.command(name="serve")
def serve_cmd():
    """Expose read-only MCP tools."""
    console.print(
        "[yellow]Phase 4 MCP Dependency:[/yellow] The ProjectDNA read-only MCP server will be "
        "available in Phase 4. Available Phase 2/3 commands: 'dna list', 'dna status', 'dna search', "
        "'dna add', 'dna scan', 'dna remove', 'dna service'."
    )


if __name__ == "__main__":
    app()
