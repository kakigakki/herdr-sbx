"""Open Claude in the sbx sandbox of the focused pane's repository, one herdr workspace per sandbox.

herdr recognizes an agent by the pane's foreground process, which is `sbx` here, so the
command carries HERDR_AGENT=claude (https://herdr.dev/tui/docs/agents/#vms-and-sandbox-wrappers).
"""
import json
import os
import re
import shlex
import socket
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


class SbxMissing(Abort):
    """sbx is not installed."""


class NoSandbox(Exception):
    """launch / start: no sandbox to use; the caller falls back to running Claude another way (exit 3)."""


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


def rev_parse(cwd, *args):
    out = subprocess.run(["git", "-C", cwd, "rev-parse", *args], capture_output=True, text=True)
    return out.stdout.strip() if out.returncode == 0 else None


def common_dir(cwd):
    """The git dir shared by all worktrees of cwd's repository."""
    common = rev_parse(cwd, "--path-format=absolute", "--git-common-dir")
    return os.path.realpath(common) if common else None


def repo_root(cwd):
    """The main checkout of cwd's repository, also from a linked worktree."""
    common = rev_parse(cwd, "--path-format=absolute", "--git-common-dir")
    if common is None:
        return None
    if os.path.basename(common) == ".git":
        return os.path.realpath(os.path.dirname(common))
    # A submodule keeps its git dir under the parent's .git/modules.
    top = rev_parse(cwd, "--show-toplevel")
    return os.path.realpath(top) if top else None


