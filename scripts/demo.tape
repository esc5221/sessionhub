# Animated README demo.  Regenerate with:  vhs scripts/demo.tape
# Requires: brew install vhs   and   ./scripts/make_demo.py run first
# (scripts/capture.sh does both).

Output docs/demo.gif

Set Shell bash
Set FontFamily "Menlo"
Set FontSize 26
Set Width 2360
Set Height 1320
Set Padding 48
Set WindowBar Colorful
Set WindowBarSize 80
Set BorderRadius 16
Set Theme "TokyoNight"
Set TypingSpeed 45ms
Set PlaybackSpeed 1

Hide
Type "export SESSIONHUB_CONFIG_DIR=/tmp/demo/config FORCE_COLOR=1 PS1='❯ '" Enter
Type "clear" Enter
Show

Sleep 700ms

Type "sessionhub search 'connection pool'" Sleep 400ms Enter
Sleep 2600ms

Type "sessionhub show 8d721671" Sleep 400ms Enter
Sleep 3200ms

Type "sessionhub recent -d 14" Sleep 400ms Enter
Sleep 3000ms
