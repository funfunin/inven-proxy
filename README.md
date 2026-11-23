# inven-proxy

인벤 2097 게시판(오픈이슈갤러리)에서 ㅇㅎ/ㅎㅂ 글을 가져와 미니앱에 주는 작은 프록시 서버야.

## 왜 필요해?

인벤은 CORS 헤더를 안 보내서, 브라우저에서 돌아가는 미니앱이 인벤을 직접 읽을 수 없어.
서버끼리는 CORS가 적용 안 되니까, 이 서버가 인벤을 가져와서 미니앱이 읽을 수 있게(JSON + CORS 허용) 건네줘.
그래서 미니앱 갤러리가 스냅샷이 아니라 실시간 목록을 보여줄 수 있어.

## 뭐가 있어?

- `server.py` — 표준 라이브러리만 쓰는 서버. 비밀 키도, DB도, 의존성도 없어.
- `render.yaml` — Render Blueprint (버튼 한 번으로 배포).
- `requirements.txt` — 비어 있음 (의존성 없음).

## API

- `GET /api/inven` → `{"updated_at", "total", "matched": [{"id", "title", "url"}]}`
  - 60초 캐시. 인벤이 잠깐 안 되면 마지막 결과를 `stale: true`로 주고, 아예 없으면 502.
- `GET /health` → `ok`
- `GET /` → 상태 페이지

## Render에 배포하기

1. 이 폴더를 GitHub 저장소로 올린다 (예: `funfunin/inven-proxy`).
2. Render 대시보드 → New → Blueprint → 저장소 선택 → Apply.
   - 또는 New → Web Service → 저장소 선택 → Runtime `Python` → Start Command `python server.py` → Free 플랜 → Create.
3. 배포가 끝나면 주소가 나와 (예: `https://inven-proxy-xxxx.onrender.com`).
4. 그 주소를 Muse에게 주면 미니앱이 `https://<주소>/api/inven`에서 실시간 목록을 가져오게 바꿔줘.

## 알고 있어야 할 것

- Render 무료 플랜은 15분 안 쓰면 잠들어. 잠든 뒤 첫 요청은 깨우는 데 30~60초쯤 걸려. 미니앱에서 첫 로딩이 한 번 느릴 수 있어.
- 가져오는 건 공개 게시판 제목 + 링크뿐이야. 본문 이미지를 긁어오는 게 아니야.
