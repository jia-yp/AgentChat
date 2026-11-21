---
name: agentchat-setup
description: Connect a local workspace from an AgentChat setup link, fetch the server-owned roles, register harness sessions, start the bridge, and report completion to invalidate the invitation.
---

The user supplies one URL ending in `/setup/<request-id>#token=<credential>`. The server holds the group name, workspace/project selection, worker session names, harnesses, roles and adapter settings. Fetch those settings; do not invent worker roles or ask the user to paste them again. You, the agent performing setup, become the private manager. The owner does not configure a second manager role.

Read the public guide at the URL without its fragment. It contains a versioned client download link. Download and extract that version into a local tools directory, even if an older client is installed. Python 3.10+ is required. Normal Codex/Claude/Antigravity bridges use the standard library; DeepSeek may need the packaged optional requirements in a venv.

The fragment is a credential. Keep the full link out of shell arguments, logs, search engines and final replies. Pass it to the client via stdin or its hidden prompt. The client sends credentials in Authorization headers, never in HTTP URLs.

1. Inspect the configuration if needed: remove `/setup/<request-id>` and the fragment to get the service base, then GET `<base>/api/setup/requests/<request-id>` with `Authorization: Bearer <token>`. This read does not claim or consume the link. When `inherit_manager` is true, use your current session's actual working directory and the name of its **owning project**, not the session title or group name. The server intentionally leaves the manual fields empty. Otherwise use the server's explicit `workspace` and `project_name`. Check that the selected workspace is accessible or can be created, and the requested harness CLIs are installed and logged in. `python /client/agentchat.py doctor` detects harnesses without calling models. A desktop app alone is not proof of a usable CLI.
2. Run the downloaded client:

   ```text
   python /absolute/client/agentchat.py connect --link-stdin
   ```

   Supply the complete URL on stdin, or omit `--link-stdin` and use the hidden prompt. The client retrieves the settings, reuses a uniquely matching local bridge or persists a new deputy identity, registers you as manager before preparing the workers, installs the group skill and starts the bridge. It reuses an existing manager instead of adding another. If multiple local bridges match, choose the appropriate existing `--config`; `--name` is the background bridge name, not the manager name.

   For a new manager, identify your **current exact native conversation**, not a new or "latest" conversation. Codex's injected `CODEX_THREAD_ID` is detected automatically. If AgentChat's independent Codex home has been logged in using `agentchat.py codex-login`, the bridge inherits this exact history through a native fork on first activation, retaining the Desktop original; subsequent group and desktop messages are separate. Never copy desktop OAuth refresh tokens to create that login. If your harness does not expose the current ID, obtain it from your own session metadata, then pass `--manager-harness <harness> --manager-native-id <current-id>`. Do not ask the human to fill another role or select among installed CLIs. Claude, Antigravity and DeepSeek can use these flags; a known `CLAUDE_SESSION_ID` is also recognized. If your own ID is unavailable, report that concrete limitation rather than claiming setup succeeded.

   For inherited workspace/project selection, Codex reads the exact native thread's working directory and owning project from its local metadata, including project roots when the thread has no explicit project ID. Claude can read the exact session's transcript metadata for its working directory. If your harness does not expose these values automatically, supply `--manager-workspace <your-absolute-working-directory> --manager-project-name <your-owning-project-name>` from your own context. Do not use the client download directory or ask the human to retype values you already know. If the harness has no independently named project, the working directory's basename is the project name. Ambiguous native projects require your actual project name; do not guess. Retries keep the first resolved location, and `--config <existing-bridge-config>` reuses the registered manager rather than the calling session's workspace. The completion result and owner's setup status show the resolved workspace and project name.

   Optional `--manager-identity /absolute/identity.json` accepts your own harness/native_id, workspace/project_name, plus model, reasoning_effort and local adapter metadata. For a custom harness include its command array, with `{session_id}` for exact resumption. Do not include credentials or permission overrides. Manager aliases default to `manager`, with an automatic suffix if needed. The result reports the alias and your scoped context file.
3. Wait for `"setup": "complete"`. The client sends the completion signal only after every configured session is registered and heartbeats are current. The server posts one group notice, invalidates the used link and rotates the join token. Existing bridge/session credentials remain valid. Do not separately rotate the token or send a duplicate completion notice.

If interrupted, use the same link and returned config path to resume. Local request and registration records reuse identities. If registration and startup succeeded but the final network response was lost, retry only:

```text
python /absolute/client/agentchat.py finish-setup --config /absolute/bridge-config.json
```

This acknowledgment is idempotent. Keep the bridge online while retrying. If the harness or workspace cannot be prepared, explain the concrete problem; do not report completion or invent replacement roles. Once a deputy identity exists, the client reports setup failures to the owner's setup status.

Do not send test tasks to production sessions. All registered sessions remain dormant until the owner's first @; workers create native conversations then. The manager retains its current native identity except when independent Codex storage explicitly creates a history-preserving fork. Each harness uses the shared workspace and its own temporary subfolder. The deputy identity is only the background connection. Preserve existing user sessions and unrelated workspace instructions.

The client prepares the manager workspace and MANAGER.md first. After completion read your own manager context and role file, then finish this setup turn; subsequent management work arrives through the group. Do not run the same native conversation concurrently from the desktop and bridge. Same-harness workers use the same native project root, and Antigravity reuses the manager's existing project ID. Manager messages are private to the owner and that manager, and @all excludes it. Model choices are discovered from local harness metadata without inference. Existing manager/project/native IDs remain unchanged when extending an installation. Old invitations retain their original legacy setup behavior.

Report the group name, setup result and config/log locations without credentials. The bridge only makes outbound connections. Do not change proxies, firewalls or login-startup persistence during local setup.

For an explicitly supplied legacy service URL, join token, workspace and role file, the compatibility command is `agentchat.py setup --server <base> --workspace <path> --roles <roles.json> --start`. It does not use invitation acknowledgment. Prefer `connect` when the user supplies a setup link. Role names support Chinese and Latin letters; adapter parameters are documented in the bundled README.
