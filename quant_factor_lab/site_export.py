from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pandas as pd

from quant_factor_lab.admin.server import AdminApp


def export_static_site(
    config_path: str | Path,
    site_dir: str | Path = "site",
    root: str | Path | None = None,
    market_limit_per_symbol: int = 1000,
    api_base_url: str | None = None,
) -> dict[str, Any]:
    """Export the admin console as a static snapshot site.

    The exported site is suitable for GitHub Pages. It contains the same
    static UI plus JSON files that emulate the read-only admin API endpoints.
    """

    app = AdminApp(config_path=config_path, root=root)
    project_root = app.root
    active_config = app.load_config()
    output_dir = app._output_dir(active_config)
    target = (project_root / site_dir if not Path(site_dir).is_absolute() else Path(site_dir)).resolve()
    try:
        target.relative_to(project_root)
    except ValueError as exc:
        raise ValueError("site_dir must be inside the project root") from exc
    if target == project_root:
        raise ValueError("site_dir cannot be the project root")
    if target.name in {".git", "runs", "quant_factor_lab", "tests"}:
        raise ValueError(f"Refusing to export into reserved project directory: {target.name}")

    resolved_api_base_url = (api_base_url if api_base_url is not None else os.environ.get("QFL_API_BASE_URL", "")).strip()
    _reset_dir(target)
    _copy_static_assets(target, resolved_api_base_url)
    _write_nojekyll(target)

    api_dir = target / "api"
    api_dir.mkdir(parents=True, exist_ok=True)
    snapshot = _sanitize_for_public_site(app.load_snapshot(active_config), project_root)
    market_payload = _sanitize_for_public_site(
        _load_static_market_payload(output_dir, market_limit_per_symbol),
        project_root,
    )
    config_payload = _sanitize_for_public_site(
        {"configPath": str(Path(config_path).name), "config": active_config},
        project_root,
    )
    runs_payload = _sanitize_for_public_site({"runs": [run.to_dict() for run in app.run_store.list_runs(limit=50)]}, project_root)

    _write_json(api_dir / "config.json", config_payload)
    _write_json(api_dir / "summary.json", snapshot)
    _write_json(api_dir / "market.json", market_payload)
    _write_json(api_dir / "realtime.json", _static_realtime_payload())
    _write_json(api_dir / "runs.json", runs_payload)
    _write_json(api_dir / "health.json", {"status": "ok", "mode": "static"})

    artifact_dir = target / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    copied_artifacts = _copy_artifacts(output_dir, artifact_dir)

    manifest = {
        "mode": "static",
        "siteDir": _public_path(target, project_root),
        "sourceOutputDir": _public_path(output_dir, project_root),
        "apiBaseUrl": resolved_api_base_url,
        "apiFiles": sorted(path.name for path in api_dir.glob("*.json")),
        "artifacts": copied_artifacts,
    }
    _write_json(target / "site_manifest.json", manifest)
    return manifest


def _reset_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def _copy_static_assets(target: Path, api_base_url: str = "") -> None:
    static_dir = Path(__file__).parent / "admin" / "static"
    for name in ("index.html", "app.css", "app.js", "runtime-config.js"):
        shutil.copy2(static_dir / name, target / name)
    static_site = "false" if api_base_url else "true"
    (target / "runtime-config.js").write_text(
        f"window.QFL_STATIC_SITE = {static_site};\n"
        f"window.QFL_API_BASE_URL = {json.dumps(api_base_url, ensure_ascii=False)};\n",
        encoding="utf-8",
    )


def _write_nojekyll(target: Path) -> None:
    (target / ".nojekyll").write_text("", encoding="utf-8")


def _load_static_market_payload(output_dir: Path, limit_per_symbol: int) -> dict[str, Any]:
    market_path = output_dir / "market_data.csv"
    technical_path = output_dir / "technical_indicators.csv"
    source_path = technical_path if technical_path.is_file() else market_path
    if not source_path.is_file():
        return {"symbols": [], "selectedSymbol": "", "rows": []}

    frame = pd.read_csv(source_path)
    if "symbol" not in frame.columns:
        return {"symbols": [], "selectedSymbol": "", "rows": []}

    frame["symbol"] = frame["symbol"].astype(str)
    symbols = sorted(frame["symbol"].dropna().unique().tolist())
    if "timestamp" in frame.columns:
        frame = frame.sort_values(["symbol", "timestamp"])
    safe_limit = max(20, min(int(limit_per_symbol), 5000))
    frame = frame.groupby("symbol", group_keys=False).tail(safe_limit)
    rows = [_json_ready(row) for row in frame.to_dict(orient="records")]
    return {"symbols": symbols, "selectedSymbol": symbols[0] if symbols else "", "rows": rows}


def _copy_artifacts(output_dir: Path, artifact_dir: Path) -> list[dict[str, Any]]:
    if not output_dir.is_dir():
        return []
    copied: list[dict[str, Any]] = []
    for source in sorted(output_dir.iterdir()):
        if not source.is_file():
            continue
        target = artifact_dir / source.name
        shutil.copy2(source, target)
        copied.append({"name": source.name, "size": target.stat().st_size})
    return copied


def _static_realtime_payload() -> dict[str, Any]:
    return {
        "status": "static",
        "error": None,
        "startedAt": None,
        "stoppedAt": None,
        "messageCount": 0,
        "orderBooks": [],
        "trades": [],
        "liquidations": [],
        "events": [
            {
                "timestamp": None,
                "level": "INFO",
                "message": "静态网页展示的是发布时的数据快照；实时流需要连接后端服务。",
            }
        ],
    }


def _write_json(path: Path, payload: Any) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(_json_ready(payload), handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def _sanitize_for_public_site(value: Any, root: Path) -> Any:
    if isinstance(value, dict):
        return {str(key): _sanitize_for_public_site(item, root) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_for_public_site(item, root) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_for_public_site(item, root) for item in value]
    if isinstance(value, str):
        return _public_string(value, root)
    return value


def _public_string(value: str, root: Path) -> str:
    normalized = value.replace("\\", "/")
    root_text = str(root).replace("\\", "/")
    if normalized.startswith(root_text):
        return "." + normalized[len(root_text) :]
    return value


def _public_path(path: Path, root: Path) -> str:
    return _public_string(str(path), root)


def _json_ready(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    if isinstance(value, tuple):
        return [_json_ready(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "item"):
        return _json_ready(value.item())
    if isinstance(value, float) and pd.isna(value):
        return None
    return value