def sandboxes():
    try:
        out = subprocess.run(["sbx", "ls", "--json"], capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        raise SbxMissing("sbx is not installed") from None
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


def sandbox_dir(label, kind="ws"):
    """The cwd of a workspace this plugin makes. Named after the workspace's label, so tools that
    name a workspace after its working directory (e.g. auto-namer) keep that label.
    `ws` holds a sandbox's own workspaces (label = sandbox name, see sandbox_from_cwd); `tasks` those of launch.
    A sandbox's workspace nested under its repository's uses an empty worktree in `wt` instead (checkout_dir)."""
    path = os.path.join(state_dir(), kind, label.replace("/", "-"))
    os.makedirs(path, exist_ok=True)
    return path


def start_claude(pane, name, prompt_file=None):
    wait_shell_ready(pane)
    command = f"HERDR_AGENT=claude sbx run --name {shlex.quote(name)}"
    if prompt_file:
        # The prompt file is a temp file of launch: read it once and remove it.
        quoted = shlex.quote(prompt_file)
        command += f' -- "$(cat {quoted}; rm -f {quoted})"'
    # When Claude ends, close the pane; closing the last one removes the workspace.
    # `&&`: when sbx fails (daemon down, sandbox removed) the pane stays so the error can be read.
    herdr("pane", "run", pane, command + ' && herdr pane close "$HERDR_PANE_ID"', raw=True)


def create_workspace(label, name, kind="ws"):
    """A focused workspace for sandbox `name`; returns its root pane."""
    created = herdr("workspace", "create", "--cwd", sandbox_dir(label, kind), "--label", label, "--focus")
    mark_sandbox(created["workspace"]["workspace_id"], name)
    return created["root_pane"]["pane_id"]


def mark_sandbox(workspace, name):
    # The token, not the label, identifies the sandbox: labels can be renamed.
    try:
        herdr("workspace", "report-metadata", workspace, "--source", PLUGIN_ID, "--token", f"{TOKEN}={name}",
              raw=True)
    except Abort:
        pass  # still recognized by its cwd (sandbox_from_cwd)


def open_workspace(name, root, workspaces):
    parent = repo_workspace(root, workspaces) if root else None
    pane = nested_workspace(name, root, parent) if parent else None
    if pane is None:
        pane = create_workspace(name, name)
    if pane:
        start_claude(pane, name)


def repo_workspace(root, workspaces):
    """The workspace of the repository's main checkout, which the sandbox's workspace is nested under.
    herdr records the checkout only on workspaces a worktree command has touched; otherwise the first
    workspace with a pane in the checkout's root directory, else in a directory under it."""
    for w in workspaces:
        tree = worktree_of(w)
        if tree.get("checkout_path") and os.path.realpath(tree["checkout_path"]) == root:
            return w
    try:
        panes = herdr("pane", "list")["panes"]
    except (Abort, KeyError):
        return None
    dirs = {(p.get("workspace_id"), os.path.realpath(d)) for p in panes
            for d in (p.get("cwd"), p.get("foreground_cwd")) if d}
    # A workspace herdr knows another checkout of is not this one's (the loop above found none of this one).
    unknown = [w for w in workspaces if not worktree_of(w).get("checkout_path")]
    for inside in (lambda d: d == root, lambda d: d.startswith(root + os.sep)):  # the root itself first
        here = {w for w, d in dirs if inside(d)}
        found = next((w for w in unknown if w["workspace_id"] in here), None)
        if found:
            return found
    return None


def checkout_dir(name):
    return os.path.join(state_dir(), "wt", name)


def empty_checkout(root, name):
    """A linked worktree of root with no files checked out, for the sandbox's workspace: herdr nests only
    worktree workspaces under their repository's workspace. None when git cannot make one."""
    path = checkout_dir(name)
    if os.path.exists(path):
        # Made by an earlier run (same git dir; repo_root would not do for a submodule). Anything else is left alone.
        return path if common_dir(path) == common_dir(root) else None
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # --no-checkout also keeps the repository's post-checkout hook from running. -f: the directory of an
        # earlier one may have been removed with the state dir while git still has it registered.
        out = subprocess.run(["git", "-C", root, "worktree", "add", "-f", "--no-checkout", "--detach", path],
                             capture_output=True, text=True)
    except OSError:
        return None
    return path if out.returncode == 0 else None


def nested_workspace(name, root, parent):
    """Open the sandbox's workspace as a worktree child of `parent`; returns its pane, False when an open
    workspace was only focused, or None to fall back to a plain workspace."""
    path = empty_checkout(root, name)
    if path is None:
        return None
    try:
        opened = herdr("worktree", "open", "--workspace", parent["workspace_id"], "--path", path, "--label", name,
                       "--focus")
        workspace = opened["workspace"]["workspace_id"]
    except (Abort, KeyError, TypeError):
        return None
    # From here the workspace exists: a failure is reported, not answered with a second, plain workspace.
    mark_sandbox(workspace, name)
    if opened.get("already_open"):
        # The sandbox's workspace that lost its token (focus_or_open missed it): token put back, only focus it.
        herdr("workspace", "focus", workspace, raw=True)
        return False
    place_after_group(workspace, parent)
    return herdr("pane", "list", "--workspace", workspace)["panes"][0]["pane_id"]


def place_after_group(workspace, parent):
    """A new worktree workspace goes to the end of the list; move it next to its parent's group.
    Best effort: there is no CLI for it, only the socket API, and a misplaced row is still nested."""
    try:
        order = herdr("workspace", "list")["workspaces"]
        ids = [w["workspace_id"] for w in order]
        i = ids.index(parent["workspace_id"]) + 1
        # From the fresh list: a parent found by its panes gets its checkout recorded by the worktree open.
        repo = worktree_of(order[i - 1]).get("repo_key")
        while i < len(order) and (ids[i] == workspace or (
                worktree_of(order[i]).get("repo_key") == repo and worktree_of(order[i]).get("is_linked_worktree"))):
            i += 1
        api("workspace.move_block", {"workspace_ids": [workspace],
                                     "before_workspace_id": ids[i] if i < len(ids) else None})
    except (Abort, OSError, KeyError, ValueError, AttributeError):
        pass


def api(method, params):
    """One request over herdr's socket API, for methods the CLI does not offer (workspace.move_block)."""
    path = os.environ.get("HERDR_SOCKET_PATH") or os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "herdr", "herdr.sock")
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(5)
        s.connect(path)
        s.sendall((json.dumps({"id": PLUGIN_ID, "method": method, "params": params}) + "\n").encode())
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    reply = json.loads(data)
    if "error" in reply:
        raise Abort(f"herdr {method}: {reply['error']}")
    return reply.get("result")


