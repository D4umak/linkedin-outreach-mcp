"""HeyLead CLI entry point.

This module allows running HeyLead as:
    python -m heylead                           # Start MCP server (stdio)
    python -m heylead --transport streamable-http  # Start HTTP server
    python -m heylead init                      # First-time setup
    python -m heylead version                   # Show version
    python -m heylead reset                     # Delete all data
"""

from __future__ import annotations

import sys


def _parse_serve_args(args: list[str]) -> dict:
    """Parse --transport, --host, --port from CLI args."""
    transport = "stdio"
    host = "0.0.0.0"
    port = 8080

    i = 0
    while i < len(args):
        if args[i] == "--transport" and i + 1 < len(args):
            transport = args[i + 1]
            i += 2
        elif args[i] == "--host" and i + 1 < len(args):
            host = args[i + 1]
            i += 2
        elif args[i] == "--port" and i + 1 < len(args):
            port = int(args[i + 1])
            i += 2
        else:
            break
    return {"transport": transport, "host": host, "port": port}


USAGE = """usage: heylead [--transport stdio|sse|streamable-http] [--host H] [--port P]
       heylead init | version | reset
       heylead daemon [--install [--load] | --uninstall | --status]

With no command, heylead starts the MCP server. `heylead daemon` with no
flag runs the scheduler daemon in the foreground (launchd uses this)."""

_DAEMON_FLAGS = {"--install", "--load", "--uninstall", "--status"}


def _print_usage_and_exit(code: int = 0) -> None:
    print(USAGE, file=sys.stdout if code == 0 else sys.stderr)
    sys.exit(code)


def main() -> None:
    """CLI router — simple arg parsing without pulling in click for MCP mode."""
    args = sys.argv[1:]

    if args and args[0] in ("--help", "-h", "help"):
        _print_usage_and_exit(0)

    if not args or args[0].startswith("--"):
        # Default: start MCP server (with optional --transport/--host/--port)
        serve_args = _parse_serve_args(args)
        from .server import main as serve
        serve(**serve_args)
        return

    cmd = args[0].lower()

    if cmd == "version" or cmd == "--version" or cmd == "-v":
        from . import __version__
        print(f"HeyLead v{__version__}")

    elif cmd == "daemon":
        # `heylead daemon --help` used to fall through to run_daemon() and
        # start a real scheduler, which then took the leader lock from the
        # launchd-managed one. Anything that is not a known flag stops here.
        if any(a in ("--help", "-h") for a in args[1:]):
            _print_usage_and_exit(0)
        unknown = [a for a in args[1:] if a not in _DAEMON_FLAGS]
        if unknown:
            print(f"heylead daemon: unknown argument(s): {' '.join(unknown)}", file=sys.stderr)
            _print_usage_and_exit(2)
        from .daemon import install, run_daemon, uninstall
        if "--install" in args:
            print(install(load="--load" in args))
        elif "--uninstall" in args:
            print(uninstall())
        elif "--status" in args:
            from .daemon import daemon_status
            for k, v in daemon_status().items():
                print(f"{k}: {v}")
        else:
            sys.exit(run_daemon())

    elif cmd == "init":
        from . import config
        config.ensure_dirs()
        cfg = config.load_config()
        print("✅ HeyLead initialized!")
        print(f"   Config: {config.config_path()}")
        print(f"   Database: {config.db_path()}")
        print(f"   Logs: {config.log_path()}")
        print()
        print("Next steps:")
        print('1. In Cursor, open AI chat (Cmd+L) and say: "Set up my HeyLead profile"')
        print("2. The AI will give you a link — sign in with Google, connect LinkedIn, paste token.")
        print("   No API keys needed!")
        print()
        print("Not using Cursor yet? Install HeyLead with one click:")
        print("   cursor://anysphere.cursor-deeplink/mcp/install?name=heylead&config=eyJjb21tYW5kIjoidXZ4IiwiYXJncyI6WyJoZXlsZWFkIl19")

    elif cmd == "reset":
        confirm = input("⚠️  This will delete ALL HeyLead data. Continue? (yes/no): ")
        if confirm.strip().lower() == "yes":
            from . import config
            from .db.schema import reset_db
            import shutil

            reset_db()

            # Remove legacy cookie file if it exists
            cookie_path = config.cookie_path()
            if cookie_path.exists():
                cookie_path.unlink()

            # Remove config
            cfg_path = config.config_path()
            if cfg_path.exists():
                cfg_path.unlink()

            # Remove logs
            log_dir = config._heylead_home() / "logs"
            if log_dir.exists():
                shutil.rmtree(log_dir)

            print("✅ All HeyLead data deleted.")
        else:
            print("Cancelled.")

    elif cmd == "backup":
        from .db.backup import create_backup, list_backups, rotate_backups

        path = create_backup("manual")
        if path:
            rotate_backups()
            print(f"Backup created: {path}")
        else:
            print("No database to back up.")

    elif cmd == "restore":
        from .db.backup import list_backups, restore_backup

        backups = list_backups()
        if not backups:
            print("No backups available.")
            sys.exit(1)

        print("Available backups:")
        for i, b in enumerate(backups, 1):
            from datetime import datetime

            ts = datetime.fromtimestamp(b.timestamp).strftime("%Y-%m-%d %H:%M:%S")
            size_kb = b.size_bytes / 1024
            print(f"  {i}. {ts}  ({b.reason}, {size_kb:.0f} KB)")

        choice = input(f"\nRestore which backup? (1-{len(backups)}, or 'cancel'): ").strip()
        if choice.lower() == "cancel" or not choice:
            print("Cancelled.")
            sys.exit(0)
        try:
            idx = int(choice) - 1
            if not (0 <= idx < len(backups)):
                raise ValueError
        except ValueError:
            print("Invalid choice.")
            sys.exit(1)

        selected = backups[idx]
        confirm = input(f"Restore from {selected.path.name}? This will overwrite the current database. (yes/no): ").strip()
        if confirm.lower() == "yes":
            restore_backup(selected.path)
            print(f"Database restored from {selected.path.name}")
        else:
            print("Cancelled.")

    elif cmd == "help" or cmd == "--help" or cmd == "-h":
        print("HeyLead — MCP-native autonomous LinkedIn SDR")
        print()
        print("Usage:")
        print("  heylead                                  Start MCP server (stdio)")
        print("  heylead --transport streamable-http       Start HTTP server")
        print("  heylead --transport sse --port 3000       Start SSE server on port 3000")
        print("  heylead init                              Initialize HeyLead")
        print("  heylead version                           Show version")
        print("  heylead backup                             Create a database backup")
        print("  heylead restore                            Restore from a backup")
        print("  heylead reset                             Delete all data")
        print()
        print("Transport options:")
        print("  --transport <type>   stdio (default), sse, or streamable-http")
        print("  --host <addr>        Bind address for HTTP (default: 0.0.0.0)")
        print("  --port <num>         Port for HTTP (default: 8080)")
        print()
        print("Install:")
        print("  Claude Code: claude mcp add heylead -- uvx heylead")
        print("  Cursor:      Settings → MCP → Add → Name: heylead, Command: uvx heylead")
        print()
        print("Remote alternative (hosted endpoint, nothing runs locally):")
        print("  Claude Code: claude mcp add --transport http heylead https://heylead.dev/mcp")
        print("  Cursor:      Settings → MCP → Add → Type: URL, Name: heylead, URL: https://heylead.dev/mcp")

    else:
        print(f"Unknown command: {cmd}")
        print("Run 'heylead help' for usage.")
        sys.exit(1)


if __name__ == "__main__":
    main()
