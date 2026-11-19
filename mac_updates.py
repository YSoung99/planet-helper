"""Check the upstream release and stage a verified macOS application."""
import hashlib
import json
import plistlib
import re
import subprocess
import ssl
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import certifi

REPO = 'YSoung99/planet-helper'
ASSET = 'planet-helper-macos-arm64.zip'
BUNDLE_ID = 'local.codex.planethelper.autotrial'


@dataclass(frozen=True)
class Release:
    tag: str
    url: str
    sha256: str
    size: int


def version(tag):
    match = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)', tag)
    return tuple(map(int, match.groups())) if match else ()


def app_version(app):
    with (app / 'Contents' / 'Info.plist').open('rb') as stream:
        return plistlib.load(stream)['CFBundleShortVersionString']


def latest(current, asset_name=ASSET):
    request = Request(f'https://api.github.com/repos/{REPO}/releases/latest',
                      headers={'Accept': 'application/vnd.github+json'})
    try:
        with urlopen(request, timeout=10, context=ssl.create_default_context(cafile=certifi.where())) as response:
            release = json.load(response)
    except HTTPError as exc:
        if exc.code == 404:
            return None, 'GitHub에 공개된 릴리스가 없습니다.'
        raise
    tag = release['tag_name']
    if not version(tag) or version(tag) <= version(current):
        return None, '설치할 새 버전이 없습니다.'
    for asset in release['assets']:
        digest = asset.get('digest') or ''
        url = f'https://github.com/{REPO}/releases/download/{tag}/{asset_name}'
        if (asset['name'] == asset_name and asset['browser_download_url'] == url
                and re.fullmatch(r'sha256:[a-f0-9]{64}', digest)
                and 0 < asset['size'] <= 1024**3):
            return Release(tag, url, digest[7:], asset['size']), f'{tag} 업데이트 가능'
    return None, f'{tag} 릴리스에 검증 가능한 설치 파일이 없습니다.'


def verify(app, expected_version=None):
    if app.is_symlink() or not app.is_dir():
        raise ValueError('앱 번들을 찾을 수 없습니다.')
    with (app / 'Contents' / 'Info.plist').open('rb') as stream:
        info = plistlib.load(stream)
    if info.get('CFBundleIdentifier') != BUNDLE_ID or info.get('CFBundleExecutable') != 'PlanetHelper':
        raise ValueError('플래닛 도우미 앱을 확인할 수 없습니다.')
    if expected_version and version(info['CFBundleShortVersionString']) != version(expected_version):
        raise ValueError('릴리스와 앱의 버전이 일치하지 않습니다.')
    subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(app)],
                   check=True, capture_output=True, timeout=60)


def download_archive(release, archive):
    digest = hashlib.sha256()
    size = 0
    with urlopen(release.url, timeout=20, context=ssl.create_default_context(cafile=certifi.where())) as response, archive.open('wb') as stream:
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            if size > release.size:
                raise ValueError('다운로드 크기가 릴리스 정보와 일치하지 않습니다.')
            digest.update(chunk)
            stream.write(chunk)
    if size != release.size or digest.hexdigest() != release.sha256:
        raise ValueError('다운로드 파일의 SHA-256이 일치하지 않습니다.')


def download(release, stage):
    archive = stage / ASSET
    download_archive(release, archive)
    subprocess.run(['/usr/bin/ditto', '-x', '-k', str(archive), str(stage)],
                   check=True, capture_output=True, timeout=180)
    apps = [path for path in stage.iterdir() if path.suffix == '.app']
    if len(apps) != 1:
        raise ValueError('설치 파일에서 앱을 하나만 찾아야 합니다.')
    candidate = apps[0]
    verify(candidate, release.tag)
    archive.unlink()
    return candidate


def prepare(app):
    return tempfile.TemporaryDirectory(delete=False, prefix='.planet-helper-update-', dir=app.parent)


def app_name(app_version):
    return f'플래닛 도우미 {app_version}.app'


def previous(app):
    # One backup slot, whatever the versioned name of the running app is.
    return app.with_name('.플래닛 도우미.previous.app')
