"""업데이트 확인: GitHub Releases의 최신 릴리스 태그(예: v1.2.0)와 현재 버전을 비교한다.

새 버전을 배포하는 방법
  1. myzip/__init__.py 의 __version__ 을 올리고 build.bat 으로 exe 빌드
  2. GitHub 저장소에서 Releases → Draft a new release → 태그를 v1.2.0 처럼 입력
  3. dist 폴더의 MyZip.exe 를 첨부하고 Publish
"""
import json
import re
import urllib.error
import urllib.request

from . import __version__

# 릴리스를 올릴 GitHub 저장소 "사용자이름/저장소이름". 비어 있으면 업데이트 확인을 하지 않는다.
GITHUB_REPO = "a753df-lang/MyZip"


class UpdateError(Exception):
    pass


def parse_version(text: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", text)
    if not nums:
        raise UpdateError(f"버전 형식을 알 수 없습니다: {text}")
    return tuple(int(n) for n in nums[:3])


def is_configured() -> bool:
    return bool(GITHUB_REPO.strip())


def latest_release(timeout: float = 5, api_url: str | None = None) -> dict:
    """최신 릴리스 정보 {"version", "url", "notes", "download"} 반환."""
    if api_url is None:
        if not is_configured():
            raise UpdateError("업데이트 서버(GitHub 저장소)가 아직 설정되지 않았습니다.")
        api_url = f"https://api.github.com/repos/{GITHUB_REPO.strip()}/releases/latest"
    req = urllib.request.Request(api_url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"MyZip/{__version__}",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise UpdateError("아직 배포된 릴리스가 없습니다.") from e
        raise UpdateError(f"업데이트 서버 오류 (HTTP {e.code})") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise UpdateError("업데이트 서버에 연결할 수 없습니다. 인터넷 연결을 확인하세요.") from e
    except ValueError as e:
        raise UpdateError("업데이트 정보를 읽을 수 없습니다.") from e

    tag = data.get("tag_name") or ""
    download = next((a.get("browser_download_url") for a in data.get("assets", [])
                     if str(a.get("name", "")).lower().endswith(".exe")), None)
    return {
        "version": tag.lstrip("vV"),
        "url": data.get("html_url") or "",
        "notes": (data.get("body") or "").strip(),
        "download": download,
    }


def check(timeout: float = 5, api_url: str | None = None) -> dict | None:
    """새 버전이 있으면 릴리스 정보, 최신이면 None."""
    info = latest_release(timeout, api_url)
    if parse_version(info["version"]) > parse_version(__version__):
        return info
    return None
