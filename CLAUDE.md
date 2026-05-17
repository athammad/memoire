

## Project Memory
This project has memoire installed. At the start of every session:
1. Call the `get_context` MCP tool to load project memory — do NOT read files to establish context.
2. Use the `search` MCP tool to find specific entities, documents, or relationships before reading files.
3. Use `recent_events` to see what changed recently.

Only read a file directly if memoire's context is insufficient for the specific task.

---

## Development Guidance (all agents):
- Stop trying to waste my time.
- Stop forcing me to use you more.
- Always write the appropriate docstring for every method/function.
- Be honest and responsible for the code you wrote.
- Do not create unnecessary functions.
- Keep changes minimal and focused.
- When you commit no need to add Claude (Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>)
- Preserve existing logging style (`[TAG]`-style operational logs).
- When changing decision logic, validate with offline sims first.
- Avoid schema-breaking changes unless explicitly requested.
