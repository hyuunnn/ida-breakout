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

- **시작/종료**: `Ctrl-Alt-K` 또는 우클릭 메뉴 *"ida-breakout: Start brick break"*
- **이동**: `←` / `→` (또는 `h`/`l`, `a`/`d`)
- **발사**: `Space`
- **재시작**: `R` (WIN/LOSE 화면에서)
- **종료**: `Esc`

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
3. 행/열 단위로 ink 스캔 → 연속 영역을 brick으로 묶음. brick마다 주변 non-ink
   픽셀의 최빈색을 `Brick.bg`(local 배경색)로 저장 — 라인/토큰 하이라이트 위의
   brick도 제 색으로 지워짐. erase 사각형(`Brick.erase`)도 이때 미리 계산:
   ink보다 ~2 logical px 크게 잡아 anti-aliasing halo를 덮되, 이웃 라인/토큰과의
   gap **중간점**에서 클램프 — 지울 때 옆 라인 글자를 깎지 않음.
4. HiDPI는 device pixel ratio로 device→logical 변환. 좌상단은 floor,
   우하단은 ceil — 양쪽 다 절삭하면 오른쪽/아래 모서리가 1px 덜 지워져 잔상.
5. 자식 위젯이 차지하는 영역(스크롤바, 헤더 등)은 마스킹
6. **캐럿 방어**: 텍스트 캐럿은 포커스가 있을 때만 그려지는 얇은 세로 막대라,
   grab에는 찍히지만 오버레이가 포커스를 가져가면 화면에서 사라짐 → 공이
   튕기는 "투명 벽"이 됨. 이중 방어: (a) `start_game()`이 grab 전에
   `clearFocus()`로 캐럿을 숨기고, (b) 검출 단계에서 폭 ≤ ~2 logical px이면서
   ink 색이 2가지 이하인 단색 막대(캐럿, 인덴트 가이드)를 brick에서 제외.
   진짜 글리프는 anti-aliasing 때문에 항상 3가지 이상의 ink 색을 가짐.

### Viewport 식별

Pseudocode 외곽 widget은 `TEAViewer`. 그 안의 가장 큰 visible child(viewport
면적의 50% 이상)가 실제 텍스트 surface. `find_pseudocode_viewport()`가 이걸
찾고, 못 찾으면 outer 자체를 fallback으로 사용. 모르는 IDA 빌드를 대응하기
위해 viewport 탐지 진단 로그를 `logging.getLogger(__name__).info(...)`로 남김
(자세한 내용은 "진단" 섹션).

### Plugmod 라이프사이클

`breakout_plugin_t(PLUGIN_MULTI)` → `breakout_plugmod_t`. plugmod에서:

- `_StartGameHandler.update()`: `BWN_PSEUDOCODE` 위젯에서만 enable
- `_UIHooks.widget_invisible`: pseudocode 탭 닫힘 감지 → 자동 종료
- `_UIHooks.finish_populating_widget_popup`: 우클릭 메뉴에 액션 부착
- `_HexraysHooks.refresh_pseudocode`: F5 재디컴파일 감지 → 자동 종료

`stop_game()` 마지막에 `ida_kernwin.activate_widget(twidget, True)`을 호출하는
이유: overlay가 `deleteLater()`로 사라진 직후 IDA의 current widget이 일시적으로
None이나 다른 도크로 빠지면서 다음 핫키 입력이 액션 `update()`에서
`AST_DISABLE_FOR_WIDGET`으로 평가되어 1~2번 무시되는 현상이 있음. 명시적으로
pseudocode TWidget으로 포커스를 복귀시켜 해결.

### Overlay 투명 자식 QWidget

