"""MyZip.exe (창 프로그램) 빌드용 진입점. 압축 파일 경로를 인자로 주면 바로 연다."""
import sys

from myzip.gui import main

main(sys.argv[1] if len(sys.argv) > 1 else None)
