# Metaverse Captioner

Windows 학생용 실시간 자막 보조창 MVP입니다.

앱이 학생 PC에서 재생되는 시스템 소리를 WASAPI loopback으로 캡처하고, OpenAI Realtime transcription으로 한국어 자막을 만든 뒤 필요하면 선택 언어로 번역해서 화면 아래 작은 창에 표시합니다. 메타버스 외부의 별도 자막창으로 쓰는 구조입니다.

## 실행

PowerShell에서 워크스페이스 루트 기준:

```powershell
.\metaverse_captioner\run_captioner.ps1
```

`OPENAI_API_KEY` 환경변수가 없으면 앱 시작 버튼을 누를 때 키 입력창이 뜹니다. 입력한 키는 파일에 저장하지 않습니다.

PowerShell이 헷갈리면 간단하게 `metaverse_captioner\run_captioner.bat`을 더블클릭해도 됩니다.

## 사용법

1. 메타버스 수업이나 영상 회의 소리가 PC에서 재생되게 합니다.
2. 이 앱을 실행합니다.
3. 언어를 고릅니다.
4. `API Key`를 누르고 OpenAI의 API Key를 붙여넣기 합니다. 실제 프로그램 제작 시 학생용 아이디 등으로 접속할 수 있도록 대체합니다.
5. `Start`를 누르면 화면 아래 자막창이 동작합니다.

## 현재 MVP 기능

- Windows 시스템 오디오 loopback 캡처
- 화면 하단 항상 위 자막창
- 한국어 실시간 전사 및 외국어 번역 (영어, 베트남어, 중국어, 일본어, 러시아어, 태국어, 몽골어, 우즈베키스탄어, 아랍어)
- 문장/발화 완료 단위 번역
- 글자 크기, 투명도, 창 너비 조절
- 자막 로그 저장

## 참고

- `gpt-realtime-whisper` 모델을 한국어 실시간 전사용으로 사용합니다.
- 외국어 번역은 기본적으로 `gpt-realtime-translate`를 사용합니다.
- 몽골어와 우즈베키스탄어는 현재 `gpt-realtime-whisper` 전사 후 빠른 텍스트 모델로 번역합니다.
- 전사 오디오 커밋 간격은 문장 중간 절단을 줄이기 위해 6초로 설정했습니다.
- 스테레오 믹스 장치가 없어도 기본 재생 장치 loopback을 사용합니다.
