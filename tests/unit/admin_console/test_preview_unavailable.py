from httpx import ASGITransport, AsyncClient
import pytest

from apps.admin_console import server


@pytest.fixture
def showcase(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<h1>Main SPA sentinel</h1>", encoding="utf-8")
    (dist / "main.js").write_text("main asset sentinel", encoding="utf-8")
    preview_asset = dist / "preview" / "pr" / "150" / "main.js"
    preview_asset.parent.mkdir(parents=True)
    preview_asset.write_text("wrong preview asset sentinel", encoding="utf-8")
    monkeypatch.setattr(server, "_get_showcase_dist", lambda: dist)
    monkeypatch.setattr(server, "_workspace_root", tmp_path)
    monkeypatch.setattr(server, "PREVIEW_PROFILE", False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "number"),
    [
        ("/preview/pr/150", "150"),
        ("/preview/pr/150/", "150"),
        ("/preview/pr/150/workspace", "150"),
        ("/preview/pr/150/main.js", "150"),
        ("/preview/pr/150/api/sessions", "150"),
        ("/preview/pr/7?head=unknown", "7"),
    ],
)
async def test_missing_preview_never_serves_main_spa(showcase, anonymous, path, number):
    response = await anonymous.get(path)

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"
    assert f"Preview for PR #{number} is not available" in response.text
    assert "No live preview is available at this URL." in response.text
    assert "sentinel" not in response.text
    assert "location" not in response.headers


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/", "/workspace", "/preview/pr/150extra"])
async def test_main_spa_still_serves_other_paths(showcase, anonymous, path):
    response = await anonymous.get(path)

    assert response.status_code == 200
    assert response.text == "<h1>Main SPA sentinel</h1>"


@pytest.mark.asyncio
async def test_main_static_assets_still_serve(showcase, anonymous):
    response = await anonymous.get("/main.js")

    assert response.status_code == 200
    assert response.text == "main asset sentinel"


@pytest.mark.asyncio
async def test_live_preview_root_path_still_serves_spa(showcase, anonymous, monkeypatch):
    monkeypatch.setattr(server, "PREVIEW_PROFILE", True)
    async with AsyncClient(
        transport=ASGITransport(app=server.app, root_path="/preview/pr/150"),
        base_url="http://localhost",
        headers=anonymous.headers,
    ) as client:
        response = await client.get("/preview/pr/150/workspace")

    assert response.status_code == 200
    assert response.text == "<h1>Main SPA sentinel</h1>"
