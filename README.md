# ctr — claude-token-rotator

Keep several Claude Code tokens in the macOS keychain, watch how much of each
account's 5-hour and 7-day quota is gone, and switch to the one with the most
headroom before the active account runs out.

Built for running many Claude Code sessions at once across more than one Max
subscription. If you only have one account, this does nothing useful for you.

```
$ claude-rotator list
   LABEL   ACCOUNT            PLAN  KIND   ADDED        5H    RESETS    7D    RESETS
*  work    you@example.com    max   oat    2026-09-10   89%  in 42m    27%  in 159h
   spare   alt@example.com    max   oat    2026-09-11    4%  in 3h11m   12%  in 159h

$ claude-rotator next
Switched to 'spare'. New shells will use it.
Sessions already running keep the old token. Run `claude-rotator rollover`.
```

macOS only. Python 3.8+, standard library only, no pip install.

## Running sessions keep their old token

A running `claude` process holds its token in memory. Changing the environment
variable or the keychain does nothing to it. A session parked on
`Usage limit reached · continuing automatically at 4pm` stays parked even after
you switch accounts, because it will retry with the token it started with.

The only fix is to restart that session. `ctr rollover` does it with
`/exit` followed by `claude --resume <session-id>`, so the conversation
survives. It drives [herdr](https://herdr.dev) panes and is a no-op if you
don't use herdr; everything else works standalone.

## Install

```sh
git clone https://github.com/binaryk/claude-token-rotator.git
cd claude-token-rotator
./install.sh
```

That symlinks `ctr` and `claude-rotator` (same binary) into `~/.local/bin`,
creates `~/.config/ctr` with mode 0700, and appends a guarded block to your
`~/.zshrc`:

```sh
# >>> ctr (claude-token-rotator) >>>
if [ -r "$HOME/.config/ctr/active.sh" ]; then . "$HOME/.config/ctr/active.sh"; fi
# <<< ctr (claude-token-rotator) <<<
```

Re-running `install.sh` replaces that block rather than adding a second one. It
backs the file up first, and follows a symlinked `~/.zshrc` to the real file
instead of replacing the link.

The background monitor is not installed by this script. See
[Monitoring](#monitoring).

## Add your tokens

Mint one long-lived token per account with `claude setup-token`. It opens a
browser login, so you have to run it yourself, once per account:

```sh
claude setup-token          # log in as account #1, copy the sk-ant-oat01-... it prints
claude-rotator add work     # prompts for it without echoing

claude setup-token          # log in as account #2
claude-rotator add spare
```

Three input forms:

| form | notes |
|---|---|
| `add <alias>` | prompts with echo off. Use this one. |
| `add <alias> -` or `add <alias> --token -` | reads stdin, for scripts |
| `add <alias> <token>` | works, but the token lands in `ps` and `~/.zsh_history`. `ctr` prints a warning telling you how to clear it. |

`add --from-login` registers whatever token your current interactive
`claude` login already put in the keychain, if you want that account in the
rotation too.

## Daily use

```sh
claude-rotator list             # every token with live usage and reset times
claude-rotator status --json    # same thing, machine readable
claude-rotator next             # switch to whichever token has the most headroom
claude-rotator use spare        # or pick one by name
claude-rotator rollover         # restart parked sessions on the new token (dry run)
claude-rotator doctor           # check the whole install end to end
```

`rollover` prints what it would do and touches nothing. Add `--apply` to run it
for real, and `--only <pane-id>` to do a single pane while you watch.

## How it picks

Among tokens that are usable, not parked, and below both thresholds, it takes
the lowest 5-hour utilisation, breaking ties on the 7-day number and then on the
alias. It switches when all of these hold:

- the active token crossed `switch_at_5h` (85%) or `switch_at_7d` (95%)
- some other token qualifies
- that token is at least `min_improvement` (10 points) better on the window that
  triggered
- the last switch was more than `min_switch_interval_s` (600) ago

The token it switches away from is parked, and stays parked until its own
readings drop below `recover_below_5h` (60%) and `recover_below_7d` (80%). That
keeps it from bouncing back and forth across a threshold.

**A token that is itself over the threshold is not a refuge.** Switching onto a
token at 99% strands you: the good token is now parked and the new one dies
within minutes. So `ctr` reports "no headroom anywhere" instead.

**Unless the active token is actually dead.** If it comes back `rejected`, any
live token beats it, so the thresholds are dropped for that one decision. A
token with 10% of its hour left is worth having.

**One failed probe is not a signal.** An unauthenticated request to the usage
endpoint returns 429, so a 429 proves nothing about your token. `ctr` needs two
consecutive failures before a probe error can trigger a switch. A measured
`rejected` still fires immediately.

Override any of these in `~/.config/ctr/config.json`:

```json
{
  "switch_at_5h": 85.0,
  "switch_at_7d": 95.0,
  "recover_below_5h": 60.0,
  "recover_below_7d": 80.0,
  "min_improvement": 10.0,
  "min_switch_interval_s": 600,
  "cache_ttl_s": 60,
  "http_timeout_s": 20,
  "auto_rollover": false
}
```

## Monitoring

```sh
claude-rotator install-monitor          # launchd, every 5 minutes
claude-rotator monitor --once           # one pass, by hand
claude-rotator uninstall-monitor
```

The agent is `com.binarcode.ctr-monitor`, logging to `~/Library/Logs/ctr.log`.
An uneventful tick writes nothing, so a quiet log is the healthy state. Check it
with `launchctl list com.binarcode.ctr-monitor` (`LastExitStatus = 0`) or with
`claude-rotator doctor`.

When it switches, you get a macOS notification and a log line. It will not touch
your running sessions unless you pass `--auto-rollover`, which is off by
default. Watch a manual `rollover` work before you turn that on.

## Where the tokens live

In the macOS keychain, one generic-password item per alias, service name
`ctr:<alias>`:

```sh
security find-generic-password -s "ctr:work" -w
```

Nothing else on disk holds a secret. `~/.config/ctr` has three files, all 0600:

- `tokens.json`: alias, account, plan, date. No token.
- `state.json`: parked tokens, last switch, cached usage percentages.
- `active.sh`: a `security find-generic-password` call, not a value.

`active.sh` reads the keychain each time your shell sources it, so the file is
inert on its own and safe to back up or sync:

```sh
CTR_ACTIVE_LABEL='work'
_ctr_tok="$(security find-generic-password -s 'ctr:work' -w 2>/dev/null || true)"
if [ -n "$_ctr_tok" ]; then export CLAUDE_CODE_OAUTH_TOKEN="$_ctr_tok"; fi
unset _ctr_tok
```

Tokens never appear in `argv`, so they never show up in `ps`. Writes go in on
stdin: `security add-generic-password ... -w` with no value prompts twice and
reads both from stdin. (Feeding it the secret only once stores an **empty**
password and still exits 0, which is worth knowing if you script `security`
yourself.)

Usage probes send your token and the word "hi" to `api.anthropic.com`. Nothing
goes anywhere else, and no log or config file ever contains more than the first
six characters of a token.

`ctr` does not touch the `Claude Code-credentials` keychain item that your
interactive login uses. It only sets an environment variable, so `claude
/login` keeps behaving normally.

## How usage is measured

Claude Code reads quota from `GET /api/oauth/usage`. That endpoint needs the
`user:profile` scope, and a `claude setup-token` token does not have it:

```json
{"error": {"type": "permission_error",
  "message": "OAuth token does not meet scope requirement user:profile"}}
```

So for long-lived tokens `ctr` sends a `POST /v1/messages` with
`max_tokens: 0`, which returns 200 with empty content and a full set of
rate-limit headers:

```
anthropic-ratelimit-unified-5h-utilization: 0.51
anthropic-ratelimit-unified-5h-reset: 1789483200
anthropic-ratelimit-unified-7d-utilization: 0.18
```

It costs 8 input tokens and 0 output tokens. The two sources agree: the same
account read 50.0/18.0 from the usage endpoint and 0.51/0.18 from the headers
minutes apart, so the header values are fractions of the same numbers. `ctr`
normalises both to percentages and remembers which one worked for each token,
so a setup-token costs one request per check instead of two.

An interactive-login token has the scope, so it uses the cheaper endpoint.

## Troubleshooting

**`claude` hangs and never answers.** You have `ANTHROPIC_API_KEY` set. It
overrides `CLAUDE_CODE_OAUTH_TOKEN` and then hangs rather than failing. Unset
it.

**`claude` says "Not logged in · Please run /login".** Same shape, different
variable: `ANTHROPIC_AUTH_TOKEN` is set. Unset it.

`doctor` checks both, and `active.sh` warns once to stderr when it sees either.

**`claude auth status` says I'm logged in, but calls fail.** It reports
`loggedIn: true` for any string at all, including a made-up one. It is not a
health check. Use `claude-rotator status`.

**A new terminal doesn't pick up the token.** `~/.zshrc` runs for interactive
shells only. Scripts and launchd jobs need to source
`~/.config/ctr/active.sh` themselves.

**`rollover` skips everything.** That is usually correct. It refuses a pane when
it is your own (`$HERDR_PANE_ID`), when the tab is labelled BOSS, when the agent
is `working`, when the agent is not `claude`, and when the viewport shows no
usage-limit line. It also refuses every pane if it cannot read the herdr tab
labels, because it would rather do nothing than restart the wrong session. Each
skip prints its reason.

## Uninstall

```sh
claude-rotator uninstall-monitor
claude-rotator remove work          # per alias; deletes the keychain item too
rm -rf ~/.config/ctr ~/.local/bin/ctr ~/.local/bin/claude-rotator
```

Then delete the guarded block from `~/.zshrc`, markers included.

## Development

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -t .   # 435 tests
python3 -m py_compile $(git ls-files "src/ctr/*.py")
```

Tests hit no network, no keychain, no launchctl, no osascript, and never read
your real config. Every subprocess call sits behind a module-level seam the
tests replace, and the parsers are pure functions tested against response
fixtures captured from the live API (`tests/fixtures/`).

`src/ctr/selector.py` has no I/O and no clock reads at all. Time arrives as a
`now` argument, which is what makes the switching rules testable.

## License

MIT. See [LICENSE](LICENSE).
