"""MyZip 압축 엔진: ZIP / TAR / TAR.GZ / TAR.BZ2 / TAR.XZ 생성, 목록, 해제, 테스트."""
import os
import tarfile
import zipfile
from dataclasses import dataclass
from datetime import datetime

from . import fastzip, splitzip

# 포맷 이름 -> 파일 확장자
FORMATS = {
    "zip": ".zip",
    "zip (bzip2)": ".zip",
    "zip (lzma)": ".zip",
    "tar": ".tar",
    "tar.gz": ".tar.gz",
    "tar.bz2": ".tar.bz2",
    "tar.xz": ".tar.xz",
}

ZIP_METHODS = {
    "zip": zipfile.ZIP_DEFLATED,
    "zip (bzip2)": zipfile.ZIP_BZIP2,
    "zip (lzma)": zipfile.ZIP_LZMA,
}

TAR_MODES = {"tar": "w", "tar.gz": "w:gz", "tar.bz2": "w:bz2", "tar.xz": "w:xz"}


class ArchiveError(Exception):
    pass


@dataclass
class Entry:
    name: str
    size: int
    packed: int
    modified: datetime | None
    is_dir: bool

    @property
    def ratio(self) -> float:
        return 0.0 if self.size == 0 else 100.0 * (1 - self.packed / self.size)


def detect_kind(path: str) -> str:
    try:
        if splitzip.open_split(path):
            return "zip"
    except (splitzip.SplitError, OSError) as e:
        raise ArchiveError(str(e)) from e
    try:
        if zipfile.is_zipfile(path):
            return "zip"
    except zipfile.BadZipFile as e:
        # 분할 압축의 마지막 조각(.zip)만 있을 때 is_zipfile은 False 대신 예외를 던진다
        if "multiple disks" in str(e):
            raise ArchiveError(
                f"분할 압축의 나머지 조각(.z01, .z02 …)을 같은 폴더에서 찾을 수 없습니다: "
                f"{os.path.basename(path)}") from e
        raise ArchiveError(f"손상된 압축 파일입니다: {os.path.basename(path)}") from e
    if tarfile.is_tarfile(path):
        return "tar"
    raise ArchiveError(f"지원하지 않는 압축 형식입니다: {os.path.basename(path)}")


def list_entries(path: str) -> list[Entry]:
    kind = detect_kind(path)
    entries = []
    if kind == "zip":
        with fastzip.open_zip(path) as zf:
            for i in fastzip.infolist(zf):
                entries.append(Entry(i.filename, i.file_size, i.compress_size,
                                     datetime(*i.date_time), i.is_dir()))
    else:
        with tarfile.open(path) as tf:
            for m in tf.getmembers():
                # tar는 개별 압축 크기가 없으므로 원본 크기를 그대로 표시
                entries.append(Entry(m.name + ("/" if m.isdir() else ""), m.size, m.size,
                                     datetime.fromtimestamp(m.mtime), m.isdir()))
    return entries


def _safe_target(dest: str, name: str) -> str:
    """Zip-Slip 방지: 압축 해제 경로가 dest 밖으로 나가지 않는지 확인."""
    dest_abs = os.path.abspath(dest)
    target = os.path.abspath(os.path.join(dest_abs, name))
    if os.path.commonpath([dest_abs, target]) != dest_abs:
        raise ArchiveError(f"위험한 경로가 포함되어 있습니다: {name}")
    return target


def extract(path: str, dest: str, members: list[str] | None = None, progress=None) -> int:
    """압축 해제. members가 None이면 전체. progress: myzip.progress.Progress. 해제한 항목 수 반환."""
    os.makedirs(dest, exist_ok=True)
    kind = detect_kind(path)
    wanted = set(members) if members else None
    count = 0
    if kind == "zip":
        with fastzip.open_zip(path) as zf:
            infos = [i for i in fastzip.infolist(zf) if wanted is None or _selected(i.filename, wanted)]
        count = fastzip.extract_zip(path, dest, infos, _safe_target, progress)
    else:
        with tarfile.open(path) as tf:
            mems = [m for m in tf.getmembers()
                    if wanted is None or _selected(m.name + ("/" if m.isdir() else ""), wanted)]
            if progress:
                progress.begin(sum(m.size for m in mems if m.isfile()), len(mems))
            for m in mems:
                _safe_target(dest, m.name)
                tf.extract(m, dest, filter="data")
                count += 1
                if progress:
                    progress.advance(m.size if m.isfile() else 0)
                    progress.item_done(m.name)
    return count


def single_root_folder(path: str) -> str | None:
    """모든 항목이 최상위 폴더 하나 안에 있으면 그 폴더 이름, 아니면 None."""
    roots = set()
    for e in list_entries(path):
        name = e.name.lstrip("/")
        top, sep, _ = name.partition("/")
        if not sep:  # 최상위에 파일이 있음
            return None
        roots.add(top)
        if len(roots) > 1:
            return None
    return roots.pop() if roots else None


