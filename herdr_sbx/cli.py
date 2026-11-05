"""Find the sbx sandbox of the focused pane's repository and open Claude in it in a new tab.

herdr recognizes an agent by the pane's foreground process, which is `sbx` here, so the
command carries HERDR_AGENT=claude (https://herdr.dev/tui/docs/agents/#vms-and-sandbox-wrappers).
"""
import json
import os
import subprocess
import time

PLUGIN_ID = "herdr-sbx"
TITLE = "herdr-sbx"


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
    subprocess.run(["herdr", "notification", "show", TITLE, "--body", body], capture_output=True)


def context():
    return json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON") or "{}")


def focused_cwd(ctx):
    # foreground_cwd follows a `cd` inside the pane; the context only has the pane's start cwd.
    pane_id = ctx.get("focused_pane_id")
    if pane_id:
        try:
            pane = herdr("pane", "get", pane_id)["pane"]
            return pane.get("foreground_cwd") or pane.get("cwd")
        except (Abort, KeyError, ValueError):
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
        out = subprocess.run(["sbx", "ls", "--json"], capture_output=True, text=True)
    except FileNotFoundError:
        raise Abort("sbx is not installed") from None
    if out.returncode != 0:
        raise Abort(f"sbx ls failed: {out.stderr.strip()[:200]}")
    return json.loads(out.stdout or "[]")


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
    return [s for s in sandboxes() if primary_workspace(s) == root]


def wait_shell_ready(pane, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if herdr("pane", "read", pane, "--lines", "3", "--source", "visible", raw=True).strip():
                return
        except Abort:
            pass
        time.sleep(0.2)


def open_tab(workspace, root, name):
    created = herdr("tab", "create", "--workspace", workspace, "--cwd", root, "--label", name, "--focus")
    pane = created["root_pane"]["pane_id"]
    wait_shell_ready(pane)
    herdr("pane", "run", pane, f"HERDR_AGENT=claude sbx run --name {name}", raw=True)


def state_dir():
    path = os.environ.get("HERDR_PLUGIN_STATE_DIR") or os.path.expanduser("~/.local/state/herdr-sbx")
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
    with open(path + ".tmp", "w") as f:
        json.dump(last, f)
    os.replace(path + ".tmp", path)


def wait_closed(pane, timeout=600.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            herdr("pane", "get", pane)
        except Abort:
            return
        time.sleep(0.2)


def pick(found, root):
    """Let the user choose in an overlay. The tab is opened only after the overlay has
    closed, because herdr restores the previous focus when an overlay closes."""
    result = os.path.join(state_dir(), f"pick-{os.getpid()}")
    choices = "\n".join(f"{s['name']}\t{s.get('status', '')}" for s in found)
    opened = herdr("plugin", "pane", "open", "--plugin", PLUGIN_ID, "--entrypoint", "picker",
                   "--placement", "overlay", "--focus", "--env", f"HERDR_SBX_CHOICES={choices}",
                   "--env", f"HERDR_SBX_ROOT={root}", "--env", f"HERDR_SBX_RESULT={result}")
    wait_closed(opened["plugin_pane"]["pane"]["pane_id"])
    try:
        with open(result) as f:
            name = f.read().strip()
        os.remove(result)
    except OSError:
        return None
    return name or None


def cmd_pick():
    try:
        out = subprocess.run(
            ["fzf", "--no-sort", "--layout=reverse", "--delimiter=\t", "--prompt=sandbox> ",
             f"--header=Open Claude in a sandbox of {os.environ.get('HERDR_SBX_ROOT', '')}"],
            input=os.environ.get("HERDR_SBX_CHOICES", ""), stdout=subprocess.PIPE, text=True)
    except FileNotFoundError:
        raise Abort("fzf is not installed") from None
    name = out.stdout.split("\t")[0].strip() if out.returncode == 0 else ""
    result = os.environ["HERDR_SBX_RESULT"]
    with open(result + ".tmp", "w") as f:
        f.write(name)
    os.replace(result + ".tmp", result)


def cmd_open():
    ctx = context()
    workspace = ctx.get("workspace_id")
    cwd = focused_cwd(ctx)
    if not workspace or not cwd:
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
    remember(label, name)
    open_tab(workspace, root, name)


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
    return 0
