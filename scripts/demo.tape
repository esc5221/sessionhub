# Animated README demo.  Regenerate with:  vhs scripts/demo.tape
# Requires: brew install vhs   and   ./scripts/make_demo.py run first
# (scripts/capture.sh does both).

Output docs/demo.gif

Set Shell bash
Set FontFamily "Menlo"
Set FontSize 13
Set Width 1180
Set Height 620
Set Padding 24
Set Margin 16
Set BorderRadius 8
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
