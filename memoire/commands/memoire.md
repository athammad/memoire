Load the causal project graph for this session using memoire.

Call the `get_context` MCP tool (memoire-ai server) and present the result as:
- A summary of the project structure (directory tree)
- The top relationships ranked by causal score, grouped by type (DRIVES, SPECIFIES, ASSERTS_ON first)
- Recent events

Do not read any source files. The graph is your source of truth for project structure and causal relationships.
