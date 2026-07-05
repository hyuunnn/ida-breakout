# ida-breakout

<p align="center">
  <img src="images/logo.png" alt="ida-breakout logo" width="720">
</p>

**English** | [한국어](README_ko.md)

Turn the IDA Pro Pseudocode view into a Breakout game. Variable names,
keywords, and numbers in the decompiled C source become bricks you smash
with the ball; the listing stays visible underneath while you play.

One hotkey turns the function in front of you into a playfield. Press it
again and you're back to your decompile.

Inspired by [vim-game-code-break](https://github.com/johngrib/vim-game-code-break).

## Requirements

- IDA Pro 9.0 or later
- Hex-Rays Decompiler license
- PySide6 (bundled with IDA 9.x)

## Installation

Clone the repo and symlink it into IDA's plugin directory:

```sh
git clone https://github.com/hyuunnn/ida-breakout.git
ln -s "$(pwd)/ida-breakout" ~/.idapro/plugins/ida-breakout
```

Restart IDA. The plugin auto-loads via `ida_breakout_entry.py`.

## Usage

![ida-breakout in action](images/image1.png)

In any Pseudocode view (the `F5` decompile output):

| Action            | Key                                                                 |
| ----------------- | ------------------------------------------------------------------- |
| Start game        | `Ctrl-Alt-K` *or* right-click → "ida-breakout: Start brick break"      |
| Move paddle       | `←` / `→` (or `h`/`l`, `a`/`d`)                                     |
| Launch ball       | `Space`                                                             |
| Restart           | `R` (after WIN / LOSE)                                              |
| Quit              | `Esc` or `Ctrl-Alt-K` (the overlay swallows right-clicks mid-game) |

The game runs as a transparent overlay on the decompiled function.
Bricks are extracted from the actually-rendered text pixels — what you
see is what you smash.

Mechanics at a glance:

- Standard Breakout paddle physics: ball speed magnitude is preserved
  across bounces; the paddle controls direction (angle), not speed.
- Multiball: every 15 points spawns an extra ball (max 5).
- Speed ramps up gradually as bricks break (capped at 2.0x).
- WIN / LOSE shows a banner with `[R] restart   [Esc] exit` — no
  auto-close.

## Troubleshooting

If the hotkey does nothing or IDA warns "no ink detected", the pixel-based
brick detector likely doesn't recognize your IDA build or color theme yet.
The plugin logs diagnostics to IDA's Output window (`ida_breakout_lib`
logger, INFO level): the detected viewport class, sampled background
colors, and line/brick counts. Please paste those lines into a GitHub
issue — unsupported viewport classes are usually a one-line fix.

To silence the logs, run this in IDA's Python console:

```python
import logging
logging.getLogger("ida_breakout_lib").setLevel(logging.WARNING)
```

