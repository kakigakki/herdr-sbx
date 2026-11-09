# herdr-sbx

A [herdr](https://herdr.dev) plugin that opens Claude Code inside the [Docker Sandbox](https://docs.docker.com/ai/sandboxes/) (`sbx`) of the current workspace's repository, in a herdr workspace of its own, with herdr's agent detection working.

## Why

herdr recognizes an agent by the foreground process of a pane. When Claude Code runs inside an `sbx` microVM, that process is `sbx`, so the pane shows no agent and no idle / working / done / blocked state. herdr's documented fix is to set `HERDR_AGENT=claude` on the host-side wrapper command ([docs](https://herdr.dev/tui/docs/agents/#vms-and-sandbox-wrappers), [herdr#2181](https://github.com/herdrdev/herdr/issues/2181)). This plugin does that for you and finds the right sandbox.

## What it does

Each sandbox gets a herdr workspace of its own, named after the sandbox, so the Spaces sidebar lists the sandboxes in use and herdr's own agent states show how many Claude sessions each has and which are working or waiting for you.

Press the key:

- **In a repository's workspace**: the repository root is taken from the focused pane's working directory (a linked git worktree resolves to its main checkout), and Claude sandboxes whose first workspace in `sbx ls` is that root are the candidates.
  - One: it is used. Several: an fzf picker opens, running ones first, and the one you picked last time in this workspace first of all. None: a notification says so. **The plugin never creates sandboxes** (create them with `sbx` or your project's own script).
  - If the sandbox already has its workspace, herdr switches to it. Otherwise the workspace is created and Claude starts in it with `HERDR_AGENT=claude sbx run --name <sandbox>`.
- **In a sandbox's workspace**: the focused pane is split (to the right when wide, otherwise downward) and one more Claude starts in the same sandbox.

When Claude exits normally, its pane closes (if `sbx run` fails, the pane stays so you can read the error); when the last one closes, the workspace goes away. The sandbox itself keeps running.

The workspace's working directory is an empty directory named after the sandbox in the plugin's state dir, so plugins that name workspaces after their directory (such as auto-namer) keep the sandbox's name. The plugin recognizes its workspaces by a workspace token (or, if that is lost, by that directory), not by their label.

## Opening a sandbox by name (`start`)

A host command that would run `sbx run --name <sandbox>` in its own pane (a project's "open the sandbox" script, for example) can get what the key does instead:

```sh
python3 -m herdr_sbx start <sandbox>
```

The sandbox's workspace is focused, or created with Claude started in it, and the command returns. It exits with **3** when there is no Claude sandbox of that name (or no `sbx`), so the caller can run `sbx run` itself.

To catch such commands without changing them, put a small `sbx` wrapper first on `PATH` that hands exactly `sbx run --name <sandbox>`, typed at a terminal in a herdr pane, to `start` (falling back to the real `sbx` on exit 3) and passes everything else to the real `sbx`. Skip it when `HERDR_AGENT` is set: that is this plugin's own `sbx run`.

## Starting Claude with a prompt from scripts (`launch`)

Scripts that used to start Claude on the host (a PR review, a watcher, a CI fix) can start it in a sandbox instead:

```sh
python3 -m herdr_sbx launch --repo <dir> --label "🔍app#7" \
  [--setup-file setup.sh] --prompt-file left.md [--prompt-file right.md]
```

- Picks the repository's Claude sandbox without asking (running first, then by name).
- Runs `setup.sh` inside the sandbox first (`sbx exec`, e.g. to check out a PR into a worktree in the VM); its last line of output replaces `{{WORKDIR}}` in the prompt files.
- Opens a workspace with that label and one Claude per prompt file (`sbx run --name <sandbox> -- "<prompt>"`), the second one split to the right.
- Exits with **3** when the repository has no sandbox, so the caller can fall back to running Claude another way. Other failures are shown as notifications (and printed to stderr).

When only the result matters (for example an agent waiting for a review file), add `--headless --log <file> [--fetch <file>]...` with a single prompt file: no workspace opens; `claude -p` runs in the sandbox in the background, its output goes to the log, and each `--fetch` file is copied from the VM to the same path on the host when it ends. A file the run did not write becomes a one-line note pointing at the log, so a waiting caller is never left hanging.

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
- Only sandboxes with an open workspace appear in the sidebar; use the picker or `sbx ls` for the others.
- herdr's Claude integration hook cannot reach the host's herdr socket from inside the VM, so herdr does not learn the session id (no session resume through herdr). Do not mount `herdr.sock` into the sandbox: it has no caller authorization, and an agent with it can drive every pane on the host.

## Development

```sh
herdr plugin link "$PWD"
python3 -m unittest discover -s tests
```

Tests run the entry points as subprocesses with fake `herdr`, `sbx` and `fzf` first on `PATH`.

## License

MIT
