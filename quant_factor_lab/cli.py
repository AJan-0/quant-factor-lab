from __future__ import annotations

import argparse
import json
from pathlib import Path

from .pipeline import PipelineRunner


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="quant-factor-lab")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run the full factor research pipeline")
    run_parser.add_argument("--config", required=True, help="Path to a JSON pipeline config")

    admin_parser = subparsers.add_parser("admin", help="Start the local admin console")
    admin_parser.add_argument("--config", default="examples/demo_config.json", help="Path to a JSON pipeline config")
    admin_parser.add_argument("--host", default="127.0.0.1", help="Host interface for the admin server")
    admin_parser.add_argument("--port", type=int, default=8765, help="Port for the admin server")
    admin_parser.add_argument("--admin-token", default=None, help="Optional bearer token for admin API endpoints")
    admin_parser.add_argument("--rate-limit", type=int, default=240, help="Max requests per client in each rate window")
    admin_parser.add_argument("--rate-window", type=int, default=60, help="Rate limit window in seconds")

    export_parser = subparsers.add_parser("export-site", help="Export a GitHub Pages compatible static site")
    export_parser.add_argument("--config", default="examples/demo_config.json", help="Path to a JSON pipeline config")
    export_parser.add_argument("--site-dir", default="site", help="Directory to write the static site into")
    export_parser.add_argument(
        "--market-limit-per-symbol",
        type=int,
        default=1000,
        help="Maximum K-line rows per symbol to include in the static snapshot",
    )

    args = parser.parse_args(argv)
    if args.command == "run":
        result = PipelineRunner.from_config_path(Path(args.config)).run()
        print(json.dumps(result.summary, indent=2))
        return 0
    if args.command == "admin":
        from .admin.server import serve_admin

        serve_admin(
            config_path=Path(args.config),
            host=args.host,
            port=args.port,
            admin_token=args.admin_token,
            rate_limit=args.rate_limit,
            rate_window_seconds=args.rate_window,
        )
        return 0
    if args.command == "export-site":
        from .site_export import export_static_site

        manifest = export_static_site(
            config_path=Path(args.config),
            site_dir=Path(args.site_dir),
            market_limit_per_symbol=args.market_limit_per_symbol,
        )
        print(json.dumps(manifest, indent=2, ensure_ascii=False))
        return 0
    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