`BreakoutOverlay`는 viewport의 자식 QWidget. `WA_TranslucentBackground`로 텍스트가
비쳐 보임. `paintEvent`에서는 부서진 brick 영역(`state.dead_bricks`)만 사각형으로
erase — 매 프레임 모든 brick을 필터링하지 않음. erase 색은 `Brick.bg`(검출 시
샘플링한 local 배경색, 없으면 전역 배경색), 사각형은 검출 시 미리 계산된
`Brick.erase`(halo 포함 + 이웃 중간점 클램프, 검출기가 항상 채움)를 사용하고,
antialiasing은 꺼서 가장자리 블렌딩 잔상을 방지. viewport에 설치한
`eventFilter`로 키 입력을 가로채고 휠 스크롤은 흡수해서 게임 중 코드가 안
밀리게 함.

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
- **벽/벽돌 반사**: component flip만 → magnitude 보존
- **멀티볼**: 점수 15점마다 +1 공 (최대 5개). 부순 위치에서 부모 반대 방향
  ± `MULTIBALL_ANGLE_NOISE` (≈14°) 각도 노이즈, magnitude 보존
- **속도 가속**: `speed_bricks` 카운터 × `SPEED_PER_BRICK` (max `SPEED_CAP=2.0x`).
  목숨 차감 시 가속만 리셋 (점수는 누적 보존). per-frame 이동량은
  `n_sub * sub_dt = speed_factor`로 component-uniform
- **AABB 충돌**: 침투 깊이 최소축으로 면 결정. 빠른 속도에서의 터널링은
  `n_sub` substep으로 방지
- **종료/재시작**: WIN/LOSE 시 타이머만 정지, 자동 종료 없음. 배너 +
  `[R] restart  [Esc] exit` 힌트 표시. `R` → `GameState.reset()`로 brick 전부
  alive 복원, 점수/목숨/속도/멀티볼 카운터 초기화. `Esc` → 종료

파라미터는 `game.py` / `overlay.py` 상단 상수 참고.

## 개발

### 테스트

`game.py`는 Qt 의존이 없어 일반 Python에서 직접 import 가능:

```sh
# 신택스 체크
python3 -m py_compile ida_breakout_lib/*.py ida_breakout*.py

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

`pseudocode.py` / `overlay.py`는 PySide6와 IDA의 실 위젯이 필요해서
IDA 내부에서만 실제 동작 검증 가능.

### 진단

`ida_breakout_lib.pseudocode` 로거가 INFO 레벨로 다음을 출력 (IDA 기본 logging
레벨에서 출력창에 보임. 조용히 하고 싶으면 `logging.getLogger("ida_breakout_lib").
setLevel(logging.WARNING)`):

- viewport 클래스/사이즈, 위젯 트리
- `viewport.grab()`의 device pixel ratio, 추출된 background colors
- 검출된 line / brick 개수
- viewport fallback 발생 시 WARNING

Brick 검출이 실패(`bricks=0`)하거나 viewport 클래스가 모르는 빌드일 때:

- `_KNOWN_OUTER_VIEWERS` / `_VIEWER_CLASS_HINTS`에 새 클래스명 추가
- `sample_viewport_bg_colors`의 `min_count_pct` / `dedupe_dist` 튜닝
- `color_threshold` (기본 40) 튜닝

## 의도적 설계 결정

리팩터/추가 작업 보류 또는 일반화하지 않은 부분들:

- **`game.py`의 AABB 4분기 충돌 처리**: 위/아래/좌/우 분기가 패턴이 비슷해
  보이지만 `and ball.vy > 0` 가드, `else` rescue 분기, `==` 매칭 순서 등
  미묘한 함정이 있어 합치지 않음. 단위 테스트가 충분히 갖춰진 뒤에야
  안전하게 리팩터 가능.
- **`overlay.stop()` / `stop_game()` cleanup의 `try/except: pass`**: PySide6에서
  IDA가 viewport QWidget을 비결정적으로 정리하는 타이밍이 있어
  `removeEventFilter` 등이 `RuntimeError: Internal C++ object already deleted`
  를 던질 수 있음. 정상 종료 경로의 양성 케이스라 조용히 삼킴 — 트레이스가
  IDA 출력창에 뜨면 사용자에게 "토글 실패"로 보여 UX가 망가짐.
- **창 리사이즈 시 자동 종료하지 않음**: 리사이즈하면 brick 좌표가 텍스트와
  어긋나지만 진행 중인 점수를 보존하는 쪽이 우선. 게임은 그대로 진행.
