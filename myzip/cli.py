"""MyZip 명령줄 도구.

  python -m myzip c out.zip 파일1 폴더2 [-f tar.gz] [-l 9]   압축
  python -m myzip x archive.zip [-o 대상폴더 | --same]        압축 해제 (--same: 같은 폴더에)
  python -m myzip l archive.zip                              목록
  python -m myzip t archive.zip                              무결성 검사
"""
import argparse
import os
import sys

from . import __version__, core
from .progress import Progress


class _Bar(Progress):
    """명령줄 진행 막대: [#####.....] 45%  120.0 MB/s  남은 시간 0:12  파일이름"""

    def item_done(self, name):
        super().item_done(name)
        width = 30
        frac = self.done_bytes / self.total_bytes if self.total_bytes else self.done_items / max(self.total_items, 1)
        filled = int(width * frac)
        secs = max(self.elapsed(), 1e-6)
        speed = self.done_bytes / secs
        left = (self.total_bytes - self.done_bytes) / speed if speed else 0
        sys.stdout.write(f"\r[{'#' * filled}{'.' * (width - filled)}] {frac * 100:3.0f}%  "
                         f"{core.human_size(int(speed))}/s  남은 시간 {int(left) // 60}:{int(left) % 60:02d}  "
                         f"{name[:30]:<30}")
        sys.stdout.flush()
        if self.done_items == self.total_items:
            sys.stdout.write("\n")


def main(argv=None):
    p = argparse.ArgumentParser(prog="myzip", description="MyZip 압축 프로그램")
    p.add_argument("--version", action="version", version=f"MyZip {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("c", help="압축")
    c.add_argument("archive")
    c.add_argument("sources", nargs="+")
    c.add_argument("-f", "--format", choices=list(core.FORMATS), default=None)
    c.add_argument("-l", "--level", type=int, default=6, choices=range(10))

    x = sub.add_parser("x", help="압축 해제")
    x.add_argument("archive")
    x.add_argument("-o", "--out", default=None, help="풀 폴더 지정")
    x.add_argument("--same", action="store_true", help="압축 파일과 같은 폴더에 풀기")

    sub.add_parser("l", help="목록").add_argument("archive")
    sub.add_parser("t", help="무결성 검사").add_argument("archive")

    a = p.parse_args(argv)
    try:
        if a.cmd == "c":
            fmt = a.format or _guess_format(a.archive)
            n = core.create(a.sources, a.archive, fmt, a.level, _Bar())
            print(f"{n}개 항목 압축 완료 → {a.archive} ({core.human_size(os.path.getsize(a.archive))})")
        elif a.cmd == "x":
            if a.same:
                out = os.path.dirname(os.path.abspath(a.archive))
            else:
                out = a.out or core.name_folder_dest(a.archive)
            n = core.extract(a.archive, out, progress=_Bar())
            print(f"{n}개 항목 해제 완료 → {out}")
        elif a.cmd == "l":
            entries = core.list_entries(a.archive)
            print(f"{'크기':>12} {'압축':>12} {'비율':>6}  {'수정 시각':<19}  이름")
            for e in entries:
                when = e.modified.strftime("%Y-%m-%d %H:%M:%S") if e.modified else ""
                print(f"{e.size:>12,} {e.packed:>12,} {e.ratio:>5.1f}%  {when:<19}  {e.name}")
            print(f"총 {len(entries)}개 항목")
        elif a.cmd == "t":
            bad = core.test(a.archive, _Bar())
            if bad:
                print("손상된 항목:", *bad, sep="\n  ")
                return 1
            print("이상 없음")
    except core.ArchiveError as e:
        print("오류:", e, file=sys.stderr)
        return 1
    return 0


def _guess_format(name: str) -> str:
    low = name.lower()
    for fmt, ext in sorted(core.FORMATS.items(), key=lambda kv: -len(kv[1])):
        if low.endswith(ext) and not fmt.startswith("zip ("):
            return fmt
    return "zip"
