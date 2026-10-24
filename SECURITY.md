# Security policy

pitchside runs on your own machine: it downloads public Cricsheet and Wikipedia data, stores it in local DuckDB files, and its MCP server talks to AI clients over stdio. It has no network service, accounts or stored credentials.

The parts most worth a look are the AI-facing tools: `query` and `export` accept SQL from an AI client and must stay read-only, with no filesystem access beyond `data/exports/`.

## Reporting a vulnerability

Please report privately through GitHub: **Security → Report a vulnerability** on this repository. Don't open a public issue for it.

Only the latest release is supported.
