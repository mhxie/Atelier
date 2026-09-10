# Scheduled routine shell environment

`scripts/routine_adapter.py` sets `ZDOTDIR` here before starting headless
Codex. Keep zsh startup dotfiles out of this directory. This stops unattended
model shell commands from loading interactive aliases, overriding `$OV`, or
inheriting credentials exported by a personal `~/.zshrc`.
