# Records docs/images/demo-cli.gif: build the example graph, then ask what a change would break.
# The example is copied to /tmp/jaffle_shop, so the paths shown are the same on every machine.
# Run from the repository root after `uv sync` and `uv run .kodex/scripts/prepare_example.py`:
#   vhs .kodex/scripts/demo-cli.tape
Output docs/images/demo-cli.gif

Set Shell "bash"
Set FontSize 15
Set Width 1280
Set Height 980
Set Padding 18
Set Theme "Builtin Dark"
Set TypingSpeed 40ms

Hide
Type "export PATH=$PWD/.venv/bin:$PATH PS1='$ ' && rm -rf /tmp/jaffle_shop && cp -R examples/jaffle_shop /tmp/jaffle_shop && cd /tmp/jaffle_shop && rm -rf konstruct-out && clear"
Enter
Show

Type "konstruct build"
Enter
Sleep 4s

Type "konstruct impact stg_payments.amount"
Enter
Sleep 7s
