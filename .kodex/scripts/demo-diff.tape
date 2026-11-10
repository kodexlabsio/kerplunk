# Records docs/images/demo-diff.gif: konstruct diff on a branch of the example project.
# Run from the repository root after `uv run .kodex/scripts/prepare_example.py`:
#   .kodex/scripts/demo_diff_setup.sh && vhs .kodex/scripts/demo-diff.tape
Output docs/images/demo-diff.gif

Set Shell "bash"
Set FontSize 14
Set Width 1300
Set Height 600
Set Padding 18
Set Theme "Builtin Dark"
Set TypingSpeed 40ms

Hide
Type "export PATH=$PWD/.venv/bin:$PATH PS1='$ ' && cd /tmp/konstruct-diff-demo && clear"
Enter
Sleep 1s
Show

Type "git diff --stat"
Enter
Sleep 2s

Type "konstruct diff -q"
Enter
Sleep 9s
