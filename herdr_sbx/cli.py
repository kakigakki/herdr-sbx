"""Open Claude in the sbx sandbox of the focused pane's repository, one herdr workspace per sandbox.

herdr recognizes an agent by the pane's foreground process, which is `sbx` here, so the
command carries HERDR_AGENT=claude (https://herdr.dev/tui/docs/agents/#vms-and-sandbox-wrappers).
"""
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time

PLUGIN_ID = "herdr-sbx"
# sbx's own naming rule. The name is typed into a host shell, so anything else is refused.
SANDBOX_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]+")
# Workspace token marking a sandbox's workspace. Namespaced: herdr merges tokens of all sources.
TOKEN = "herdr_sbx"


class Abort(Exception):
    """A reason shown to the user as a herdr notification."""


class NoSandbox(Exception):
    """launch: the repository has no sandbox; the caller falls back to its host mode (exit 3)."""


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


def sandbox_dir(label):
    """The cwd of a workspace this plugin makes. Named after the workspace's label, so tools that
    name a workspace after its working directory (e.g. auto-namer) keep that label."""
    path = os.path.join(state_dir(), "ws", label.replace("/", "-"))
    os.makedirs(path, exist_ok=True)
    return path


def start_claude(pane, name, prompt_file=None):
    wait_shell_ready(pane)
    command = f"HERDR_AGENT=claude sbx run --name {shlex.quote(name)}"
    if prompt_file:
        command += f' -- "$(cat {shlex.quote(prompt_file)})"'
    # When Claude ends, close the pane; closing the last one removes the workspace.
    # `&&`: when sbx fails (daemon down, sandbox removed) the pane stays so the error can be read.
    herdr("pane", "run", pane, command + ' && herdr pane close "$HERDR_PANE_ID"', raw=True)


def create_workspace(label, name):
    """A focused workspace for sandbox `name`; returns its root pane."""
    created = herdr("workspace", "create", "--cwd", sandbox_dir(label), "--label", label, "--focus")
    workspace = created["workspace"]["workspace_id"]
    # The token, not the label, identifies the sandbox: labels can be renamed.
    try:
        herdr("workspace", "report-metadata", workspace, "--source", PLUGIN_ID, "--token", f"{TOKEN}={name}",
              raw=True)
    except Abort:
        pass  # still recognized by its cwd (sandbox_from_cwd)
    return created["root_pane"]["pane_id"]


def open_workspace(name):
    start_claude(create_workspace(name, name), name)


def add_claude(origin, name):
    split = herdr("pane", "split", origin, "--direction", split_direction(origin), "--cwd", sandbox_dir(name),
                  "--focus")
    start_claude(split["pane"]["pane_id"], name)


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


def sandbox_of(workspace):
    return (workspace.get("tokens") or {}).get(TOKEN)


def sandbox_from_cwd(cwd):
    """Fallback for a sandbox's workspace whose token is gone (e.g. after a herdr restart)."""
    if not cwd:
        return None
    parent, name = os.path.split(os.path.realpath(cwd))
    return name if parent == os.path.realpath(os.path.join(state_dir(), "ws")) else None


def cmd_open():
    ctx = context()
    workspace = ctx.get("workspace_id")
    origin = ctx.get("focused_pane_id")
    if not workspace or not origin:
        raise Abort("no focused pane")
    workspaces = herdr("workspace", "list")["workspaces"]
    cwd = focused_cwd(ctx)
    here = sandbox_of(next((w for w in workspaces if w["workspace_id"] == workspace), {})) or sandbox_from_cwd(cwd)
    if here:
        # Inside a sandbox's workspace: one more Claude in the same sandbox.
        if not SANDBOX_NAME.fullmatch(here):
            raise Abort(f"unexpected sandbox name: {here!r}")
        if here not in {s.get("name") for s in sandboxes()}:
            raise Abort(f"sandbox {here} no longer exists (sbx ls)")
        add_claude(origin, here)
        return
    if not cwd:
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
    # The sandbox's own workspace, not one `launch` made for a task in the same sandbox.
    existing = next((w for w in workspaces if sandbox_of(w) == name and w.get("label") == name), None)
    if existing:
        herdr("workspace", "focus", existing["workspace_id"], raw=True)
    else:
        open_workspace(name)
    remember(label, name)


def run_setup(name, setup_file):
    """Run the setup script inside the sandbox; its last stdout line is the work directory."""
    with open(setup_file) as f:
        try:
            out = subprocess.run(["sbx", "exec", "-i", name, "--", "bash", "-s"], stdin=f, capture_output=True,
                                 text=True, timeout=600)
        except subprocess.TimeoutExpired:
            raise Abort(f"setup in {name} timed out") from None
    if out.returncode != 0:
        raise Abort(f"setup in {name} failed: {(out.stderr or out.stdout).strip()[-200:]}")
    lines = out.stdout.strip().splitlines()
    return lines[-1].strip() if lines else ""


def cmd_launch(argv):
    """Start Claude with given prompts in a sandbox of the repository, in a workspace of its own.
    For scripts that used to start Claude on the host (PR review, watch, CI fix)."""
    import argparse
    parser = argparse.ArgumentParser(prog="python3 -m herdr_sbx launch")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--label", required=True, help="the new workspace's label")
    parser.add_argument("--setup-file", help="bash run in the sandbox first; its last stdout line "
                                             "replaces {{WORKDIR}} in the prompts")
    parser.add_argument("--prompt-file", action="append", required=True, help="one pane per file, left to right")
    args = parser.parse_args(argv)
    root = repo_root(args.repo)
    found = candidates(root) if root else []
    if not found:
        raise NoSandbox(f"no sandbox for {root or args.repo}")
    # No picker: callers run without a terminal (e.g. Raycast).
    name = min(found, key=lambda s: (s.get("status") != "running", s["name"]))["name"]
    workdir = run_setup(name, args.setup_file) if args.setup_file else ""
    prompts = []
    for path in args.prompt_file:
        with open(path) as f:
            text = f.read().replace("{{WORKDIR}}", workdir)
        fd, prompt = tempfile.mkstemp(prefix="herdr-sbx-prompt-", suffix=".md")
        with os.fdopen(fd, "w") as f:
            f.write(text)
        prompts.append(prompt)
    panes = [create_workspace(args.label, name)]
    for _ in prompts[1:]:
        panes.append(herdr("pane", "split", panes[0], "--direction", "right", "--ratio", "0.6",
                           "--cwd", sandbox_dir(args.label), "--no-focus")["pane"]["pane_id"])
    for pane, prompt in zip(panes, prompts):
        start_claude(pane, name, prompt)
    print(name)


def main(argv):
    commands = {"open": cmd_open, "pick": cmd_pick, "launch": cmd_launch}
    if not argv or argv[0] not in commands or (argv[0] != "launch" and len(argv) != 1):
        print(f"usage: python3 -m herdr_sbx {{{'|'.join(commands)}}}")
        return 2
    try:
        if argv[0] == "launch":
            cmd_launch(argv[1:])
        else:
            commands[argv[0]]()
    except NoSandbox as e:
        print(e, file=sys.stderr)
        return 3
    except Abort as e:
        notify(str(e))
        return 1
    except (OSError, KeyError, ValueError, TypeError, AttributeError) as e:
        # Unexpected herdr/sbx output: say so instead of dying silently in the plugin log.
        notify(f"unexpected error: {e!r}")
        return 1
    return 0
