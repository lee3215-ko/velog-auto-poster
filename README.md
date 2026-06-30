# 벨로그 자동 포스팅

다계정 벨로그 자동 출간 + TempMail 임시 메일 생성 데스크톱 앱입니다.

## 실행

```powershell
pip install -r requirements.txt
playwright install chromium
python velog_gui.py
```

또는 `run.bat`

## 빌드 (배포용 exe)

```powershell
.\build.bat
```

결과: `dist\VelogPoster\VelogPoster.exe`

## 자동 업데이트

- 앱 시작 시 GitHub `version.json`을 확인해 새 버전이 있으면 알림
- **예** 선택 시 zip 다운로드 → 설치 폴더에 덮어쓰기 → 재실행
- `velog_settings.json`(계정·탭·설정)은 업데이트 시 **그대로 유지**

설정 파일은 exe와 같은 폴더에 저장됩니다.

## 배포 (개발자)

버전 올리고 GitHub Release 올리기:

```powershell
.\deploy.bat
```

`paths.py`의 `APP_VERSION`과 `version.json`이 자동으로 bump 됩니다.

## 저장소

https://github.com/lee3215-ko/velog-auto-poster
