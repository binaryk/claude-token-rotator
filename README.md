# ctr

You have more than one Claude account. When one runs out, `ctr` switches to
another, and the Claude windows you already have open keep working.

![ctr ui](assets/ctr-ui.svg)

## What it does

- **Shows** how much each account has used, and when it resets.
- **Switches** accounts with one command. Open Claude windows switch too.
- **Watches** in the background and switches for you before an account runs out.
- **Keeps your keys safe** in the Mac keychain (on Linux: the system keyring).

Works on a Mac and on Linux (tested on Arch / Omarchy).

## Install it with Claude

Copy this and paste it into Claude Code:

```text
Install ctr for me: https://github.com/binaryk/claude-token-rotator

1. Download it to ~/Sites/claude-token-rotator and run ./install.sh
2. Run: python3 -m pip install --user textual
3. Run: ctr doctor, and fix what it says if you can.
4. Stop and tell me to do this myself in my own terminal, once for each Claude account:
       claude setup-token     (log in, then copy the code it shows)
       ctr add <a name>       (paste the code)
   Never ask me to paste that code here, and never show it.
5. When I say I'm done, run ctr status, then switch to the account with the most left.
6. Tell me to open a new terminal tab and run: ctr ui
```

## Or install it yourself

```sh
git clone https://github.com/binaryk/claude-token-rotator.git
cd claude-token-rotator
./install.sh
python3 -m pip install --user textual
```

Then, once for each account:

```sh
claude setup-token     # log in, copy the code
ctr add work           # paste it
```

## Use it

| Type this | It does this |
|---|---|
| `ctr ui` | Opens the screen above. Arrows to pick, Enter to switch, q to quit. |
| `ctr status` | Shows how much each account has used. |
| `ctr switch work` | Switches to the account called `work`. |
| `ctr next` | Switches to the account with the most left. |
| `ctr install-monitor` | Switches for you when an account gets full. |
| `ctr auto off` | Stops switching for you. `ctr auto on` starts it again. |
| `ctr switch --restore-login` | Puts back your normal Claude login. |
| `ctr doctor` | Checks that everything is set up right. |

## Good to know

- Open Claude windows switch on their next message, within about 30 seconds.
- A Claude window started with `CLAUDE_CODE_OAUTH_TOKEN` set will not switch.
  Close it and open it again.
- Want the details? Read [HOW-IT-WORKS.md](HOW-IT-WORKS.md).

## On Linux

The same commands work. What is different underneath:

- Keys go to the system keyring through `secret-tool` (libsecret). The keyring
  must be unlocked for your ssh logins and for systemd user services. Check with
  `printf x | secret-tool store --label=t t t && secret-tool lookup t t && secret-tool clear t t`.
- `ctr switch` writes Claude's own login file, `~/.claude/.credentials.json`.
  It keeps a copy of the file it replaced next to it (`.ctr-prev`), and your
  normal login in `.ctr-login`. Open Claude windows follow a switch, as on a Mac.
- `ctr install-monitor` installs a systemd user timer (`ctr-monitor.timer`,
  every 5 minutes). To keep it running after you log out:
  `loginctl enable-linger $USER`. Logs: `journalctl --user -u ctr-monitor`
  and `~/.local/state/ctr/ctr.log`.
- `install.sh` adds its line to `~/.bashrc` (and `~/.zshrc` if your shell is zsh).
- Arch's `containerd` package also installs a `/usr/bin/ctr`. Put
  `~/.local/bin` first in your `PATH`, or call `claude-rotator` instead.
  `ctr doctor` tells you which one you are running.
- For `ctr ui`: `sudo pacman -S python-textual python-rich` (Arch blocks `pip install --user`).

## License

MIT
