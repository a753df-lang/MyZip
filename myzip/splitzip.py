"""분할 압축 읽기.

  - zip 분할:   이름.z01, 이름.z02 … 이름.zip  (반디집·WinZip·7-Zip의 'zip 분할')
  - 단순 분할:  이름.zip.001, 이름.zip.002 …   (zip 파일을 바이트 단위로 잘라 둔 것)

조각들을 하나의 가상 파일로 이어 붙여 읽는다. zip 분할은 중앙 디렉터리의 오프셋이
조각마다 따로 매겨져 있으므로, 전체 기준 오프셋으로 고친 중앙 디렉터리를 맨 뒤에
덧붙여 표준 zipfile이 그대로 읽을 수 있게 만든다. 조각 파일은 고치지 않는다.
"""
import bisect
import io
import os
import re
import struct
import threading

U16, U32 = 0xFFFF, 0xFFFFFFFF
_Z_PART = re.compile(r"\.z(\d{2,})$", re.I)
_NUM_PART = re.compile(r"\.(\d{3})$")
_CENTRAL = struct.Struct("<4s4B4HL2L5H2L")  # 중앙 디렉터리 항목 고정부 (46바이트)


class SplitError(Exception):
    pass


def find_parts(path: str) -> list[str] | None:
    """분할 압축이면 조각 경로 목록(순서대로), 아니면 None."""
    path = os.path.abspath(path)
    m = _NUM_PART.search(path)
    if m:
        stem, width = path[: m.start()], len(m.group(1))
        parts = []
        while os.path.isfile(p := f"{stem}.{len(parts) + 1:0{width}d}"):
            parts.append(p)
        if not parts:
            raise SplitError(f"첫 조각이 없습니다: {os.path.basename(stem)}.{1:0{width}d}")
        return parts
    m = _Z_PART.search(path)
    if m:
        stem = path[: m.start()]
    elif path.lower().endswith(".zip"):
        stem = path[:-4]
    else:
        return None
    parts = []
    while os.path.isfile(p := f"{stem}.z{len(parts) + 1:02d}"):
        parts.append(p)
    if not parts:
        if m:  # .z02 등을 열었는데 .z01이 없음
            raise SplitError(f"첫 조각이 없습니다: {os.path.basename(stem)}.z01")
        if _eocd_disk(path):  # 분할의 마지막 조각(.zip)만 있음
            raise SplitError(f"분할 압축의 나머지 조각(.z01, .z02 …)을 같은 폴더에서 찾을 수 없습니다: "
                             f"{os.path.basename(path)}")
        return None
    last = stem + ".zip"
    if not os.path.isfile(last):
        raise SplitError(f"마지막 조각이 없습니다: {os.path.basename(last)}")
    return parts + [last]


def stem_name(name: str) -> str:
    """'사과.z01' → '사과', '사과.zip.001' → '사과.zip'"""
    m = _Z_PART.search(name) or _NUM_PART.search(name)
    return name[: m.start()] if m else name


class SplitFile(io.RawIOBase):
    """여러 조각 + 덧붙인 바이트(tail)를 하나의 읽기 전용 파일처럼 보여 준다."""

    def __init__(self, parts, sizes, tail=b""):
        super().__init__()
        self.parts, self.sizes, self.starts = [], [], []
        pos = 0
        for p, s in zip(parts, sizes):
            if s:  # 빈 조각은 건너뛴다 (bisect가 엉뚱한 조각을 고르지 않게)
                self.parts.append(p)
                self.sizes.append(s)
                self.starts.append(pos)
                pos += s
        self.tail_start, self.tail = pos, tail
        self.size = pos + len(tail)
        self.pos = 0
        self.handles = {}

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, offset, whence=0):
        if whence == 1:
            offset += self.pos
        elif whence == 2:
            offset += self.size
        if offset < 0:
            raise OSError("음수 위치로 이동할 수 없습니다")
        self.pos = offset
        return offset

    def read(self, n=-1):
        # 조각 경계에서 짧게 끊지 않고 요청한 만큼 채워서 돌려준다 (zipfile이 그렇게 기대함)
        if n is None or n < 0:
            n = self.size - self.pos
        out = []
        while n > 0 and self.pos < self.size:
            if self.pos >= self.tail_start:
                at = self.pos - self.tail_start
                buf = self.tail[at: at + n]
            else:
                i = bisect.bisect_right(self.starts, self.pos) - 1
                at = self.pos - self.starts[i]
                f = self.handles.get(i)
                if f is None:
                    f = self.handles[i] = open(self.parts[i], "rb", buffering=0)
                f.seek(at)
                buf = f.read(min(n, self.sizes[i] - at))
                if not buf:
                    raise OSError(f"조각을 읽을 수 없습니다: {os.path.basename(self.parts[i])}")
            out.append(buf)
            self.pos += len(buf)
            n -= len(buf)
        return b"".join(out)

    def readinto(self, b):
        data = self.read(len(b))
        b[: len(data)] = data
        return len(data)

    def close(self):
        for f in self.handles.values():
            f.close()
        self.handles.clear()
        super().close()


