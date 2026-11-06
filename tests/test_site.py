"""The project website (site/, built by scripts/build_site.py for GitHub Pages)."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]

RELEASE = {
    "tag_name": "v1.2.3",
    "html_url": "https://github.com/bugraskl/eye-tracker/releases/tag/v1.2.3",
    "published_at": "2026-10-01T10:05:12Z",
    "assets": [
        {
            "name": f"EyeTracker-1.2.3-{suffix}",
            "size": size,
            "browser_download_url": f"https://dl/{suffix}",
        }
        for suffix, size in (
            ("windows-x64-setup.exe", 63_204_271),
            ("windows-x64-portable.zip", 89_695_115),
            ("macos-arm64.dmg", 88_420_854),
            ("linux-x86_64.AppImage", 132_553_208),
            ("linux-x86_64.tar.gz", 143_728_124),
        )
    ]
    + [
        {"name": "SHA256SUMS.txt", "size": 520, "browser_download_url": "https://dl/SHA256SUMS.txt"}
    ],
}


@pytest.fixture(scope="module")
def site() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_site", ROOT / "scripts" / "build_site.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def built(site: ModuleType, tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("site") / "_site"
    site.build(RELEASE, out)
    return out


def _ids(html: str) -> list[str]:
    return sorted(re.findall(r'\bid="([^"]+)"', html))


def test_both_languages_are_built(built: Path) -> None:
    english = (built / "index.html").read_text(encoding="utf-8")
    turkish = (built / "tr" / "index.html").read_text(encoding="utf-8")
    assert '<html lang="en">' in english
    assert '<html lang="tr">' in turkish
    assert (built / ".nojekyll").exists()


@pytest.mark.parametrize("page", ["index.html", "tr/index.html"])
def test_no_placeholder_survives(built: Path, page: str) -> None:
    assert "{{" not in (built / page).read_text(encoding="utf-8")


def test_languages_have_the_same_structure(built: Path) -> None:
    """Both pages carry the same sections, anchors and download cards."""
    english = (built / "index.html").read_text(encoding="utf-8")
    turkish = (built / "tr" / "index.html").read_text(encoding="utf-8")
    assert _ids(english) == _ids(turkish)
    for tag in ("<section", "<article", "<details", "data-download"):
        assert english.count(tag) == turkish.count(tag), tag


@pytest.mark.parametrize("page", ["index.html", "tr/index.html"])
def test_local_links_resolve(built: Path, page: str) -> None:
    html = (built / page).read_text(encoding="utf-8")
    folder = (built / page).parent
    missing = []
    for target in re.findall(r'(?:src|href)="([^"#]+)"', html):
        if "://" in target or target.startswith(("mailto:", "data:")):
            continue
        resolved = (folder / target).resolve()
        if resolved.is_dir():
            resolved = resolved / "index.html"
        if not resolved.exists():
            missing.append(target)
    assert missing == []


@pytest.mark.parametrize("page", ["index.html", "tr/index.html"])
def test_release_data_is_filled_in(built: Path, page: str) -> None:
    html = (built / page).read_text(encoding="utf-8")
    assert "1.2.3" in html
    for suffix in ("windows-x64-setup.exe", "macos-arm64.dmg", "linux-x86_64.AppImage"):
        assert f'href="https://dl/{suffix}"' in html
        assert f'href="https://dl/{suffix}"'.replace("href", "data-") not in html
    assert 'href="https://dl/SHA256SUMS.txt"' in html
    assert "63 MB" in html


def test_dates_are_localised(built: Path) -> None:
    assert "October 1, 2026" in (built / "index.html").read_text(encoding="utf-8")
    assert "1 Ekim 2026" in (built / "tr" / "index.html").read_text(encoding="utf-8")


def test_hero_demo_speaks_the_page_language(built: Path) -> None:
    """The display-arrangement demo's status messages are translated."""
    english = (built / "index.html").read_text(encoding="utf-8")
    turkish = (built / "tr" / "index.html").read_text(encoding="utf-8")
    for html in (english, turkish):
        for name in ("look", "away", "pick", "pane"):
            assert f'data-msg-{name}="' in html
    assert "Looking at display" in english
    assert "Looking at display" not in turkish
    assert "ekrana bakıyorsunuz" in turkish


