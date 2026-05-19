Expand the memoire graph node at: $ARGUMENTS

Call the `expand` MCP tool (memoire-ai server) with path="$ARGUMENTS" and display:
- All causal relationships (DRIVES, SPECIFIES, ASSERTS_ON, DOCUMENTS, IMPLEMENTS, RELATES_TO) with rationales and confidence scores
- All structural relationships (IMPORTS, INHERITS, CONTAINS)
- Side-effect categories and mutable state attributes if present