_cache = {}
_cache_lock = threading.Lock()


def open_split(path: str):
    """분할 압축이면 '가상 파일을 새로 여는 함수', 아니면 None.

    조각이 빠졌거나 손상되었으면 SplitError. 스레드마다 따로 열 수 있도록 함수를 돌려준다.
    """
    parts = find_parts(path)
    if not parts:
        return None
    stats = [os.stat(p) for p in parts]
    key = tuple((p, s.st_size, s.st_mtime_ns) for p, s in zip(parts, stats))
    with _cache_lock:
        hit = _cache.get(key)
    if hit is None:
        sizes = [s.st_size for s in stats]
        if _NUM_PART.search(parts[0]):
            hit = (parts, sizes, b"") if _has_eocd(parts[-1]) else False
        else:
            hit = _rebuild(parts, sizes)
        with _cache_lock:
            _cache.clear()  # 마지막으로 연 분할 압축 하나만 기억
            _cache[key] = hit
    if not hit:
        return None
    parts, sizes, tail = hit
    return lambda: SplitFile(parts, sizes, tail)


def total_size(path: str) -> int:
    parts = find_parts(path)
    return sum(os.path.getsize(p) for p in parts) if parts else os.path.getsize(path)


def _eocd_disk(path) -> int:
    """zip 끝 레코드의 '이 디스크 번호'. 분할의 마지막 조각이면 0보다 크다. 읽을 수 없으면 0."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(f.tell() - 65557, 0))
            end = f.read()
    except OSError:
        return 0
    at = end.rfind(b"PK\x05\x06")
    if at < 0 or len(end) - at < 22:
        return 0
    disk = struct.unpack_from("<H", end, at + 4)[0]
    if disk == U16 and at >= 20 and end[at - 20: at - 16] == b"PK\x06\x07":
        disk = struct.unpack_from("<L", end, at - 4)[0] - 1  # ZIP64 locator의 전체 디스크 수 - 1
    return disk


def _has_eocd(path):
    with open(path, "rb") as f:
        f.seek(0, 2)
        f.seek(max(f.tell() - 65557, 0))
        return b"PK\x05\x06" in f.read()


def _rebuild(parts, sizes):
    """조각별 오프셋을 전체 기준으로 바꾼 중앙 디렉터리 + 끝 레코드를 만든다."""
    name = os.path.basename(parts[-1])
    with open(parts[-1], "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(size - 65557 - 76, 0))
        end = f.read()
    at = end.rfind(b"PK\x05\x06")
    if at < 0 or len(end) - at < 22:
        raise SplitError(f"손상된 분할 압축 파일입니다: {name}")
    disk_no, cd_disk, _, count, cd_size, cd_off, _ = struct.unpack("<4H2LH", end[at + 4: at + 22])
    if U16 in (disk_no, cd_disk, count) or U32 in (cd_size, cd_off):
        loc = end[at - 20: at] if at >= 20 else b""
        if loc[:4] != b"PK\x06\x07":
            raise SplitError(f"손상된 분할 압축 파일입니다: {name}")
        e64_disk, e64_off, _ = struct.unpack("<LQL", loc[4:])
        if e64_disk >= len(parts):
            raise SplitError(f"조각이 빠졌습니다: {os.path.basename(parts[-1][:-4])}.z{len(parts):02d}")
        starts = _starts(sizes)
        with SplitFile(parts, sizes) as vf:
            vf.seek(starts[e64_disk] + e64_off)
            e64 = vf.read(56)
        if e64[:4] != b"PK\x06\x06":
            raise SplitError(f"손상된 분할 압축 파일입니다: {name}")
        disk_no, cd_disk, _, count, cd_size, cd_off = struct.unpack("<2L4Q", e64[16:56])
    if disk_no == 0:
        return False  # .z01이 옆에 남아 있을 뿐 실제로는 분할이 아닌 zip
    if disk_no != len(parts) - 1:
        stem = parts[-1][:-4]
        raise SplitError(f"조각이 빠졌습니다: {os.path.basename(stem)}.z{len(parts):02d}")

    starts = _starts(sizes)
    with SplitFile(parts, sizes) as vf:
        vf.seek(starts[cd_disk] + cd_off)
        cd = vf.read(cd_size)
    if len(cd) != cd_size:
        raise SplitError(f"손상된 분할 압축 파일입니다: {name}")

    out = bytearray()
    pos = n = 0
    while pos + 46 <= len(cd) and cd[pos: pos + 4] == b"PK\x01\x02":
        fields = list(_CENTRAL.unpack_from(cd, pos))
        name_len, extra_len, comment_len, disk, _, _, offset = fields[12:19]
        csize, usize = fields[10], fields[11]
        fname = cd[pos + 46: pos + 46 + name_len]
        extra = cd[pos + 46 + name_len: pos + 46 + name_len + extra_len]
        comment = cd[pos + 46 + name_len + extra_len: pos + 46 + name_len + extra_len + comment_len]
        pos += 46 + name_len + extra_len + comment_len

        # ZIP64 추가 필드에서 실제 값을 꺼내고, 나머지 추가 필드는 그대로 둔다
        others, z64 = bytearray(), b""
        i = 0
        while i + 4 <= len(extra):
            tag, ln = struct.unpack_from("<HH", extra, i)
            if tag == 1:
                z64 = extra[i + 4: i + 4 + ln]
            else:
                others += extra[i: i + 4 + ln]
            i += 4 + ln
        j = 0
        vals = {}
        for key, cur in (("usize", usize), ("csize", csize), ("offset", offset)):
            if cur == U32:
                vals[key] = struct.unpack_from("<Q", z64, j)[0]
                j += 8
        if disk == U16:
            disk = struct.unpack_from("<L", z64, j)[0]
        if disk >= len(starts):
            raise SplitError(f"손상된 분할 압축 파일입니다: {name}")
        offset = starts[disk] + vals.get("offset", offset)

        new64 = b""
        if usize == U32:
            new64 += struct.pack("<Q", vals["usize"])
        if csize == U32:
            new64 += struct.pack("<Q", vals["csize"])
        if offset >= U32:
            new64 += struct.pack("<Q", offset)
            offset = U32
        if new64:
            others += struct.pack("<HH", 1, len(new64)) + new64
        fields[13], fields[15], fields[18] = len(others), 0, offset
        out += _CENTRAL.pack(*fields) + fname + others + comment
        n += 1
    if n != count:
        raise SplitError(f"손상된 분할 압축 파일입니다: {name}")

    # 덧붙인 중앙 디렉터리는 조각들 바로 뒤(전체 크기 위치)에서 시작한다
    cd_start = sum(sizes)
    cd_end = cd_start + len(out)
    if n >= U16 or cd_start >= U32 or len(out) >= U32:
        out += struct.pack("<IQHHIIQQQQ", 0x06064B50, 44, 45, 45, 0, 0, n, n, len(out), cd_start)
        out += struct.pack("<IIQI", 0x07064B50, 0, cd_end, 1)
        out += struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, U16, U16, U32, U32, 0)
    else:
        out += struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, n, n, len(out), cd_start, 0)
    return parts, sizes, bytes(out)


def _starts(sizes):
    starts, pos = [], 0
    for s in sizes:
        starts.append(pos)
        pos += s
    return starts
