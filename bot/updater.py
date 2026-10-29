"""Uppdaterar programmet från GitHub med ett klick.

Hämtar senaste versionen av repot som zip, skriver över programfilerna och
lämnar `state/` (nycklar, historik, nödbroms-status) och `.venv/` orörda.
Därefter avslutas appen med RESTART_CODE så att startfilen startar om den.
"""

from __future__ import annotations

import io
import json
import logging
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

RESTART_CODE = 10
KEEP = {"state", ".venv", ".git", "__pycache__"}
TIMEOUT = 20

log = logging.getLogger("bot")


class Updater:
    def __init__(self, root: Path, cfg: dict[str, Any], state_dir: Path):
        u = cfg.get("update", {})
        self.repo: str = u.get("repo", "")
        self.branch: str = u.get("branch", "main")
        self.root = root
        self.version_path = state_dir / "installed_version.json"
        self.token_path = state_dir / "github_token.txt"
        self.latest: dict | None = None
        self.checked_at: str | None = None
        self.error: str | None = None

    # ---------- hjälp ----------
    @property
    def enabled(self) -> bool:
        return bool(self.repo)

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/vnd.github+json", "User-Agent": "momentum-bot-updater"}
        if self.token_path.exists():
            tok = self.token_path.read_text().strip()
            if tok:
                h["Authorization"] = f"Bearer {tok}"
        return h

    def installed(self) -> dict:
        if self.version_path.exists():
            try:
                return json.loads(self.version_path.read_text())
            except json.JSONDecodeError:
                pass
        # Zip-filen man laddade ner första gången har sin version i installed_from.txt
        origin = self.root / "installed_from.txt"
        if origin.exists():
            sha = origin.read_text().strip()
            if sha:
                return {"sha": sha, "installed_at": None}
        return {}

    def set_token(self, token: str) -> None:
        self.token_path.write_text(token.strip())

    # ---------- kontroll ----------
    def check(self) -> dict:
        if not self.enabled:
            return self.info()
        try:
            r = requests.get(f"https://api.github.com/repos/{self.repo}/commits/{self.branch}",
                             headers=self._headers(), timeout=TIMEOUT)
            if r.status_code == 404:
                raise RuntimeError("Hittar inte repot – är det privat? Lägg i så fall in en GitHub-token.")
            r.raise_for_status()
            c = r.json()
            self.latest = {
                "sha": c["sha"],
                "message": c["commit"]["message"].split("\n")[0],
                "date": c["commit"]["committer"]["date"],
            }
            self.error = None
        except Exception as e:
            self.error = str(e) if isinstance(e, RuntimeError) else "Kunde inte kontrollera uppdateringar."
            log.warning("Uppdateringskontroll misslyckades: %s", e)
        self.checked_at = datetime.now(timezone.utc).isoformat()
        return self.info()

    def info(self) -> dict:
        inst = self.installed()
        available = bool(self.latest and self.latest["sha"] != inst.get("sha"))
        return {
            "enabled": self.enabled,
            "repo": self.repo,
            "installed": inst,
            "latest": self.latest,
            "available": available,
            "checked_at": self.checked_at,
            "error": self.error,
        }

    # ---------- installation ----------
    def apply(self) -> dict:
        if not self.enabled:
            raise RuntimeError("Ingen uppdateringskälla är inställd.")
        info = self.check()
        if not info["latest"]:
            raise RuntimeError(self.error or "Kunde inte nå GitHub.")
        sha = info["latest"]["sha"]
        r = requests.get(f"https://api.github.com/repos/{self.repo}/zipball/{sha}",
                         headers=self._headers(), timeout=120)
        r.raise_for_status()
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        names = zf.namelist()
        prefix = names[0].split("/")[0] + "/"
        if not any(n == prefix + "bot/app.py" for n in names):
            raise RuntimeError("Nedladdningen ser inte ut som Momentum Bot – avbryter.")

        written = 0
        for name in names:
            rel = name[len(prefix):]
            if not rel or name.endswith("/"):
                continue
            top = rel.split("/")[0]
            if top in KEEP or "__pycache__" in rel:
                continue
            dest = (self.root / rel).resolve()
            if self.root.resolve() not in dest.parents:
                continue  # skydd mot sökvägar utanför programmappen
            data = zf.read(name)
            if dest.exists() and dest.read_bytes() == data:
                continue  # oförändrad (skriv inte över startfilen i onödan medan den körs)
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".new")
            tmp.write_bytes(data)
            if rel.endswith((".command", ".sh")):
                tmp.chmod(0o755)
            tmp.replace(dest)
            written += 1

        for d in self.root.rglob("__pycache__"):
            if ".venv" not in d.parts:
                shutil.rmtree(d, ignore_errors=True)
        self.version_path.write_text(json.dumps({**info["latest"], "installed_at": datetime.now(timezone.utc).isoformat()}))
        log.info("Uppdaterad till %s (%d filer ändrade).", sha[:7], written)
        return {"ok": True, "sha": sha, "files": written}
