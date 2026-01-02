from __future__ import annotations

import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from urllib import request


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_post_json(url: str, payload: dict, timeout_s: float = 1.5) -> None:
    try:
        data = json.dumps(payload).encode("utf-8")
        req = request.Request(url, data=data, headers={"Content-Type": "application/json"})
        request.urlopen(req, timeout=timeout_s).read()
    except Exception:
        # never break the CLI
        return


def _run_in_background(fn, *args, **kwargs) -> None:
    t = threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True)
    t.start()


def _default_user_config_path() -> Path:
    # ApiMesh already writes apimesh/config.json in the workspace. :contentReference[oaicite:4]{index=4}
    p = os.getenv("APIMESH_USER_CONFIG_PATH") or str(Path("apimesh") / "config.json")
    return Path(p)


def _load_json(path: Path) -> Dict[str, Any]:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_json(path: Path, obj: Dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _get_or_create_install_id(cfg_path: Path) -> str:
    cfg = _load_json(cfg_path)
    iid = cfg.get("telemetry_install_id")
    if not iid:
        iid = str(uuid.uuid4())  # random install ID, not machine ID
        cfg["telemetry_install_id"] = iid
        _save_json(cfg_path, cfg)
    return iid


# Set this to your PostHog project API key to bake in a default.
DEFAULT_POSTHOG_API_KEY = "phc_te3mp08IuF167Pd3zQvC0ocdGd4Wj2undKV1cEQE1n1"
DEFAULT_POSTHOG_HOST = "https://us.i.posthog.com"
DEFAULT_TELEMETRY_ENABLED = True


@dataclass
class PostHogTelemetry:
    enabled: bool
    api_key: str
    host: str = "https://us.i.posthog.com"  # or https://eu.i.posthog.com :contentReference[oaicite:5]{index=5}
    cfg_path: Path = _default_user_config_path()

    @classmethod
    def from_env(cls) -> "PostHogTelemetry":
        env_enabled = os.getenv("APIMESH_TELEMETRY")
        enabled = DEFAULT_TELEMETRY_ENABLED if env_enabled is None else env_enabled == "1"

        api_key = (
            os.getenv("APIMESH_POSTHOG_API_KEY", "").strip()
            or os.getenv("APIMESH_DEFAULT_POSTHOG_API_KEY", "").strip()
            or DEFAULT_POSTHOG_API_KEY
        )
        host = (
            os.getenv("APIMESH_POSTHOG_HOST", "").strip()
            or os.getenv("APIMESH_DEFAULT_POSTHOG_HOST", "").strip()
            or DEFAULT_POSTHOG_HOST
        )
        return cls(enabled=enabled and bool(api_key), api_key=api_key, host=host)

    def _endpoint(self) -> str:
        # PostHog event capture endpoint is /i/v0/e/ :contentReference[oaicite:6]{index=6}
        return f"{self.host.rstrip('/')}/i/v0/e/"

    def new_run_id(self) -> str:
        return str(uuid.uuid4())

    def capture(self, event: str, properties: Optional[Dict[str, Any]] = None, timestamp: Optional[str] = None) -> None:
        if not self.enabled:
            return

        install_id = _get_or_create_install_id(self.cfg_path)
        payload = {
            "api_key": self.api_key,
            "distinct_id": install_id,
            "event": event,
            "properties": properties or {},
            "timestamp": timestamp or _now_iso(),
        }

        _run_in_background(_safe_post_json, self._endpoint(), payload)

    @contextmanager
    def stage(self, run_id: str, name: str, extra: Optional[Dict[str, Any]] = None):
        t0 = time.time()
        try:
            yield
            ok = True
            err_type = None
        except Exception as e:
            ok = False
            err_type = type(e).__name__
            raise
        finally:
            dt_ms = int((time.time() - t0) * 1000)
            props = {"run_id": run_id, "stage": name, "duration_ms": dt_ms, "success": ok}
            if err_type:
                props["error_type"] = err_type
            if extra:
                props.update(extra)
            self.capture("apimesh_stage_finished", props)
