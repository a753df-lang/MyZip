"""멀티코어 ZIP 엔진.

압축: 파일을 4MB 조각으로 나눠 모든 CPU 코어에서 동시에 Deflate 하고(pigz 방식),
      앞 조각의 끝 32KB를 사전(zdict)으로 넘겨 압축률 손실을 줄인다.
      이미 압축된 파일(jpg, mp4, zip 등)은 다시 압축하지 않고 그대로 저장한다.
해제/테스트: 항목마다 별도 스레드에서 동시에 처리한다. (zlib은 GIL을 놓으므로 실제로 병렬 실행됨)
"""
import os
import struct
import threading
import time
import zipfile
import zlib
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import splitzip

CHUNK = 4 << 20
SMALL = 1 << 20  # 이 크기 이하 파일은 읽기까지 워커 스레드에서 통째로 처리
WORKERS = os.cpu_count() or 4
ZIP64_THRESHOLD = 0xF0000000
U32 = 0xFFFFFFFF

# 다시 압축해도 거의 줄지 않는 확장자 → 저장(STORED)으로 시간 절약
STORE_EXT = {
    ".zip", ".7z", ".rar", ".gz", ".tgz", ".bz2", ".xz", ".zst", ".lz4", ".cab", ".jar", ".apk",
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".avif",
    ".mp3", ".aac", ".m4a", ".ogg", ".opus", ".flac",
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm", ".m4v",
    ".docx", ".xlsx", ".pptx", ".hwpx", ".pdf", ".epub",
}


