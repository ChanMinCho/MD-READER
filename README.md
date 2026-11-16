# MD-READER

브라우저에서 마크다운을 읽기 좋게 보여 주는 뷰어입니다. 설치 없이 웹에서 바로 쓰거나, 내 PC 에서 로컬 서버로 띄워 쓸 수 있습니다.

**웹에서 바로 쓰기:** https://chanmincho.github.io/MD-READER/

## 할 수 있는 것

- 마크다운 렌더링 (표, 각주, 체크리스트, 목차)
- 코드 강조, JSON 블록 자동 정렬
- mermaid 다이어그램, KaTeX 수식
- 라이트/다크 테마, 배경, 도트 고양이
- 번역 (Chrome 138 이상 내장 번역기)
- 편집, HTML 내보내기, PDF 저장

## 웹 버전과 로컬 버전

| | 웹 (github.io) | 로컬 (`server.py`) |
|---|---|---|
| 파일 열기 | 파일 고르기 · 끌어다 놓기 | 위 방법 + 경로 입력·자동완성 |
| 최근 파일·설정 | 이 브라우저에 저장 | 뷰어 폴더 `state.json` 에 저장 |
| 저장 | 내려받기 | 원래 파일에 덮어쓰기 |
| PDF | 인쇄 창에서 "PDF로 저장" | 바로 PDF 파일 생성 (Chrome·Edge 필요) |
| 검색 | 현재 문서 | 최근 파일 전체 |
| md 옆 이미지 | 표시 안 됨 | 표시 |

웹 버전에서 연 문서는 브라우저 안에서만 읽힙니다. 어디에도 업로드되지 않습니다.

## 로컬에서 쓰기

python3 만 있으면 됩니다. 외부 패키지는 필요 없습니다.

```bash
git clone https://github.com/ChanMinCho/MD-READER.git
cd MD-READER
python3 server.py            # 뷰어 실행 후 브라우저가 열림
python3 server.py 문서.md     # 파일을 열면서 실행
```

macOS 에서는 `마크다운뷰어.command` 를 더블클릭해도 됩니다.

## 라이선스

같이 들어 있는 라이브러리와 폰트의 라이선스는 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 에 있습니다.
