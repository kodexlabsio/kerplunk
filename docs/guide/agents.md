# Coding Agents: Skill and MCP Server

`konstruct install` writes `~/.claude/skills/konstruct/SKILL.md`. Claude Code and Cursor both read that location, so `/konstruct what breaks if I change stg_payments.amount?` works in either. The skill tells the agent to keep the graph current with `--refresh`, to run `impact` before changing a model or column, and how to read the output. It pre-approves `konstruct` shell commands for the turn it runs in. `konstruct status` shows where it is installed.

| Option | Effect |
|--------|--------|
| `--agent claude\|cursor\|both` | `cursor` writes `~/.cursor/skills` instead. Default `both` |
| `--target <dir>` | Install into a repository (`.claude/skills`) rather than the home directory, so the team shares it |
| `--mcp` | With `--target`, also register `konstruct mcp` in `.mcp.json` (Claude Code) and `.cursor/mcp.json` (Cursor), keeping any other servers |
| `--force` | Replace a skill that was edited by hand; otherwise a reinstall only replaces konstruct's own earlier versions |

The MCP server exposes `impact`, `trace`, `search`, `describe`, `conflicts`, `define`, `ontology`, `explain`, `path`, `query` and `diff`, returns the same structured results as `--json`, and rereads `graph.json` when a build changes it.