def add_claude(origin, name):
    split = herdr("pane", "split", origin, "--direction", split_direction(origin), "--cwd", sandbox_dir(name),
                  "--focus")
    start_claude(split["pane"]["pane_id"], name)


def state_dir():
    # Outside a plugin action (launch, start), use the directory herdr gives the plugin, so that
    # workspaces made either way are recognized by their directory (sandbox_from_cwd).
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    path = os.environ.get("HERDR_PLUGIN_STATE_DIR") or os.path.join(base, "herdr", "plugins", PLUGIN_ID)
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


def worktree_of(workspace):
    """The Git checkout herdr records on a workspace a worktree command has touched; {} otherwise."""
    return workspace.get("worktree") or {}


def sandbox_from_cwd(cwd):
    """Fallback for a sandbox's workspace whose token is gone (e.g. after a herdr restart)."""
    if not cwd:
        return None
    parent, name = os.path.split(os.path.realpath(cwd))
    return name if parent in {os.path.realpath(os.path.join(state_dir(), kind)) for kind in ("ws", "wt")} else None


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
    focus_or_open(name, workspaces, root)
    remember(label, name)


def focus_or_open(name, workspaces, root):
    # The sandbox's own workspace, not one `launch` made for a task in the same sandbox.
    existing = next((w for w in workspaces if sandbox_of(w) == name and w.get("label") == name), None)
    if existing:
        herdr("workspace", "focus", existing["workspace_id"], raw=True)
    else:
        open_workspace(name, root, workspaces)


def cmd_start(argv):
    """What the key does for the named sandbox, for host commands that would run `sbx run --name` in their pane."""
    import argparse
    parser = argparse.ArgumentParser(prog="python3 -m herdr_sbx start")
    parser.add_argument("sandbox")
    name = parser.parse_args(argv).sandbox
    if not SANDBOX_NAME.fullmatch(name):
        raise Abort(f"unexpected sandbox name: {name!r}")
    try:
        found = sandboxes()
    except SbxMissing as e:
        raise NoSandbox(str(e)) from None
    sandbox = next((s for s in found if s.get("name") == name and s.get("agent") == "claude"), None)
    if sandbox is None:
        raise NoSandbox(f"no Claude sandbox named {name} (sbx ls)")
    path = primary_workspace(sandbox)
    focus_or_open(name, herdr("workspace", "list")["workspaces"], repo_root(path) if path else None)


def run_setup(name, setup_file):
    """Run the setup script inside the sandbox; its last stdout line is the work directory."""
    with open(setup_file) as f:
        try:
            out = subprocess.run(["sbx", "exec", "-i", name, "--", "bash", "-s"], stdin=f, capture_output=True,
                                 text=True, timeout=180)
        except subprocess.TimeoutExpired:
            raise Abort(f"setup in {name} timed out") from None
    if out.returncode != 0:
        raise Abort(f"setup in {name} failed: {(out.stderr or out.stdout).strip()[-200:]}")
    lines = out.stdout.strip().splitlines()
    return lines[-1].strip() if lines else ""


