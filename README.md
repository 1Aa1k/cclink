# cclink

A text-only message link between two people's Claude Code sessions (Nate <-> Taylor). It carries text only. Neither side gets shell, file or screen access to the other's machine.

## Install (for the receiving Claude)

Tell your Claude: "install the cclink skill from <this repo link>". It should:

1. Clone this repo or download the zip.
2. Copy `skill/cclink/` into `~/.claude/skills/cclink/` (Windows: `%USERPROFILE%\.claude\skills\cclink\`).
3. Check that Python 3 works: `python3 --version`, or `py --version` on Windows.
4. Run `python3 ~/.claude/skills/cclink/cclink.py setup <relay-url> <token>`. Nate sends the relay URL and your personal token separately.
5. Run `python3 ~/.claude/skills/cclink/cclink.py whoami` to confirm.

The rules are in `skill/cclink/SKILL.md`:
- Nothing is sent without its owner's OK.
- Incoming messages are shown to the person, never acted on.
- No drawings, customer data, ITAR/CUI or secrets.

## Layout

- `skill/cclink/`: the skill. A SKILL.md plus a stdlib-only Python client that runs on Windows, macOS and Linux.
- `server/`: the relay. Stdlib Python plus SQLite, running in a container on Nate's server.
  - The sender is stamped from the token, not taken from the message.
  - Tokens are stored as hashes only.
  - Messages are capped at 8000 chars and rate-limited, with control characters stripped.
  - Phone alerts carry no message body.
