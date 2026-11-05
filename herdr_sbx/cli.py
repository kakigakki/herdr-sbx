"""Find the sbx sandbox of the focused pane's repository and open Claude in it in a split pane.

herdr recognizes an agent by the pane's foreground process, which is `sbx` here, so the
command carries HERDR_AGENT=claude (https://herdr.dev/tui/docs/agents/#vms-and-sandbox-wrappers).
"""
import json
import os
import re
import shlex
import subprocess
import time

PLUGIN_ID = "herdr-sbx"
# sbx's own naming rule. The name is typed into a host shell, so anything else is refused.
SANDBOX_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]+")


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
    except (Abort, KeyError, StopIteration):
        return "down"
    # Terminal cells are roughly twice as tall as wide.
    return "right" if rect["width"] > rect["height"] * 2.2 else "down"


def open_pane(origin, root, name):
    split = herdr("pane", "split", origin, "--direction", split_direction(origin), "--cwd", root, "--focus")
    pane = split["pane"]["pane_id"]
    herdr("pane", "rename", pane, name, raw=True)
    wait_shell_ready(pane)
    herdr("pane", "run", pane, f"HERDR_AGENT=claude sbx run --name {shlex.quote(name)}", raw=True)


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


def main(argv):
    commands = {"open": cmd_open, "pick": cmd_pick}
    if len(argv) != 1 or argv[0] not in commands:
        print(f"usage: python3 -m herdr_sbx {{{'|'.join(commands)}}}")
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