def name_folder_dest(path: str, parent: str | None = None) -> str:
    """parent 안에 '압축 파일 이름' 폴더를 만들어 푸는 대상 경로. parent 기본값은 압축 파일이 있는 폴더.

    압축 파일 안에 최상위 폴더가 하나뿐이면 그 폴더가 이미 이름 폴더 역할을 하므로
    parent에 바로 푼다 (폴더가 이중으로 생기지 않게).
    """
    parent = os.path.abspath(parent) if parent else os.path.dirname(os.path.abspath(path))
    if single_root_folder(path):
        return parent
    base = splitzip.stem_name(os.path.basename(path))  # 사과.z01 → 사과, 사과.zip.001 → 사과.zip
    for ext in (".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".zip", ".tar"):
        if base.lower().endswith(ext):
            base = base[: -len(ext)]
            break
    return os.path.join(parent, base)


def conflicts(path: str, dest: str, members: list[str] | None = None) -> list[str]:
    """풀었을 때 dest에 이미 존재하는 파일 이름 목록 (덮어쓰기 확인용)."""
    wanted = set(members) if members else None
    found = []
    for e in list_entries(path):
        if e.is_dir or (wanted is not None and not _selected(e.name, wanted)):
            continue
        if os.path.isfile(_safe_target(dest, e.name)):
            found.append(e.name)
    return found


def _selected(name: str, wanted: set[str]) -> bool:
    # 폴더를 선택하면 그 안의 파일도 함께 해제
    return name in wanted or any(w.endswith("/") and name.startswith(w) for w in wanted)


def _walk_sources(sources: list[str]):
    """(실제 경로, 압축 내부 이름) 목록 생성. 폴더는 재귀적으로 포함."""
    for src in sources:
        src = os.path.abspath(src)
        base = os.path.dirname(src)
        if os.path.isdir(src):
            yield src, os.path.relpath(src, base).replace(os.sep, "/") + "/"
            for root, dirs, files in os.walk(src):
                dirs.sort()
                for d in dirs:
                    full = os.path.join(root, d)
                    yield full, os.path.relpath(full, base).replace(os.sep, "/") + "/"
                for f in sorted(files):
                    full = os.path.join(root, f)
                    yield full, os.path.relpath(full, base).replace(os.sep, "/")
        else:
            yield src, os.path.basename(src)


def create(sources: list[str], out_path: str, fmt: str = "zip", level: int = 6, progress=None) -> int:
    """압축 파일 생성. level: 0(저장)~9(최고). progress: myzip.progress.Progress. 추가한 항목 수 반환.

    취소하거나 오류가 나면 만들다 만 압축 파일은 지운다."""
    if fmt not in FORMATS:
        raise ArchiveError(f"알 수 없는 형식: {fmt}")
    items = list(_walk_sources(sources))
    out_abs = os.path.abspath(out_path)
    items = [(p, a) for p, a in items if os.path.abspath(p) != out_abs]
    if progress:
        progress.begin(sum(os.path.getsize(p) for p, a in items if not a.endswith("/")), len(items))
    try:
        return _create(items, out_path, fmt, level, progress)
    except BaseException:
        try:
            os.remove(out_path)
        except OSError:
            pass
        raise


def _create(items, out_path, fmt, level, progress):
    def done(path, arc):
        if progress:
            progress.advance(0 if arc.endswith("/") else os.path.getsize(path))
            progress.item_done(arc)

    if fmt == "zip":
        # 기본 ZIP(Deflate)은 멀티코어 엔진 사용
        return fastzip.create_zip(items, out_path, level, progress)
    if fmt in ZIP_METHODS:
        method = ZIP_METHODS[fmt] if level > 0 else zipfile.ZIP_STORED
        kw = {}
        if method in (zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2):
            kw["compresslevel"] = max(1, level) if method == zipfile.ZIP_BZIP2 else level
        with zipfile.ZipFile(out_path, "w", compression=method, **kw) as zf:
            for path, arc in items:
                zf.write(path, arc)
                done(path, arc)
    else:
        mode = TAR_MODES[fmt]
        kw = {}
        if fmt in ("tar.gz", "tar.bz2"):
            kw["compresslevel"] = max(1, level)
        elif fmt == "tar.xz":
            kw["preset"] = min(level, 9)
        with tarfile.open(out_path, mode, **kw) as tf:
            for path, arc in items:
                tf.add(path, arc.rstrip("/"), recursive=False)
                done(path, arc)
    return len(items)


def test(path: str, progress=None) -> list[str]:
    """무결성 검사. 손상된 항목 이름 목록 반환 (빈 목록이면 정상)."""
    bad = []
    kind = detect_kind(path)
    if kind == "zip":
        bad = fastzip.test_zip(path, progress)
    else:
        try:
            with tarfile.open(path) as tf:
                mems = tf.getmembers()
                if progress:
                    progress.begin(sum(m.size for m in mems if m.isfile()), len(mems))
                for m in mems:
                    if m.isfile():
                        f = tf.extractfile(m)
                        while buf := f.read(1 << 20):
                            if progress:
                                progress.advance(len(buf))
                    if progress:
                        progress.item_done(m.name)
        except (tarfile.TarError, OSError, EOFError) as e:
            bad.append(str(e))
    return bad


def archive_size(path: str) -> int:
    """압축 파일 크기. 분할 압축이면 모든 조각의 합."""
    try:
        return splitzip.total_size(path)
    except splitzip.SplitError:
        return os.path.getsize(path)


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024