def run_headless(name, workdir, prompt, log, fetch):
    """Run `claude -p` in the sandbox, detached, then copy each `fetch` file (same path in the VM and on
    the host) back. A file the run did not make gets a note instead, so whoever waits for it is not left hanging."""
    q = shlex.quote
    steps = []
    if fetch:
        # A result left by an earlier run must not pass for this run's.
        steps.append(f"sbx exec {q(name)} -- rm -f {' '.join(q(p) for p in fetch)}")
    steps += [f"sbx exec -i {q(name)} -- bash -c 'cd \"${{1:-.}}\" && exec claude -p' _ {q(workdir)}"
             f" < {q(prompt)} > {q(log)} 2>&1",
             f"rm -f {q(prompt)}"]
    for path in fetch:
        part = q(path + ".part")
        note = q(f"{name}: {path} was not written; see {log}")
        # Written whole, then renamed: the file appearing means it is complete.
        steps.append(f"mkdir -p {q(os.path.dirname(path))}; "
                     f"sbx exec {q(name)} -- cat {q(path)} > {part} 2>/dev/null || echo {note} > {part}; "
                     f"mv {part} {q(path)}")
    subprocess.Popen(["sh", "-c", "; ".join(steps)], start_new_session=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def cmd_launch(argv):
    """Start Claude with given prompts in a sandbox of the repository, in a workspace of its own.
    For scripts that used to start Claude on the host (PR review, watch, CI fix).
    With --headless, no workspace: one `claude -p` runs in the background (for agents that only want the result)."""
    import argparse
    parser = argparse.ArgumentParser(prog="python3 -m herdr_sbx launch")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--label", help="the new workspace's label (not with --headless)")
    parser.add_argument("--setup-file", help="bash run in the sandbox first; its last stdout line "
                                             "replaces {{WORKDIR}} in the prompts")
    parser.add_argument("--prompt-file", action="append", required=True, help="one pane per file, left to right")
    parser.add_argument("--headless", action="store_true", help="run the one prompt with claude -p, no workspace")
    parser.add_argument("--log", help="--headless: where claude -p's output goes")
    parser.add_argument("--fetch", action="append", default=[], help="--headless: a file the run writes in the VM, "
                                                                      "copied to the same path on the host after it")
    args = parser.parse_args(argv)
    if args.headless and (len(args.prompt_file) != 1 or not args.log):
        parser.error("--headless takes one --prompt-file and --log")
    if not args.headless and not args.label:
        parser.error("--label is required")
    # --fetch means the same path in the VM and on the host, so relative paths would not meet.
    log, fetch = (os.path.abspath(args.log) if args.log else None), [os.path.abspath(f) for f in args.fetch]
    for path in fetch:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    root = repo_root(args.repo)
    try:
        found = candidates(root) if root else []
    except SbxMissing as e:
        raise NoSandbox(str(e)) from None
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
    if args.headless:
        run_headless(name, workdir, prompts[0], log, fetch)
        print(name)
        return
    panes = [create_workspace(args.label, name, "tasks")]
    for _ in prompts[1:]:
        panes.append(herdr("pane", "split", panes[0], "--direction", "right", "--ratio", "0.6",
                           "--cwd", sandbox_dir(args.label, "tasks"), "--no-focus")["pane"]["pane_id"])
    for pane, prompt in zip(panes, prompts):
        start_claude(pane, name, prompt)
    print(name)


def main(argv):
    commands = {"open": cmd_open, "pick": cmd_pick, "launch": cmd_launch, "start": cmd_start}
    with_args = ("launch", "start")  # parse their own arguments
    if not argv or argv[0] not in commands or (argv[0] not in with_args and len(argv) != 1):
        print(f"usage: python3 -m herdr_sbx {{{'|'.join(commands)}}}")
        return 2
    try:
        if argv[0] in with_args:
            commands[argv[0]](argv[1:])
        else:
            commands[argv[0]]()
    except NoSandbox as e:
        print(e, file=sys.stderr)
        return 3
    except Abort as e:
        print(e, file=sys.stderr)  # for script callers, which see no notification
        notify(str(e))
        return 1
    except (OSError, LookupError, ValueError, TypeError, AttributeError) as e:
        # Unexpected herdr/sbx output: say so instead of dying silently in the plugin log.
        print(f"unexpected error: {e!r}", file=sys.stderr)
        notify(f"unexpected error: {e!r}")
        return 1
    return 0