def _dos_datetime(ts):
    t = time.localtime(ts)
    if t.tm_year < 1980:
        return 0, (1 << 5) | 1
    return ((t.tm_hour << 11) | (t.tm_min << 5) | (t.tm_sec // 2),
            ((t.tm_year - 1980) << 9) | (t.tm_mon << 5) | t.tm_mday)


def _deflate(data, level, zdict, last):
    c = zlib.compressobj(level, zlib.DEFLATED, -15, 9, zlib.Z_DEFAULT_STRATEGY, zdict) if zdict \
        else zlib.compressobj(level, zlib.DEFLATED, -15, 9)
    return c.compress(data) + c.flush(zlib.Z_FINISH if last else zlib.Z_SYNC_FLUSH)


def _pack_small(path, level, store):
    """작은 파일 하나를 통째로 읽고 CRC 계산 + 압축 (워커 스레드에서 실행)."""
    with open(path, "rb", buffering=0) as f:
        raw = f.read()
    crc = zlib.crc32(raw)
    if not store and raw:
        packed = _deflate(raw, level, None, True)
        if len(packed) < len(raw):
            return zipfile.ZIP_DEFLATED, packed, crc, len(raw)
    # 압축해도 줄지 않으면 그대로 저장
    return zipfile.ZIP_STORED, raw, crc, len(raw)


class _Entry:
    __slots__ = ("name", "method", "time", "date", "crc", "csize", "usize",
                 "offset", "zip64", "is_dir")

    def __init__(self, name, method, mtime, usize, is_dir):
        self.name = name.encode("utf-8")
        self.method = method
        self.time, self.date = _dos_datetime(mtime)
        self.crc = 0
        self.csize = 0
        self.usize = usize
        self.offset = 0
        self.zip64 = usize >= ZIP64_THRESHOLD
        self.is_dir = is_dir


def create_zip(items, out_path, level=6, progress=None):
    """items: [(실제 경로, 압축 내부 이름)] — 폴더는 이름이 '/'로 끝남.
    progress: myzip.progress.Progress (begin은 호출하는 쪽에서 한다)."""
    entries = []
    with open(out_path, "wb", buffering=1 << 20) as out, ThreadPoolExecutor(WORKERS) as pool:
        try:
            _write_entries(items, level, progress, out, pool, entries)
        except BaseException:
            pool.shutdown(wait=True, cancel_futures=True)  # 취소하면 남은 압축 작업은 버린다
            raise
        _write_central(out, entries)
    return len(items)


def _write_entries(items, level, progress, out, pool, entries):
    # 순서대로 쓸 작업: ("hdr", e) / ("small", e, future) / ("data", e, future|bytes, 원본 크기) / ("end", e, arc)
    pending = deque()
    window = WORKERS * 8

    def flush(limit):
        while len(pending) > limit:
            job = pending.popleft()
            kind, e = job[0], job[1]
            if kind == "hdr":
                e.offset = out.tell()
                out.write(_local_header(e))
            elif kind == "small":  # 작은 파일: 결과를 받은 뒤 방식/CRC가 확정됨
                e.method, data, e.crc, e.usize = job[2].result()
                e.offset = out.tell()
                out.write(_local_header(e))
                out.write(data)
                e.csize = len(data)
                if progress:
                    progress.advance(e.usize)
            elif kind == "data":
                data = job[2] if isinstance(job[2], bytes) else job[2].result()
                out.write(data)
                e.csize += len(data)
                if progress:
                    progress.advance(job[3])
            else:  # "end": 헤더의 CRC/크기를 실제 값으로 고쳐 쓴다
                if not e.zip64 and e.csize > U32:
                    raise OSError(f"압축 결과가 너무 큽니다: {job[2]}")
                end = out.tell()
                out.seek(e.offset + 14)
                if e.zip64:
                    out.write(struct.pack("<I", e.crc))
                    out.seek(e.offset + 30 + len(e.name) + 4)
                    out.write(struct.pack("<QQ", e.usize, e.csize))
                else:
                    out.write(struct.pack("<III", e.crc, e.csize, e.usize))
                out.seek(end)
                if progress:
                    progress.item_done(job[2])

    for path, arc in items:
        st = os.stat(path)
        is_dir = arc.endswith("/")
        usize = 0 if is_dir else st.st_size
        store = is_dir or level == 0 or os.path.splitext(arc)[1].lower() in STORE_EXT
        e = _Entry(arc, zipfile.ZIP_STORED if store else zipfile.ZIP_DEFLATED,
                   st.st_mtime, usize, is_dir)
        entries.append(e)
        small = not is_dir and usize <= SMALL
        if small:
            pending.append(("small", e, pool.submit(_pack_small, path, level, store)))
        else:
            pending.append(("hdr", e))
        if not is_dir and not small:
            crc = 0
            read = 0
            prev_tail = None
            with open(path, "rb", buffering=0) as f:
                chunk = f.read(CHUNK)
                while True:
                    nxt = f.read(CHUNK) if len(chunk) == CHUNK else b""
                    last = not nxt
                    crc = zlib.crc32(chunk, crc)
                    read += len(chunk)
                    if store:
                        pending.append(("data", e, chunk, len(chunk)))
                    else:
                        pending.append(("data", e, pool.submit(_deflate, chunk, level, prev_tail, last),
                                        len(chunk)))
                        prev_tail = chunk[-32768:] if chunk else None
                    flush(window)
                    if last:
                        break
                    chunk = nxt
            e.crc = crc & U32
            # 압축 도중 파일 크기가 바뀌었을 수 있으므로 실제 읽은 크기를 기록
            if read >= ZIP64_THRESHOLD and not e.zip64:
                raise OSError(f"압축 중에 파일이 커졌습니다: {arc}")
            e.usize = read
        pending.append(("end", e, arc))
        flush(window)
    flush(0)


def _local_header(e):
    extra = struct.pack("<HHQQ", 1, 16, e.usize, 0) if e.zip64 else b""
    sizes = (U32, U32) if e.zip64 else (0, 0)
    return struct.pack("<IHHHHHIIIHH", 0x04034B50, 45 if e.zip64 else 20, 0x800, e.method,
                       e.time, e.date, 0, sizes[0], sizes[1], len(e.name), len(extra)) + e.name + extra


def _write_central(out, entries):
    cd_start = out.tell()
    for e in entries:
        big = e.zip64 or e.offset >= U32 or e.csize >= U32
        extra = struct.pack("<HHQQQ", 1, 24, e.usize, e.csize, e.offset) if big else b""
        out.write(struct.pack(
            "<IHHHHHHIIIHHHHHII", 0x02014B50, 45 if big else 20, 45 if big else 20, 0x800, e.method,
            e.time, e.date, e.crc, U32 if big else e.csize, U32 if big else e.usize,
            len(e.name), len(extra), 0, 0, 0, 0x10 if e.is_dir else 0x20,
            U32 if big else e.offset) + e.name + extra)
    cd_end = out.tell()
    cd_size = cd_end - cd_start
    n = len(entries)
    if n >= 0xFFFF or cd_start >= U32 or cd_size >= U32:
        out.write(struct.pack("<IQHHIIQQQQ", 0x06064B50, 44, 45, 45, 0, 0, n, n, cd_size, cd_start))
        out.write(struct.pack("<IIQI", 0x07064B50, 0, cd_end, 1))
        out.write(struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, 0xFFFF, 0xFFFF, U32, U32, 0))
    else:
        out.write(struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, n, n, cd_size, cd_start, 0))


def infolist(zf):
    """UTF-8 표시가 없는 이름은 cp437로 읽혀 한글이 깨지므로 UTF-8 → CP949 순으로 다시 해석."""
    infos = zf.infolist()
    for i in infos:
        if not i.flag_bits & 0x800:
            try:
                raw = i.filename.encode("cp437")
            except UnicodeEncodeError:
                continue
            try:
                i.filename = raw.decode("utf-8")
            except UnicodeDecodeError:
                i.filename = raw.decode("cp949", errors="replace")
    return infos


def open_zip(path):
    """읽기용 ZipFile. 분할 압축(.z01 … / .zip.001 …)이면 조각을 이어 붙인 가상 파일로 연다."""
    split = splitzip.open_split(path)
    if split is None:
        return zipfile.ZipFile(path)
    zf = zipfile.ZipFile(split())
    zf._filePassed = 0  # zf.close()가 가상 파일도 닫도록
    return zf