def test_without_release_data_links_fall_back_to_the_releases_page(
    site: ModuleType, tmp_path: Path
) -> None:
    site.build({}, tmp_path / "_site")
    html = (tmp_path / "_site" / "index.html").read_text(encoding="utf-8")
    assert "{{" not in html
    assert f'href="{site.RELEASES_URL}"' in html


def test_site_makes_no_third_party_requests(built: Path) -> None:
    """Like the app: no web fonts, trackers or scripts loaded from other sites."""
    loaded = re.compile(
        r'<(?:script[^>]*\ssrc|img[^>]*\ssrc|link[^>]*rel="(?:stylesheet|icon|apple-touch-icon|preload)"'
        r'[^>]*\shref)="([^"]+)"'
    )
    for page in ("index.html", "tr/index.html"):
        html = (built / page).read_text(encoding="utf-8")
        resources = loaded.findall(html)
        assert resources
        assert [url for url in resources if "://" in url] == []
    css = (built / "assets" / "style.css").read_text(encoding="utf-8")
    assert "@import" not in css
    assert "url(http" not in css


@pytest.mark.parametrize(
    ("size", "text"), [(0, ""), (520, "1 kB"), (63_204_271, "63 MB"), (143_728_124, "144 MB")]
)
def test_human_size(site: ModuleType, size: int, text: str) -> None:
    assert site.human_size(size) == text


def test_unknown_placeholder_is_an_error(site: ModuleType) -> None:
    with pytest.raises(SystemExit):
        site.render("{{NOPE}}", {}, Path("x.html"))


# ---------------------------------------------------------------- output safety
def test_refuses_to_empty_a_user_folder(site: ModuleType, tmp_path: Path) -> None:
    """TD-SEC-014: a typo in --out must not delete a user folder."""
    user = tmp_path / "Documents"
    user.mkdir()
    (user / "thesis.txt").write_text("keep me", encoding="utf-8")
    with pytest.raises(SystemExit, match="refusing to delete"):
        site.build({}, user)
    assert (user / "thesis.txt").read_text(encoding="utf-8") == "keep me"


def test_empties_a_new_empty_or_build_named_folder(site: ModuleType, tmp_path: Path) -> None:
    empty = tmp_path / "empty-folder"
    empty.mkdir()
    site.build({}, empty)  # an empty folder has nothing to lose
    assert (empty / "index.html").is_file()

    out = tmp_path / "_site"
    out.mkdir()
    (out / "stale.html").write_text("old", encoding="utf-8")
    site.build({}, out)  # a build output may be rebuilt in place
    assert not (out / "stale.html").exists()
    assert (out / "index.html").is_file()


def test_refuses_the_repository_and_the_site_sources(site: ModuleType) -> None:
    for target in (site.ROOT, site.SITE):
        with pytest.raises(SystemExit, match="refusing"):
            site.build({}, target)


_GITHUB_REPO_LINK = re.compile(
    r"https://github\.com/bugraskl/eye-tracker/(?:blob|tree)/main/([^\"#?]+)"
)


def _missing_github_targets(text: str) -> list[str]:
    """Repository paths behind absolute GitHub links in a site page."""
    return [path for path in _GITHUB_REPO_LINK.findall(text) if not (ROOT / path).exists()]


@pytest.mark.parametrize("page", ["site/index.html", "site/tr/index.html"])
def test_absolute_github_links_point_at_repo_files(page: str) -> None:
    """The pages link to docs with absolute blob/main URLs; a docs move must not break them."""
    text = (ROOT / page).read_text(encoding="utf-8")
    assert _missing_github_targets(text) == []


def test_github_link_check_detects_a_moved_file() -> None:
    assert _missing_github_targets(
        "https://github.com/bugraskl/eye-tracker/blob/main/docs/nope.md"
    ) == ["docs/nope.md"]
