"""Find the sbx sandbox of the focused pane's repository and open Claude in it in a split pane.

herdr recognizes an agent by the pane's foreground process, which is `sbx` here, so the
command carries HERDR_AGENT=claude (https://herdr.dev/tui/docs/agents/#vms-and-sandbox-wrappers).
"""
import json
import os
import re
import shlex
import subprocess
import sys
import time

PLUGIN_ID = "herdr-sbx"
# sbx's own naming rule. The name is typed into a host shell, so anything else is refused.
SANDBOX_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]+")
# One sidebar row per sandbox ($sbx1 … $sbx6): a row whose tokens are unset is not drawn.
REPORT_ROWS = 6


class Abort(Exception):
    """A reason shown to the user as a herdr notification."""


def herdr(*args, raw=False):
    out = subprocess.run(["herdr", *args], capture_output=True, text=True)
    if out.returncode != 0:
        raise Abort(f"herdr {args[0]} {args[1]}: {(out.stderr or out.stdout).strip()[:200]}")
    if raw:
        return out.stdout
    try:
        return json.loads(out.stdout)["result"]
    except (ValueError, KeyError):
        raise Abort(f"herdr {args[0]} {args[1]}: unexpected output") from None


def notify(body):
    subprocess.run(["herdr", "notification", "show", PLUGIN_ID, "--body", body], capture_output=True)


def context():
    return json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON") or "{}")


def focused_cwd(ctx):
    # foreground_cwd follows a `cd` inside the pane; the context only has the pane's start cwd.
    pane_id = ctx.get("focused_pane_id")
    if pane_id:
        try:
            pane = herdr("pane", "get", pane_id)["pane"]
            return pane.get("foreground_cwd") or pane.get("cwd")
        except (Abort, KeyError):
            pass
    return ctx.get("focused_pane_cwd")


def repo_root(cwd):
    """The main checkout of cwd's repository, also from a linked worktree."""
    def rev_parse(*args):
        out = subprocess.run(["git", "-C", cwd, "rev-parse", *args], capture_output=True, text=True)
        return out.stdout.strip() if out.returncode == 0 else None

    common = rev_parse("--path-format=absolute", "--git-common-dir")
    if common is None:
        return None
    if os.path.basename(common) == ".git":
        return os.path.realpath(os.path.dirname(common))
    # A submodule keeps its git dir under the parent's .git/modules.
    top = rev_parse("--show-toplevel")
    return os.path.realpath(top) if top else None


