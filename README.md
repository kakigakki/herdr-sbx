# herdr-sbx

A [herdr](https://herdr.dev) plugin that opens Claude Code inside the [Docker Sandbox](https://docs.docker.com/ai/sandboxes/) (`sbx`) of the current workspace's repository, in a split pane, with herdr's agent detection working.

## Why

herdr recognizes an agent by the foreground process of a pane. When Claude Code runs inside an `sbx` microVM, that process is `sbx`, so the pane shows no agent and no idle / working / done / blocked state. herdr's documented fix is to set `HERDR_AGENT=claude` on the host-side wrapper command ([docs](https://herdr.dev/tui/docs/agents/#vms-and-sandbox-wrappers), [herdr#2181](https://github.com/herdrdev/herdr/issues/2181)). This plugin does that for you and finds the right sandbox.

## What it does

Press the key in any pane of a repository:

1. The repository root is taken from the focused pane's working directory (a linked git worktree resolves to its main checkout).
2. Sandboxes whose first workspace in `sbx ls` is that root are the candidates.
   - One: it opens right away.
   - Several: an fzf picker opens. Running ones come first, and the one you picked last time in this workspace (by label) comes first of all.
   - None: a notification says so. **The plugin never creates sandboxes** (create them with `sbx` or your project's own script).
3. The focused pane is split (to the right when it is wide, otherwise downward), and the new pane, named after the sandbox, runs `HERDR_AGENT=claude sbx run --name <sandbox>`.

## Sandbox summary in the sidebar (optional)

`python3 -m herdr_sbx report` puts a `$sbx` workspace token on the workspace whose checkout is the directory a sandbox was created from (the first workspace in `sbx ls`), listing all of that directory's sandboxes:

```text
relation-1● 2⡿1?1  relation-2○
```

`●` running / `○` stopped, then the number of interactive Claude sessions attached from the host (`sbx run --name …` processes), how many are working (`⡿`) and how many wait for you (`?`). States come from herdr for panes opened by this plugin. The VM is never looked into, so this does not keep sandboxes awake; `claude -p` running headless inside a VM is not counted.

herdr plugins have no timer, so run `report` from your own periodic job (e.g. a launchd agent or cron every minute). Tokens expire after 5 minutes when the job stops.

```sh
cd /path/to/herdr-sbx && python3 -m herdr_sbx report
```

Show it in `~/.config/herdr/config.toml`, e.g. at the end of a spaces row, and on Claude's agent rows (the pane token `sbx` is set on panes this plugin opens):

```toml
[ui.sidebar.spaces]
rows = [["state_icon", "workspace"], ["branch", "git_status", { token = "$sbx", fg = "#83a598" }]]

[ui.sidebar.agents.rows_by_agent]
claude = [["state_icon", "workspace", "tab", { token = "$sbx", fg = "#83a598" }], ["terminal_title_stripped"]]
```

## Requirements

- herdr 0.9.5 or later, `sbx`, `fzf`, `git`, `python3` (standard library only)
- macOS or Linux

## Install

```sh
herdr plugin install kakigakki/herdr-sbx
```

Bind a key in `~/.config/herdr/config.toml` (`prefix+s` is herdr's settings key by default):

```toml
[[keys.command]]
key = "prefix+shift+s"
type = "plugin_action"
command = "herdr-sbx.open"
description = "open Claude in this repo's sandbox"
```

## Limitations

- State comes from reading the screen, so Claude's subagents inside the VM do not get their own rows.
- herdr's Claude integration hook cannot reach the host's herdr socket from inside the VM, so herdr does not learn the session id (no session resume through herdr). Do not mount `herdr.sock` into the sandbox: it has no caller authorization, and an agent with it can drive every pane on the host.
- Each run opens a new Claude session in the sandbox (`sbx run --name` behavior).

## Development

```sh
herdr plugin link "$PWD"
python3 -m unittest discover -s tests
```

Tests run the entry points as subprocesses with fake `herdr`, `sbx` and `fzf` first on `PATH`.

## License

MIT
