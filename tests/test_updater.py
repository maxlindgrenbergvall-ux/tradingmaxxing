import io
import json
import zipfile

from bot import updater as upd


class Resp:
    def __init__(self, status=200, data=None, content=b""):
        self.status_code, self._data, self.content = status, data, content

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


def make_zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr("owner-repo-abc1234/" + name, data)
    return buf.getvalue()


def test_update_overwrites_code_but_keeps_state(tmp_path, monkeypatch):
    root = tmp_path / "app"
    (root / "bot").mkdir(parents=True)
    (root / "state").mkdir()
    (root / "bot" / "app.py").write_text("old")
    (root / "state" / "credentials.json").write_text("SECRET")
    zipdata = make_zip({
        "bot/app.py": "new",
        "web/index.html": "<html>",
        "state/credentials.json": "FROM-REPO",   # får aldrig skriva över användarens state
        "../evil.txt": "x",                       # får aldrig hamna utanför programmappen
    })
    commit = {"sha": "abc1234def", "commit": {"message": "Ny funktion\n\nmer", "committer": {"date": "2026-10-29T12:00:00Z"}}}

    def fake_get(url, headers=None, timeout=None):
        return Resp(content=zipdata) if "zipball" in url else Resp(data=commit)

    monkeypatch.setattr(upd.requests, "get", fake_get)
    u = upd.Updater(root, {"update": {"repo": "owner/repo"}}, root / "state")
    assert u.check()["available"] is True
    out = u.apply()
    assert out["files"] == 2
    assert (root / "bot" / "app.py").read_text() == "new"
    assert (root / "web" / "index.html").exists()
    assert (root / "state" / "credentials.json").read_text() == "SECRET"
    assert not (tmp_path / "evil.txt").exists()
    assert json.loads((root / "state" / "installed_version.json").read_text())["sha"] == "abc1234def"
    assert u.info()["available"] is False


def test_private_repo_asks_for_token(tmp_path, monkeypatch):
    monkeypatch.setattr(upd.requests, "get", lambda *a, **k: Resp(status=404))
    u = upd.Updater(tmp_path, {"update": {"repo": "owner/repo"}}, tmp_path)
    info = u.check()
    assert "token" in info["error"] and info["available"] is False
