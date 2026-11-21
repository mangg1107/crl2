# CRL2

RLCM 웹 운영 화면과 BAND 사진 대댓글 게임을 Google Sheets의 참가자와 칩 잔액에 연결합니다. BAND 게임은 참가자별로 진행되며, 정산 결과를 RLCM 대시보드의 로그에서 확인할 수 있습니다.

## Vercel 웹과 Windows 봇

Vercel이 웹 화면과 정산 API를 운영합니다. Windows가 꺼져 있을 때도 사이트에서 참가자와 칩을 관리할 수 있으며, BAND 대댓글은 Windows에서 봇이 실행되는 동안 감시합니다. 참가자, 잔액 및 게임 기록은 Sheets에 저장합니다. PostgreSQL은 여러 Vercel 인스턴스 사이의 정산 요청 순서를 제어할 때 선택적으로 사용합니다.

### Vercel 설정

1. GitHub 저장소를 Vercel에 연결합니다. 저장소 안에 `crl2`가 들어 있다면 **Root Directory**를 `crl2`로 지정합니다. `crl2` 자체를 저장소로 올렸다면 기본 디렉터리를 사용하고, Framework Preset은 **Express**를 선택합니다.
2. **Settings → Environment Variables**에서 Production에 아래 값을 설정하고 배포합니다. [Vercel 환경변수 문서](https://vercel.com/docs/environment-variables)

| 변수 | 값 |
| --- | --- |
| `GOOGLE_SPREADSHEET_ID` | RLCM 시트 URL의 `/d/`와 `/edit` 사이 ID |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | 시트 편집 권한이 있는 서비스 계정 JSON 전체. 또는 `GOOGLE_CLIENT_EMAIL`과 `GOOGLE_PRIVATE_KEY` 설정 |
| `DATABASE_URL` | 선택 항목. PostgreSQL 풀링 연결 주소. 설정하지 않으면 각 Vercel 인스턴스 안에서 요청을 순서대로 처리합니다. 여러 인스턴스의 동시 정산까지 조정하려면 설정하세요. |
| `CRL_WORKER_TOKEN` | Windows 봇과 Vercel이 공유하는 토큰 |

웹 운영 화면과 API는 로그인 없이 공개됩니다. 공개 운영 화면을 사용하려면 Vercel Production Deployment Protection도 꺼야 합니다. 봇 토큰은 Windows 봇의 내부 API 요청에만 사용합니다.

```powershell
node -e "console.log(require('node:crypto').randomBytes(32).toString('hex'))"
```

Vercel Production 도메인에 접속해 참가자 목록을 확인하고 BAND 표시 이름을 연결합니다. 같은 스프레드시트를 쓰는 기존 운영 서버와 예약 작업은 전환할 때 중지해 주세요. 정산 잠금은 같은 데이터베이스에 연결된 CRL2의 요청에 적용됩니다. Preview 배포에서 테스트할 경우 별도 시트를 사용합니다.

### Windows 봇 설정

Windows에 Node.js 22 이상, Python 3.11 이상, Edge를 준비합니다. PowerShell로 `crl2` 폴더에서 실행합니다.

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r worker\requirements.txt
Copy-Item .env.worker.example .env.worker
notepad .env.worker
npm.cmd run worker
```

`.env.worker`에는 `CRL_PUBLIC_URL`(예: `https://my-project.vercel.app`), Vercel과 동일한 `CRL_WORKER_TOKEN`, `BAND_PAGE_URL`을 넣습니다. 처음 뜬 Edge 창에서 BAND에 로그인합니다. Windows 콘솔에서 **BAND 대댓글 감시를 시작했습니다**가 표시되면 다음 분에 테스트 댓글을 작성합니다. 내부 `/api/crl/internal/*` 경로는 CRL2의 봇 토큰으로 인증합니다.

`.env.worker`, 서비스 계정 JSON과 브라우저 프로필은 Git 저장소 밖에서 관리합니다. Windows 봇은 Sheets 자격 증명 없이 Vercel API로 명령을 보냅니다.

## Windows 단독 실행

Node.js 22 이상, Python 3.11 이상, Edge가 필요합니다. PowerShell에서 `crl2` 폴더를 열어 실행합니다.

```powershell
npm.cmd install
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r worker\requirements.txt
Copy-Item .env.example .env
notepad .env
npm.cmd start
```

1. `.env`의 `BAND_PAGE_URL`에 게임용 BAND 사진 상세 주소를 지정합니다. `https://www.band.us/band/번호/media/번호` 형식입니다.
2. RLCM에서 사용한 서비스 계정 인증을 `crl2/key.json` 또는 `GOOGLE_SERVICE_ACCOUNT_JSON`이나 `GOOGLE_CLIENT_EMAIL`·`GOOGLE_PRIVATE_KEY` 환경변수로 설정합니다. 서비스 계정에는 스프레드시트 편집 권한이 필요합니다. 기본값은 기존 RLCM 시트입니다. 별도 운영용 시트를 쓰려면 `GOOGLE_SPREADSHEET_ID`를 지정합니다.
3. [http://127.0.0.1:3000](http://127.0.0.1:3000)에서 BAND 표시 이름, RLCM 참가자, 정산에 사용할 칩 색을 연결합니다. 사이트는 이 PC에서 접속합니다. 명령은 각자의 BAND 계정에서 작성합니다.
4. **봇 시작**을 누르고 처음 열린 전용 브라우저에서 BAND에 로그인합니다. 사이트에서 **댓글 감시 중**이 표시되면 진행할 수 있습니다.

Sheets 잔액을 수정하는 운영 작업은 CRL2 인스턴스 한 곳에서 수행하세요. 같은 시트를 쓰는 기존 RLCM 서버나 Vercel 작업과 동시에 수정하면 두 서버가 읽은 이전 잔액을 기준으로 각각 기록할 수 있습니다. 기존 봇과 CRL2의 감시 대상 사진은 분리하세요.

## BAND 대댓글

운영자가 봇에 로그인할 ALEA 계정으로 지정된 사진에 원댓글 네 개를 작성해 둡니다. 각 원댓글의 첫 줄은 `룰렛`, `바카라`, `블랙잭`, `환전` 중 해당 제목으로 적습니다. 두 번째 줄부터 안내 문구를 넣을 수 있습니다. 참가자는 처음에 네 원댓글 중 하나에 `확인`을 보냅니다. 각 대댓글의 종목은 원댓글 첫 줄에서 정합니다. BAND가 앞에 붙인 `@**ALEA**`나 `@ALEA`도 인식합니다. 봇은 명령 작성자에게 비밀 답글을 보냅니다. 참가자는 밴드에서 고유한 표시 이름을 유지해야 합니다.

| 동작 | 입력 양식 | 예시 |
| --- | --- | --- |
| 연결·잔액 확인 | `확인` | `확인` |
| 룰렛 | `[칩 색] <수량> / <대상>` | `Blue 10 / 1, 5, 8, 10` |
| 바카라 시작 | `[칩 색] <수량> / <플레이어·뱅커·타이>` | `노랑, 10, 플레이어` |
| 블랙잭 시작 | `[칩 색] <수량>` | `화이트 / 10` |
| 환전 | `<환전 전 칩 색>, <수량>, <환전 후 칩 색>` | `레드, 5, 그린` |
| 카드 한 장 받기 | `히트` | `히트` |
| 현재 패로 승부 | `스탠드` | `스탠드` |
| 진행 중인 패 또는 최근 정산 조회 | `상태` | `상태` |

값 사이에 쉼표, 띄어쓰기, `/`를 각각 쓰거나 섞어 쓸 수 있습니다. 게임의 칩 색은 수량 앞에 적습니다. 생략하면 참가자 연결에 설정한 정산 칩을 사용합니다. 룰렛의 `10 레드`는 기본 정산 칩 10개로 레드에 베팅하는 입력이며, `블루 10 레드`는 블루 칩 10개로 레드에 베팅하는 입력입니다.

칩 색은 `레드 / red / 빨강`, `블루 / blue / 파랑`, `그린 / green / 초록`, `옐로 / yellow / 노랑`, `화이트 / white / 흰색`처럼 영문과 한국어 이름, 음역을 모두 인식합니다. `옐로우`도 사용할 수 있습니다.

베팅 수량은 0 이상의 정수입니다. 룰렛은 번호 1~4개 또는 `1더즌`, `2컬럼`, `레드`, `블랙`, `짝수`, `홀수`, `빅`, `스몰` 같은 옵션 하나를 고릅니다. 히트와 스탠드는 진행 중인 카드 게임과 같은 종목의 원댓글 아래에서 보냅니다.

환전 수량은 1 이상의 정수입니다. 웹 환율로 계산한 뒤 소수점 이하는 버립니다. 환전 결과가 0이면 칩이 차감되지 않습니다.

모든 종목에서 0칩부터 베팅할 수 있습니다. 배당은 원금과 별도의 순이익 배율이며 소수점 이하는 버립니다. 룰렛은 0~36 중 하나를 뽑습니다. 임의 번호 1~4개 배율은 차례로 40:1, 20:1, 13:1, 9:1입니다. 더즌과 컬럼은 2.2:1이며 레드·블랙, 짝·홀, 빅·스몰은 1.2:1입니다. 0은 0에 직접 건 경우 당첨됩니다.

바카라는 첫 두 장의 합이 한쪽이라도 8·9이면 즉시 승부합니다. 양쪽이 0~7점이면 플레이어가 히트 또는 스탠드를 선택합니다. PLAYER는 1.2:1, BANKER는 1:1, TIE는 10:1입니다. TIE가 나왔을 때 PLAYER나 BANKER에 건 베팅은 반환합니다. 블랙잭은 플레이어가 21을 넘으면 즉시 패배하고, 딜러는 합이 17 이상이 될 때까지 뽑습니다. 승리 배율은 1.2:1이며 무승부는 베팅을 반환합니다.

BAND의 바카라·블랙잭은 참가자마다 한 판씩 진행합니다. 웹 카드 게임 진행 중 BAND에서 칩을 사용해 잔액이 부족해지면 웹 게임을 이어갈 때 베팅 칩을 다시 확보해야 합니다. Vercel 사이트의 **웹 게임** 메뉴에서 하이 앤 로우, 레드 앤 블랙, PvP 종목을 진행할 수 있습니다.

Sheets의 `BAND 게임` 탭에 이름 연결, 참가자 확인, 카드 게임 상태와 명령별 처리 기록이 저장됩니다. 서버는 칩 잔액 변경과 해당 명령 기록을 한 번의 Sheets 요청으로 반영합니다. 같은 댓글이 다시 들어오면 저장된 결과를 반환합니다. 소실된 답글 내용은 `상태`로 확인할 수 있습니다. 새 입력 양식은 적용 시각의 다음 분부터 처리합니다. 이후 재시작 시 기존 처리 기준과 이력을 유지합니다.

설정, 브라우저 프로필, 처리 이력은 `crl2` 안에 저장됩니다. `worker/browser_profile` 폴더에는 BAND 로그인 세션이 들어 있습니다. 로그와 게임 이력을 유지하려면 `BAND 게임` 탭, `band_`으로 시작하는 이름이 지정된 범위, `worker/processed-*.json` 파일을 보존하세요.

## 검증

```powershell
npm.cmd test
```

테스트는 메모리에서 Sheets 응답을 재현해 여러 참가자의 블랙잭 진행, 동일 댓글의 중복 방지, 응답 손실 후 조회, 잔액 부족을 검사합니다. Vercel API의 운영자 및 봇 인증, 같은 사이트의 요청만 받는 정책과 정산 잠금도 검사합니다.
