# MyZip

파이썬 표준 라이브러리만으로 만든 Windows용 압축 프로그램입니다.

## 다운로드

[Releases](https://github.com/a753df-lang/MyZip/releases/latest)에서 `MyZip.exe`를 받아 실행하세요. 설치가 필요 없습니다.

## 기능

- **압축:** ZIP, TAR, TAR.GZ, TAR.BZ2, TAR.XZ
  - 기본 ZIP은 모든 CPU 코어로 동시에 압축합니다.
  - 이미 압축된 파일(jpg, mp4, zip 등)은 다시 압축하지 않고 그대로 저장합니다.
- **풀기:** 폴더 선택해서 풀기, 같은 폴더에 풀기, 이름 폴더에 풀기
  - 압축 파일 안에 최상위 폴더가 하나뿐이면 폴더가 이중으로 생기지 않게 풉니다.
- **분할 압축 열기와 풀기:** `.z01`, `.z02` … `.zip` 형식과 `.zip.001`, `.002` … 형식
- **진행 창:** 속도 그래프, 경과 시간과 남은 시간, 속도, 처리한 크기, 일시 정지와 취소
- **무결성 검사**
- **한글 파일 이름:** UTF-8 표시가 없는 zip의 CP949 파일 이름도 깨지지 않습니다.
- **업데이트 확인:** 도움말 → 업데이트 확인

## 명령줄

```
myzip-cli c 결과.zip 파일 폴더 [-f tar.gz] [-l 9]   압축
myzip-cli x 압축.zip [-o 대상폴더 | --same]          풀기
myzip-cli l 압축.zip                                 목록
myzip-cli t 압축.zip                                 검사
```

소스에서 실행하려면 `python -m myzip`을 쓰면 됩니다. Python 3.11 이상이 필요합니다.

## 빌드

`build.bat`을 실행하면 `dist\MyZip.exe`와 `dist\myzip-cli.exe`가 만들어집니다. PyInstaller가 필요한데, 임시 가상환경에 자동으로 설치됩니다.

## 새 버전 배포

1. `myzip/__init__.py`의 `__version__`을 올립니다.
2. `build.bat`을 실행합니다.
3. GitHub Releases에서 `v1.3.0` 같은 태그로 새 릴리스를 만들고 `dist\MyZip.exe`를 첨부합니다.
