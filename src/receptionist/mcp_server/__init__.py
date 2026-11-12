"""MCP server for the clinic receptionist: scheduling tools and knowledge-base search (RAG).

Entry points:
    python -m receptionist.mcp_server             serve streamable HTTP at MCP_SERVER_URL
    python -m receptionist.mcp_server.mint_token  mint a bearer token
    python -m receptionist.mcp_server.rag.evaluate

In-process use: `receptionist.mcp_server.server.build_server(settings)` and
`receptionist.mcp_server.db.reset_database(path)`.
"""
