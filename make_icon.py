"""MyZip 아이콘(myzip.ico) 생성: 노란 폴더 + 지퍼. 외부 라이브러리 없이 PNG를 직접 만든다."""
import struct
import zlib


def draw(n):
    s = n / 256
    px = [[(0, 0, 0, 0)] * n for _ in range(n)]

    def rect(x0, y0, x1, y1, c, r=0):
        x0, y0, x1, y1, r = (int(v * s) for v in (x0, y0, x1, y1, r))
        for y in range(max(y0, 0), min(y1, n)):
            for x in range(max(x0, 0), min(x1, n)):
                dx = max(x0 + r - x, 0, x - (x1 - 1 - r))
                dy = max(y0 + r - y, 0, y - (y1 - 1 - r))
                if dx * dx + dy * dy <= r * r:
                    px[y][x] = c

    rect(24, 44, 116, 84, (230, 160, 20, 255), 14)      # 폴더 탭
    rect(16, 68, 240, 224, (250, 190, 40, 255), 18)     # 폴더 몸통
    rect(16, 68, 240, 92, (255, 214, 90, 255), 12)      # 윗면 하이라이트
    rect(116, 68, 140, 224, (90, 70, 50, 255))          # 지퍼 줄
    for i, y in enumerate(range(72, 196, 12)):          # 지퍼 이빨
        x = 108 if i % 2 else 128
        rect(x, y, x + 20, y + 7, (200, 200, 205, 255), 2)
    rect(110, 186, 146, 232, (60, 60, 70, 255), 8)      # 지퍼 손잡이
    rect(120, 198, 136, 220, (200, 200, 205, 255), 4)
    return px


def png(px):
    n = len(px)
    raw = b"".join(b"\0" + bytes(v for p in row for v in p) for row in px)

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", n, n, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


sizes = [16, 32, 48, 256]
images = [png(draw(n)) for n in sizes]
header = struct.pack("<HHH", 0, 1, len(sizes))
offset = 6 + 16 * len(sizes)
entries = b""
for n, img in zip(sizes, images):
    entries += struct.pack("<BBBBHHII", n % 256, n % 256, 0, 0, 1, 32, len(img), offset)
    offset += len(img)
with open("myzip.ico", "wb") as f:
    f.write(header + entries + b"".join(images))
print("myzip.ico 생성")