def sandboxes():
    try:
        out = subprocess.run(["sbx", "ls", "--json"], capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        raise Abort("sbx is not installed") from None
    except subprocess.TimeoutExpired:
        raise Abort("sbx ls did not respond") from None
    if out.returncode != 0:
        raise Abort(f"sbx ls failed: {out.stderr.strip()[:200]}")
    try:
        found = json.loads(out.stdout or "[]")
    except ValueError:
        found = None
    if not isinstance(found, list):
        raise Abort("sbx ls --json: unexpected output")
    return found


def primary_workspace(sandbox):
    workspaces = sandbox.get("workspaces") or []
    if not workspaces:
        return None
    path = workspaces[0]
    for suffix in (":ro", ":rw"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
    return os.path.realpath(path)


def candidates(root):
    found = [s for s in sandboxes() if s.get("agent") == "claude" and primary_workspace(s) == root]
    for s in found:
        if not SANDBOX_NAME.fullmatch(str(s.get("name"))):
            raise Abort(f"unexpected sandbox name: {s.get('name')!r}")
    return found


def wait_shell_ready(pane, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if herdr("pane", "read", pane, "--lines", "3", "--source", "visible", raw=True).strip():
                return
        except Abort:
            pass
        time.sleep(0.2)


def split_direction(origin):
    try:
        rect = next(p["rect"] for p in herdr("pane", "layout", "--pane", origin)["layout"]["panes"]
                    if p["pane_id"] == origin)
        # Terminal cells are roughly twice as tall as wide.
        return "right" if rect["width"] > rect["height"] * 2.2 else "down"
    except (Abort, KeyError, TypeError, StopIteration):
        return "down"


def open_pane(origin, root, name):
    split = herdr("pane", "split", origin, "--direction", split_direction(origin), "--cwd", root, "--focus")
    pane = split["pane"]["pane_id"]
    try:
        herdr("pane", "rename", pane, name, raw=True)
        # The pane name can be overwritten (e.g. by auto-naming plugins); the token is what report reads.
        herdr("pane", "report-metadata", pane, "--source", PLUGIN_ID, "--token", f"sbx={name}", raw=True)
    except Abort:
        pass  # cosmetic: the pane is already split, so still start Claude in it
    wait_shell_ready(pane)
    # Clearing the token when sbx exits keeps a later host-side agent in this pane from being
    # counted as the sandbox's.
    herdr("pane", "run", pane, f"HERDR_AGENT=claude sbx run --name {shlex.quote(name)}; "
          f'herdr pane report-metadata "$HERDR_PANE_ID" --source {PLUGIN_ID} --clear-token sbx', raw=True)


def state_dir():
    path = os.environ.get("HERDR_PLUGIN_STATE_DIR") or os.path.join(
        os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), PLUGIN_ID)
    os.makedirs(path, exist_ok=True)
    return path


def load_last():
    try:
        with open(os.path.join(state_dir(), "last.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def remember(label, name):
    # Keyed by workspace label: workspace ids are not kept across herdr restarts.
    last = load_last()
    last[label] = name
    path = os.path.join(state_dir(), "last.json")
    write_atomic(path, json.dumps(last))


def write_atomic(path, text):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def wait_for_pick(pane, result):
    """Until the picker has written its result or its pane is gone. No timeout: the user
    may leave the picker open as long as they like."""
    while not os.path.exists(result):
        try:
            herdr("pane", "get", pane)
        except Abort:
            return
        time.sleep(0.2)
    # The pane must open after the overlay is gone (see pick).
    for _ in range(50):
        try:
            herdr("pane", "get", pane)
        except Abort:
            return
        time.sleep(0.1)


def pick(found, root):
    """Let the user choose in an overlay. The pane is opened only after the overlay has
    closed, because herdr restores the previous focus when an overlay closes."""
    result = os.path.join(state_dir(), f"pick-{os.getpid()}")
    # A leftover from a crashed run with the same pid would end the wait before the user picks.
    if os.path.exists(result):
        os.remove(result)
    choices = "\n".join(f"{s['name']}\t{s.get('status', '')}" for s in found)
    opened = herdr("plugin", "pane", "open", "--plugin", PLUGIN_ID, "--entrypoint", "picker",
                   "--placement", "overlay", "--focus", "--env", f"HERDR_SBX_CHOICES={choices}",
                   "--env", f"HERDR_SBX_ROOT={root}", "--env", f"HERDR_SBX_RESULT={result}")
    wait_for_pick(opened["plugin_pane"]["pane"]["pane_id"], result)
    try:
        with open(result) as f:
            name = f.read().strip()
        os.remove(result)
    except OSError:
        return None
    return name if name in {s["name"] for s in found} else None


def cmd_pick():
    result = os.environ.get("HERDR_SBX_RESULT")
    if not result:
        raise Abort("the picker is opened by the open action")
    try:
        out = subprocess.run(
            ["fzf", "--no-sort", "--layout=reverse", "--delimiter=\t", "--prompt=sandbox> ",
             f"--header=Open Claude in a sandbox of {os.environ.get('HERDR_SBX_ROOT', '')}"],
            input=os.environ.get("HERDR_SBX_CHOICES", ""), stdout=subprocess.PIPE, text=True,
            # The user's global options (e.g. --print-query) would change what fzf prints.
            env={**os.environ, "FZF_DEFAULT_OPTS": ""})
    except FileNotFoundError:
        raise Abort("fzf is not installed") from None
    write_atomic(result, out.stdout.split("\t")[0].strip() if out.returncode == 0 else "")


def cmd_open():
    ctx = context()
    workspace = ctx.get("workspace_id")
    origin = ctx.get("focused_pane_id")
    cwd = focused_cwd(ctx)
    if not workspace or not origin or not cwd:
        raise Abort("no focused pane")
    root = repo_root(cwd)
    if root is None:
        raise Abort(f"not in a git repository: {cwd}")
    found = candidates(root)
    if not found:
        raise Abort(f"no sandbox for {root} (sbx ls)")
    label = ctx.get("workspace_label") or workspace
    if len(found) == 1:
        name = found[0]["name"]
    else:
        last = load_last().get(label)
        found.sort(key=lambda s: (s["name"] != last, s.get("status") != "running", s["name"]))
        name = pick(found, root)
        if name is None:
            return
    open_pane(origin, root, name)
    remember(label, name)


def sessions_per_sandbox():
    """Interactive Claude sessions attached from the host: `sbx run --name <name>` processes.
    The VM is not looked into, so that this never keeps a sandbox awake."""
    out = subprocess.run(["ps", "-axo", "args="], capture_output=True, text=True)
    counts = {}
    for line in out.stdout.splitlines():
        argv = line.split()
        if not argv or os.path.basename(argv[0]) != "sbx":
            continue
        # Global flags may come before the subcommand (`sbx -D run …`).
        if next((a for a in argv[1:] if not a.startswith("-")), None) != "run":
            continue
        name = None
        for i, a in enumerate(argv):
            if a == "--name" and i + 1 < len(argv):
                name = argv[i + 1]
            elif a.startswith("--name="):
                name = a[len("--name="):]
            if name:
                counts[name] = counts.get(name, 0) + 1
                break
    return counts


def workspace_dir(ws):
    # Only the workspace's own checkout: a pane's cwd would also match workspaces that merely
    # have a pane open in that directory.
    path = (ws.get("worktree") or {}).get("checkout_path")
    return os.path.realpath(path) if path else None


def summary(sandbox, sessions, statuses):
    name = sandbox["name"]
    text = name + ("●" if sandbox.get("status") == "running" else "○")
    count = max(sessions.get(name, 0), len(statuses))
    text += f" {count}" if count else ""
    working, blocked = statuses.count("working"), statuses.count("blocked")
    text += f"⡿{working}" if working else ""
    text += f"?{blocked}" if blocked else ""
    return text


def cmd_report():
    by_dir = {}
    for s in sandboxes():
        directory = primary_workspace(s)
        if directory and SANDBOX_NAME.fullmatch(str(s.get("name"))):
            by_dir.setdefault(directory, []).append(s)
    sessions = sessions_per_sandbox()
    statuses = {}
    for a in herdr("agent", "list")["agents"]:
        name = (a.get("tokens") or {}).get("sbx")
        if name and a.get("agent") == "claude":
            statuses.setdefault(name, []).append(a.get("agent_status"))
    for ws in herdr("workspace", "list")["workspaces"]:
        directory = workspace_dir(ws)
        found = sorted(by_dir.get(directory, []) if directory else [], key=lambda s: s["name"])
        lines = [summary(s, sessions, statuses.get(s["name"], [])) for s in found]
        if len(lines) > REPORT_ROWS:
            lines = lines[:REPORT_ROWS - 1] + [f"+{len(lines) - REPORT_ROWS + 1} more"]
        # Expires on its own when the periodic job that calls report stops.
        args = ["workspace", "report-metadata", ws["workspace_id"], "--source", PLUGIN_ID, "--ttl-ms", "300000"]
        for i in range(REPORT_ROWS):
            key = f"sbx{i + 1}"
            args += ["--token", f"{key}={lines[i]}"] if i < len(lines) else ["--clear-token", key]
        herdr(*args, raw=True)


def main(argv):
    if argv == ["report"]:
        # Runs every minute from a periodic job: report failures to stderr, never as notifications.
        try:
            cmd_report()
        except (Abort, OSError, KeyError, ValueError, TypeError, AttributeError) as e:
            print(f"herdr-sbx report: {e}", file=sys.stderr)
            return 1
        return 0
    commands = {"open": cmd_open, "pick": cmd_pick}
    if len(argv) != 1 or argv[0] not in commands:
        print(f"usage: python3 -m herdr_sbx {{{'|'.join([*commands, 'report'])}}}")
        return 2
    try:
        commands[argv[0]]()
    except Abort as e:
        notify(str(e))
        return 1
    except (KeyError, ValueError, TypeError, AttributeError) as e:
        # Unexpected herdr/sbx output: say so instead of dying silently in the plugin log.
        notify(f"unexpected error: {e!r}")
        return 1
    return 0
