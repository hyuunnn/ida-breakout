# ida-breakout

IDA Pro의 Pseudocode 뷰에 떠 있는 디컴파일 결과 텍스트를 그대로 벽돌로 만들어
Breakout(벽돌깨기)을 즐기는 플러그인. 변수명/키워드/숫자가 화면에서 통째로
부서져 사라지는 비주얼이 핵심.

핫키 한 번이면 현재 보고 있는 함수가 게임판이 되고, 다시 누르면 원래 코드로
돌아옴.

플러그인 골격은 [HexRaysSA/ida-claude-plugins](https://github.com/HexRaysSA/ida-claude-plugins)
의 `ida-plugin-development` 스킬이 제공하는 컨벤션을 따름 (PLUGIN_ENTRY shim,
`plugmod_t` 라이프사이클, action / UI hook 패턴, `ida-plugin.json` 매니페스트,
hcli 패키징). 이 컨벤션과 어긋나는 변경은 의도적인 경우에만 — 그 의도는
"의도적 설계 결정" 섹션에 기록.

## 요구사항

- IDA Pro 9.0+
- Hex-Rays Decompiler 라이선스
- PySide6 (IDA 9.x 번들). IDA 8.x는 지원하지 않음.

## 설치

레포를 IDA 플러그인 폴더에 심링크하거나 통째로 복사:

```sh
git clone https://github.com/<owner>/ida-breakout.git
ln -s "$(pwd)/ida-breakout" ~/.idapro/plugins/ida-breakout
```

IDA를 재시작하면 `ida_breakout_entry.py`가 `PLUGIN_ENTRY`로 잡혀 로드됨.

## 사용법

Pseudocode 뷰 (`F5`로 디컴파일된 창)에서:

- **시작**: `Ctrl-Alt-K` 또는 우클릭 메뉴 *"ida-breakout: Start brick break"*
  (우클릭 메뉴는 게임 중엔 오버레이가 마우스를 흡수해 열리지 않음 — 시작 전용)
- **이동**: `←` / `→` (또는 `h`/`l`, `a`/`d`)
- **발사**: `Space`
- **재시작**: `R` (WIN/LOSE 화면에서)
- **종료**: `Esc` 또는 `Ctrl-Alt-K`

게임은 현재 함수의 디컴파일 결과 위에 투명 오버레이로 깔리고, 충돌 박스는
실제 텍스트 픽셀에서 추출됨. 점수 15점마다 추가 공이 분기하고 (최대 5개),
부순 벽돌 수에 비례해 속도가 점진적으로 가속(최대 2.0x).

## 파일 구조

```
ida_breakout_entry.py       # PLUGIN_ENTRY shim, should_load() 환경 게이트
ida_breakout.py          # plugin_t / plugmod_t, 액션, UI/Hex-Rays 훅
ida_breakout_lib/
  game.py                 # 순수 물리 (Qt 의존 없음, 단위 테스트 가능)
  pseudocode.py           # viewport 탐지, bg 색 샘플링, 픽셀 brick 검출
  overlay.py              # BreakoutOverlay(QWidget) — 페인트, 입력, 타이머
tests/
  test_game.py            # 충돌/게임 플로우 단위 테스트 (Qt 불필요)
```

## 핵심 아키텍처

### 픽셀 기반 brick 검출

텍스트/`QFontMetrics` 기반이 아니라 픽셀에서 직접 ink 영역을 추출. IDA가
라인 헤더 padding, 인덴트 가이드, 컬러 룬 등을 그리는 방식이 빌드마다
미묘하게 달라 글자 좌표가 어긋나는 문제가 있었음.

흐름:
1. `viewport.grab()` → `QImage` (RGB32)
2. `sample_viewport_bg_colors()`로 다중 배경색 샘플링 (라인 하이라이트, 인덴트
   가이드 등 false positive 방지). 색은 양자화 없이 **정확한 픽셀값**으로
   수집 — erase fill로 그대로 쓰이므로 몇 단위만 어긋나도 사각형이 티가 남.
3. 행/열 단위로 ink 스캔 → 연속 영역을 brick으로 묶음. erase 사각형
   (`Brick.erase`)을 먼저 계산: ink보다 ~2 logical px 크게 잡아 anti-aliasing
   halo를 덮되, 이웃 라인/토큰과의 gap **중간점**에서 클램프 — 지울 때 옆 라인
   글자를 깎지 않음. `Brick.bg`(local 배경색)는 그 **erase 사각형 내부**의
   non-ink 최빈색 — 실제로 칠할 영역에서 관측된 색만 쓰므로 하이라이트 경계가
   erase 밴드에 걸려도 엉뚱한 색 프린지가 안 남고, 라인/토큰 하이라이트 위의
   brick도 제 색으로 지워짐.
4. HiDPI는 device pixel ratio로 device→logical 변환. dpr은 grab한 pixmap의
   `devicePixelRatio()`를 그대로 읽음 — 폭 비율(`grab폭/뷰포트폭`)로 추정하면
   pixmap 정수 라운딩 노이즈(예: 1.5x에서 1.50050)가 ceil 경계를 넘겨 튜닝값이
   창 폭 1px 차이로 널뛰기함. 좌상단은 floor, 우하단은 ceil — 양쪽 다 절삭하면
   오른쪽/아래 모서리가 1px 덜 지워져 잔상. 검출 튜닝 파라미터는 **logical px
   기준**으로 받아 내부에서 dpr로 환산: gap/padding류는 `_dp()`(ceil — 명시적
   0은 0으로 존중, 분수 dpr에서도 단조 스케일링), 최소 크기 필터(`min_run_w/h`)는
   `_dp_min()`(round, 최소 1 — 하한에 ceil을 쓰면 분수 dpr에서 1 device px짜리
   ink run이 새로 탈락하는 역방향 반올림이 됨). 1x/2x 디스플레이에서 같은
   폰트가 같은 brick 분할을 냄. 단 0.5 같은 sub-pixel 값은 dpr=1에서 1 device
   px로 양자화됨. 기본값은 기존 Retina(dpr=2) device 값과 정확히 일치하도록
   잡음 (Retina에서는 동작 변화 없음).
5. 자식 위젯이 차지하는 영역(스크롤바, 헤더 등)은 이중으로 마스킹: 스캔
   **전에** 해당 픽셀을 주 배경색으로 중립화하고 (fallback 경로에서 grab에
   찍힌 스크롤바가 전 행에 ink를 뿌려 라인 밴드를 통째로 병합시키는 걸 차단 —
   검출 후 brick 드롭만으로는 밴드 분할 오염을 되돌릴 수 없음), 스캔 **후에도**
   겹치는 brick을 드롭
6. **캐럿 방어**: 텍스트 캐럿은 포커스가 있을 때만 그려지는 얇은 세로 막대라,
   grab에는 찍히지만 오버레이가 포커스를 가져가면 화면에서 사라짐 → 공이
   튕기는 "투명 벽"이 됨. 이중 방어: (a) `start_game()`이 grab 전에
   `clearFocus()`로 캐럿을 숨기고, (b) 검출 단계에서 폭 ≤ ~2 logical px이면서
   ink 색이 2가지 이하이고 **라인 밴드 행의 ~90% 이상을 덮는** 단색 막대(캐럿,
   인덴트 가이드 — 둘 다 라인 전체 높이로 그려짐)를 brick에서 제외. 진짜
   글리프는 보통 anti-aliasing 때문에 3가지 이상의 ink 색을 갖지만, AA가 꺼진
   폰트(비트맵 폰트 등)에선 단색이므로 높이 조건이 좁은 글리프('l', '|')를
   구제함 — 글리프 stem은 밴드 전 행을 덮는 일이 드묾.

### Viewport 식별

Pseudocode 외곽 widget은 `TEAViewer`. 그 안의 가장 큰 visible child(viewport
면적의 50% 이상)가 실제 텍스트 surface. `find_pseudocode_viewport()`가 이걸
찾고, 못 찾으면 outer 자체를 fallback으로 사용. 모르는 IDA 빌드를 대응하기
위해 viewport 탐지 진단 로그를 `logging.getLogger(__name__).info(...)`로 남김
(자세한 내용은 "진단" 섹션).

### Plugmod 라이프사이클

`breakout_plugin_t(PLUGIN_MULTI)` → `breakout_plugmod_t`. plugmod에서:

- `_StartGameHandler.update()`: `BWN_PSEUDOCODE` 위젯에서만 enable
- `toggle_game()`: 게임이 뜬 탭에서 누르면 종료(토글 off). **다른** pseudocode
  탭에서 누르면 기존 게임을 정리하고 그 탭에서 새로 시작 — 단일 오버레이
  아키텍처라 게임이 "이동"함. 이때 `start_game(twidget)`에 대상 위젯을 직접
  넘김: `stop_game()`이 이전 탭을 activate하므로 `get_current_widget()`을
  다시 읽으면 이전 탭이 나옴.
- `start_game()`은 overlay를 `active_overlay`에 **등록한 뒤** `start()` 호출:
  `__init__`에서 이미 이벤트 필터가 설치되므로, `start()`가 예외를 던져도
  `stop_game()`이 정리할 수 있는 상태로 남아 입력을 삼키는 고아 오버레이가
  안 생김.
- `_UIHooks.widget_invisible`: pseudocode 탭 닫힘 감지 → 자동 종료
- `_UIHooks.finish_populating_widget_popup`: 우클릭 메뉴에 액션 부착
- `_HexraysHooks.refresh_pseudocode`: 게임 중인 탭(`vu.ct == active_twidget`)의
  F5 재디컴파일만 감지 → 자동 종료. 다른 pseudocode 탭의 F5는 무시

`stop_game()` 마지막에 `ida_kernwin.activate_widget(twidget, True)`을 호출하는
이유: overlay가 `deleteLater()`로 사라진 직후 IDA의 current widget이 일시적으로
None이나 다른 도크로 빠지면서 다음 핫키 입력이 액션 `update()`에서
`AST_DISABLE_FOR_WIDGET`으로 평가되어 1~2번 무시되는 현상이 있음. 명시적으로
pseudocode TWidget으로 포커스를 복귀시켜 해결.

### Overlay 투명 자식 QWidget

`BreakoutOverlay`는 viewport의 자식 QWidget. `WA_TranslucentBackground`로 텍스트가
비쳐 보임. 부서진 brick의 erase 사각형은 QPixmap 레이어(`_erase_layer`)에
증분으로 구워 두고 `paintEvent`는 그 레이어 한 장만 blit — 벽돌이 수천 개 죽은
후반에도 프레임당 페인트 비용이 늘지 않음. 레이어는 오버레이 리사이즈나
재시작(`dead_bricks`가 줄어듦)에 자동 재생성. erase 색은 `Brick.bg`(검출 시
샘플링한 local 배경색, 없으면 전역 배경색), 사각형은 검출 시 미리 계산된
`Brick.erase`(halo 포함 + 이웃 중간점 클램프, 검출기가 항상 채움)를 사용하고,
antialiasing은 꺼서 가장자리 블렌딩 잔상을 방지. 매 틱의 repaint는
`update(QRegion)`으로 실제 바뀐 영역(패들/공의 이전+현재 rect, 방금 죽은
벽돌의 erase rect, 상태 텍스트가 바뀐 프레임의 상단 밴드)만 invalidate —
반투명 오버레이는 전체 update 시 Qt가 뷰포트 크기만큼 부모 콘텐츠를 매
프레임 재합성하므로, 이걸 제한해야 프레임 비용이 창 크기와 무관해짐.
WIN/LOSE 전환 프레임만 배너 때문에 전체 update. viewport에 설치한
`eventFilter`로 키 입력을 가로채고, 게임이 안 쓰는 키와 휠 스크롤, **마우스
이벤트(클릭/드래그/더블클릭/컨텍스트 메뉴)까지 전부 흡수** — ignore된
이벤트는 부모 viewport로 전파돼 PageUp/Down이 코드를 스크롤시키고, 클릭은
캐럿/라인 하이라이트를 움직여 구워 둔 erase 색과 어긋나게 하며, 더블클릭
네비게이션은 재디컴파일 → `refresh_pseudocode` 훅이 게임을 강제 종료시키므로.
액션 단축키(토글 핫키 등)는 keyPressEvent 전달 전에 디스패치되므로 여전히
동작함. TEAViewer는 스크롤바를 QAbstractScrollArea 없이 평범한 자식 위젯으로
두어 스크롤바 policy 트릭이 안 통함 — `start_game()`이 outer 위젯 아래의 모든
`QScrollBar`를 모아 overlay에 넘기고, overlay가 같은 eventFilter로 입력만
무력화함 (숨기면 viewport가 재배치되어 시작 시점 grab 기준의 brick 좌표가
어긋나므로 보이게 둠).

## 게임 메커니즘

- **발사**: `±MAX_PADDLE_ANGLE` (60°) 랜덤 각도, magnitude `base_speed*√2` 고정
- **패들 반사**: 표준 Breakout 방식. 입사 직전의 `speed = hypot(vx, vy)`를
  측정해서 패들 중심 기준 offset∈[-1,+1]을 각도로 변환:
  ```
  angle = offset * MAX_PADDLE_ANGLE
  vx = speed * sin(angle)
  vy = -speed * cos(angle)
  ```
  **magnitude를 보존하는 게 중요**. 가산식(`vx += offset*spin`)으로 만들면
  가장자리 hit이 누적될 때 magnitude가 73%까지 증가해서 "직선 느림 / 대각선
  빠름" 현상이 발생.
- **벽/벽돌 반사**: component 부호를 **set** (`±abs`, 단순 negate 아님) →
  magnitude 보존 + 이미 멀어지는 중인 공(벽에 겹쳐 스폰된 멀티볼 등)을
  도로 뒤집지 않음
- **멀티볼**: 점수 15점마다 +1 공 (최대 5개, **생존 공 기준** — 같은 프레임에
  이미 바닥으로 빠졌지만 아직 리스트에서 제거되지 않은 공은 슬롯을 차지하지
  않음). 부순 위치에서 부모 반대 방향 ± `MULTIBALL_ANGLE_NOISE` (≈14°) 각도
  노이즈, magnitude 보존. 상한에 걸려 분기가 거부되면 `next_multiball_score`를
  올리지 않고 슬롯이 빌 때까지 대기
- **속도 가속**: `speed_bricks` 카운터 × `SPEED_PER_BRICK` (max `SPEED_CAP=2.0x`).
  목숨 차감 시 가속만 리셋 (점수는 누적 보존). per-frame 이동량은
  `n_sub * sub_dt = speed_factor`로 component-uniform
- **AABB 충돌**: 반사면은 substep 이동 **전** 위치 기준으로 결정
  (`_resolve_brick_bounce`) — 공의 박스가 어느 축 바깥에 있었는지로 진입면을
  찾고, 코너 진입(두 축 다 바깥)은 실제 변위 기준 entry time이 **늦은** 축이
  맞은 면 (swept AABB). 이전 위치가 이미 두 축 모두 겹쳐 있으면(접촉 상태
  스폰, 이웃 벽돌이 substep 사이에 죽음) 진입 방향이 없으므로 침투 깊이
  최소축으로 fallback. 침투 깊이를 1차 기준으로 쓰면 안 됨: 벽돌이 가로로
  길고 얇은 토큰이라 아래에서 벽돌 끝을 스치면 수평 침투 < 수직 침투가 되어
  vx만 뒤집히고 공이 라인을 그대로 관통 (바닥면 진입의 ~3%에서 실측,
  `tests/test_game.py`의 몬테카를로가 회귀 감시). 빠른 속도에서의 터널링은
  `n_sub` substep으로 방지. 후보 벽돌은 y 정렬 밴드 인덱스(`_ensure_brick_index`,
  bisect)로 공이 걸친 1~2개 라인만 스캔 — 전체 리스트 스캔은 죽은 벽돌까지
  포함해 총 벽돌 수에 비례하므로 (벽돌 3천 개·공 5개에서 ~3.5ms/frame →
  인덱스로 ~0.04ms). 인덱스는 bricks 리스트의 identity/길이 변화에만 재빌드
  (brick 좌표는 검출 후 불변이라는 가정)
- **종료/재시작**: WIN/LOSE 시 타이머만 정지, 자동 종료 없음. 배너 +
  `[R] restart  [Esc] exit` 힌트 표시. `R` → `GameState.reset()`로 brick 전부
  alive 복원, 점수/목숨/속도/멀티볼 카운터 초기화. `Esc` → 종료

파라미터는 `game.py` / `overlay.py` 상단 상수 참고.

## 개발

### 테스트

`game.py`는 Qt 의존이 없어 일반 Python에서 직접 import 가능:

```sh
# 신택스 체크
python3 -m py_compile ida_breakout_lib/*.py ida_breakout*.py tests/*.py

# 게임 로직 단위 테스트 (충돌 면 판정, 벽 반사, 게임 플로우)
python3 -m unittest discover -s tests

# 게임 로직 smoke test
python3 -c "
from ida_breakout_lib.game import GameState, Paddle, Brick, Phase
g = GameState(width=400, height=300, paddle=Paddle(x=160, y=280))
g.bricks = [Brick(x=10, y=10, w=20, h=8)]
g.spawn_ball_on_paddle()
g.reset()
assert g.phase is Phase.READY
print('ok')
"
```

`pseudocode.py`의 검출 로직(배경색 샘플링, brick/erase rect 계산, 캐럿 필터)은
IDA 없이도 검증 가능: `QT_QPA_PLATFORM=offscreen`으로 QApplication을 띄우고
합성 픽셀 버퍼를 `grab=(bytes, w, h, dpr)` 파라미터로 주입하면 viewport grab을
우회함. dpr 값을 바꿔 HiDPI 경로도, `pseudocode.np = None`으로 pure-python
fallback 경로도 같은 방식으로 테스트 가능. viewport 탐지(`find_pseudocode_viewport`)와
`overlay.py`는 IDA의 실 위젯이 필요해서 IDA 내부에서만 검증 가능.

### 진단

`ida_breakout_lib.pseudocode` 로거가 INFO 레벨로 다음을 출력 (IDA 기본 logging
레벨에서 출력창에 보임. 조용히 하고 싶으면 `logging.getLogger("ida_breakout_lib").
setLevel(logging.WARNING)`):

- viewport 클래스/사이즈, 위젯 트리
- `viewport.grab()`의 device pixel ratio, 추출된 background colors
- 검출된 line / brick 개수
- viewport fallback 발생 시 WARNING

Brick 검출이 실패(`bricks=0`)하거나 viewport 클래스가 모르는 빌드일 때:

- `_VIEWER_CLASS_HINTS` / `_CUSTOM_CONTROL_HINTS`에 새 클래스명(부분 문자열 매칭이라
  정확한 이름을 넣어도 됨) 추가
- `sample_viewport_bg_colors`의 `min_count_pct` / `dedupe_dist` 튜닝
- `color_threshold` (기본 40) 튜닝

## 의도적 설계 결정

리팩터/추가 작업 보류 또는 일반화하지 않은 부분들:

- **`overlay.stop()` / `stop_game()` cleanup의 `try/except: pass`**: PySide6에서
  IDA가 viewport QWidget을 비결정적으로 정리하는 타이밍이 있어
  `removeEventFilter` 등이 `RuntimeError: Internal C++ object already deleted`
  를 던질 수 있음. 정상 종료 경로의 양성 케이스라 조용히 삼킴 — 트레이스가
  IDA 출력창에 뜨면 사용자에게 "토글 실패"로 보여 UX가 망가짐.
- **`widget_invisible`은 탭 닫힘/숨김을 구분하지 않음**: IDA의
  `ui_widget_invisible`은 빌드에 따라 도크 탭 전환·레이아웃 변경에서도 발화할
  수 있고, SDK가 진짜 닫힘인지 구분할 정보를 주지 않음. 그런 빌드에서는 탭
  전환만으로 게임이 종료되어 점수를 잃지만, 숨겨진 탭의 오버레이를 살려 두고
  복귀 시 복원하는 방식은 그 사이의 재디컴파일/스크롤로 grab 스냅샷이 무효화될
  위험 대비 이득이 없어 단순 종료를 유지. hide에서 발화하지 않는 빌드에서는
  원래 의도(닫힘 감지)대로만 동작.
- **창 리사이즈에 대응하지 않음**: 오버레이는 시작 시점의 viewport 크기에
  고정 (자식 QWidget은 부모 크기를 따라가지 않고, 일부러 동기화하지도 않음 —
  `resizeEvent` 핸들러나 viewport `Resize` 추적을 추가하지 말 것). 게임
  무대(brick 좌표, 배경 스냅샷)가 시작 시점 화면에 묶여 있어 리사이즈를
  따라가도 텍스트와 어긋나 의미가 없음. 진행 중인 점수 보존이 우선이라 자동
  종료도 하지 않고 게임은 그대로 진행.