class _Readers:
    """스레드마다 파일 핸들을 따로 연다 (하나를 공유하면 seek가 꼬임)."""

    def __init__(self, path):
        self.path = path
        split = splitzip.open_split(path)
        self.open_raw = split or (lambda: open(path, "rb", buffering=0))
        self.local = threading.local()
        self.all = []
        self.lock = threading.Lock()

    def _get(self, attr, opener):
        h = getattr(self.local, attr, None)
        if h is None:
            h = opener()
            setattr(self.local, attr, h)
            with self.lock:
                self.all.append(h)
        return h

    def zip(self):
        return self._get("zf", lambda: open_zip(self.path))

    def raw(self):
        return self._get("raw", self.open_raw)

    def chunks(self, info):
        """항목의 압축 해제된 데이터를 조각으로 내보낸다. CRC가 틀리면 BadZipFile."""
        if info.flag_bits & 1 or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            # 암호화 / bzip2 / lzma 는 표준 zipfile로 처리
            with self.zip().open(info) as f:
                while True:
                    buf = f.read(CHUNK)
                    if not buf:
                        return
                    yield buf
        # Deflate/저장 항목은 zipfile을 거치지 않고 직접 읽어 오버헤드를 줄인다
        f = self.raw()
        f.seek(info.header_offset)
        head = f.read(30)
        if len(head) != 30 or head[:4] != b"PK\x03\x04":
            raise zipfile.BadZipFile(f"잘못된 로컬 헤더: {info.filename}")
        name_len, extra_len = struct.unpack("<HH", head[26:30])
        f.seek(info.header_offset + 30 + name_len + extra_len)
        left = info.compress_size
        crc = 0
        d = zlib.decompressobj(-15) if info.compress_type == zipfile.ZIP_DEFLATED else None
        while left:
            raw = f.read(min(left, CHUNK))
            if not raw:
                raise zipfile.BadZipFile(f"파일이 잘렸습니다: {info.filename}")
            left -= len(raw)
            if not d:
                crc = zlib.crc32(raw, crc)
                yield raw
                continue
            # 잘 압축된 데이터는 4MB가 수백 MB로 풀릴 수 있으므로 한 번에 CHUNK씩만 꺼낸다
            buf = raw
            while True:
                out = d.decompress(buf, CHUNK)
                buf = d.unconsumed_tail
                if out:
                    crc = zlib.crc32(out, crc)
                    yield out
                if not buf and len(out) < CHUNK:
                    break
        if d:
            tail = d.flush()
            if tail:
                crc = zlib.crc32(tail, crc)
                yield tail
        if crc != info.CRC:
            raise zipfile.BadZipFile(f"CRC 오류: {info.filename}")

    def close(self):
        for h in self.all:
            h.close()


def _run_all(work, items, on_result):
    """items를 모든 코어에서 처리. 취소/오류가 나면 아직 시작하지 않은 작업은 버린다."""
    pool = ThreadPoolExecutor(WORKERS)
    try:
        for fut in as_completed([pool.submit(work, i) for i in items]):
            on_result(fut.result())
    except BaseException:
        pool.shutdown(wait=True, cancel_futures=True)
        raise
    pool.shutdown(wait=True)


def extract_zip(path, dest, infos, safe_target, progress=None):
    """progress: myzip.progress.Progress (처리한 바이트, 취소, 일시 정지)."""
    # 폴더를 먼저 모두 만들어 두면 스레드끼리 makedirs 경쟁이 생기지 않는다
    targets = {}
    for info in infos:
        target = safe_target(dest, info.filename)
        targets[info.filename] = target
        os.makedirs(target if info.is_dir() else os.path.dirname(target), exist_ok=True)
    files = sorted((i for i in infos if not i.is_dir()), key=lambda i: -i.file_size)
    if progress:
        progress.begin(sum(i.file_size for i in files), len(files))
    readers = _Readers(path)

    def work(info):
        if progress:
            progress.advance(0)  # 취소했으면 파일을 만들기 전에 멈춘다
        target = targets[info.filename]
        try:
            with open(target, "wb", buffering=0) as dst:
                for buf in readers.chunks(info):
                    dst.write(buf)
                    if progress:
                        progress.advance(len(buf))
        except BaseException:
            try:  # 쓰다 만 파일은 지운다
                os.remove(target)
            except OSError:
                pass
            raise
        mtime = time.mktime(info.date_time + (0, 0, -1))
        os.utime(target, (mtime, mtime))
        return info.filename

    try:
        _run_all(work, files, progress.item_done if progress else lambda name: None)
    finally:
        readers.close()
    return len(infos)


def test_zip(path, progress=None):
    readers = _Readers(path)
    with open_zip(path) as zf:
        infos = [i for i in infolist(zf) if not i.is_dir()]
    if progress:
        progress.begin(sum(i.file_size for i in infos), len(infos))

    def work(info):
        try:
            for buf in readers.chunks(info):
                if progress:
                    progress.advance(len(buf))
            return info.filename, True
        except (zipfile.BadZipFile, zlib.error, OSError, EOFError):
            return info.filename, False

    bad = []

    def on_result(result):
        name, ok = result
        if not ok:
            bad.append(name)
        if progress:
            progress.item_done(name)

    try:
        _run_all(work, sorted(infos, key=lambda i: -i.file_size), on_result)
    finally:
        readers.close()
    return bad
