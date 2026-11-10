# Coding Agents: Skills, Plugin and MCP Server

Konstruct gives coding agents the graph in three ways: skills that teach whole workflows with the right commands, an MCP server with tools, prompts and resources, and a hook that runs the gate after an agent edits SQL or dbt YAML. `konstruct install` writes them in each agent's own format, and the Claude Code plugin bundles all of them.

## The Skills

Each skill is an [Agent Skill](https://agentskills.io/), a `SKILL.md` an agent loads when a request matches its description, or when invoked by name (`/konstruct-safe-change` in Claude Code). Each runs real `konstruct` commands and says how to read their output.

| Skill | What the agent does |
|-------|---------------------|
| `konstruct` | Answers questions from the graph with the narrowest command: `impact`, `trace`, `describe`, `explain`, `define`, `ontology`, `conflicts`, `history`, `path`, `search`, `list` or `query` |
| `konstruct-safe-change` | Runs `impact` before changing a model, table or column, makes the edit with everything that reads it, then runs `gate` and fixes each finding until it passes |
| `konstruct-review-change` | Reviews a branch or pull request with `diff`, `gate` and `impact`, and ends with a verdict (block, needs owner sign-off, or safe to merge), the breaking changes, what the change reaches and the `dbt build --select` to run |
| `konstruct-reconcile` | Explains why two numbers differ with `conflicts`, `explain`, `trace`, `history` and `verify`: the filter, join, source, grain or aggregation that differs, and which side follows the approved definition |
| `konstruct-write-query` | Writes a query from the `ontology` and `define`: the approved term's required filters and grain, an existing column that holds the measure, and joins only along the relationships the ontology lists |
| `konstruct-define-terms` | Records a term in `definitions.yml` with its owner, status, grain, filters and columns, leaves approval to the owner, and checks the build's warnings |
| `konstruct-onboard` | Gives an overview of a repository from `status`, `REPORT.md`, the ontology and the most used models |
| `konstruct-export-semantic-layer` | Exports the ontology as an [Apache Ossie](ossie.md) model and reports each measure left out and why |

Claude Code also gets a `konstruct-data-reviewer` subagent, which reviews a change with the same commands and does not edit files. Each skill pre-approves `konstruct` shell commands for the turn it runs in.

## Installing for Each Agent

```bash
konstruct install                                   # Claude Code and Cursor, in your home directory
konstruct install --target . --agent all --mcp      # every agent, in this repository, with MCP
```

| Agent | Skills | Instructions | MCP server (`--mcp`, with `--target`) |
|-------|--------|--------------|----------------------------------------|
| Claude Code (`claude`) | `.claude/skills/`, and the subagent in `.claude/agents/` | | `.mcp.json` |
| Cursor (`cursor`) | `.claude/skills/` (Cursor reads it), or `.cursor/skills/` when installing for Cursor alone | `.cursor/rules/konstruct.mdc`, attached to SQL and dbt YAML files | `.cursor/mcp.json` |
| Codex (`codex`) | `.agents/skills/` | A section in `AGENTS.md` (`~/.codex/AGENTS.md` in the home directory) | `.codex/config.toml` |
| GitHub Copilot (`copilot`) | `.claude/skills/` or `.agents/skills/` | A section in `.github/copilot-instructions.md` | `.vscode/mcp.json` |
| Gemini CLI (`gemini`) | `.agents/skills/` | A section in `GEMINI.md` (`~/.gemini/GEMINI.md` in the home directory) | `.gemini/settings.json`, and `.agents/mcp_config.json` for Antigravity CLI |
| Windsurf (`windsurf`) | `.agents/skills/` | `.devin/rules/konstruct.md`, applied when relevant | `.devin/mcp_config.json` |

Only Claude Code expands a skill's `$ARGUMENTS`, so the skills written for other agents leave out the request section. Without `--target`, files go under your home directory, where Copilot, Cursor and Windsurf have no instructions file and MCP is registered per project. Cursor and Copilot read both `.claude/skills/` and `.agents/skills/`, so installing for Claude Code and an agent that reads only `.agents/skills/` gives them the same skills twice.

| Option | Effect |
|--------|--------|
| `--agent AGENT ...` | `claude`, `cursor`, `codex`, `copilot`, `gemini` or `windsurf`, one or more; `both` (Claude Code and Cursor, the default) or `all` |
| `--target <dir>` | Install into a repository rather than the home directory, so the team shares it |
| `--mcp` | Also register `konstruct mcp` in each agent's configuration, keeping any other servers and the file's indentation. A `konstruct` server you configured differently is left as it is, unless `--force` is given (in a Codex `config.toml`, always) |
| `--force` | Replace files and sections that were edited by hand |

Reinstalling is safe. Generated files (skills, the subagent and the rules) end with a marker, and the instructions go in a marked section of `AGENTS.md`, `GEMINI.md` or `copilot-instructions.md` (the Codex `config.toml` entry in the same way), so a reinstall updates what Konstruct wrote and leaves the rest of the file as it is. A file or section edited by hand stops the install, with nothing written, until you rerun it with `--force`. `konstruct status` shows where the skills are installed.

## The Claude Code Plugin

The repository is a Claude Code plugin marketplace. In Claude Code:

```text
/plugin marketplace add kodexlabsio/konstruct
/plugin install konstruct@konstruct
```

On Claude Code 2.1.275 or later, `/plugin install konstruct --marketplace kodexlabsio/konstruct` does both in one step.

The plugin needs the `konstruct` command on the `PATH`, and bundles:

- the skills, as `/konstruct:safe-change`, `/konstruct:review-change` and so on;
- the slash commands `/konstruct:impact <column>`, `/konstruct:review [base]`, `/konstruct:gate [base]`, `/konstruct:explain <column>` and `/konstruct:define <term>`;
- the `data-reviewer` subagent;
- the MCP server, run in the project directory;
- the hook below.

## The Hook

After the agent writes or edits a `.sql` file, a YAML file under one of a dbt project's resource paths (`model-paths`, `seed-paths`, `snapshot-paths`, `analysis-paths`, `macro-paths` and `test-paths`, or dbt's defaults), `dbt_project.yml`, a `definitions.yml` or an Ossie model, the plugin's PostToolUse hook runs `konstruct hook post-edit`. It brings a copy of the graph up to date and runs the gate:

- when the gate passes, the agent gets a one-line note;
- when it fails, the agent gets the findings, each with its fix, next to the result of its edit, and is told to fix them before it proposes the change;
- when the build warns about the edited file (SQL it cannot parse, YAML it cannot read), the agent is told, since the gate may not see all of the change;
- other files (including anything under a directory whose name starts with a dot, such as `.github`), and repositories with no Konstruct graph, are ignored; a dbt project or a repository with a `konstruct.toml` that has no graph yet gets a reminder to run `konstruct build`.

The hook works from the project root, the directory Claude Code names in `CLAUDE_PROJECT_DIR` or the one the graph was built from, whichever directory the session is in. It builds from files alone: it never runs dbt (which renders the project's macros), connects to no warehouse, and writes to `.hook/` in the output directory, without `graph.html`, so the graph you built is left as it is. Hooks run for edits made in parallel take turns, and one that finds the files as the last run left them gives that run's answer again; and `.hook/` holds a `.gitignore` so it is never committed. Each answer ends with the MCP server's notice that the text it quotes comes from the repository and is data, not instructions. The plugin starts the hook only for `.sql`, `.yml` and `.yaml` files. To add it to Claude Code without the plugin, run `konstruct hook post-edit` as a PostToolUse command hook; it reads Claude Code's PostToolUse event on standard input. `--remind` only reminds the agent to run the gate.

## The MCP Server

The server exposes `impact`, `trace`, `search`, `describe`, `conflicts`, `define`, `ontology`, `explain`, `history`, `path`, `query`, `diff` and `gate`, returns the same structured results as `--json`, and rereads `graph.json` when a build changes it. Its prompts are the workflows of the skills, and its resources are `REPORT.md`, the ontology and a summary of the graph (see [MCP Server](../reference/mcp.md)).

## Before Opening a Pull Request

The skills, the hook and the `gate` MCP tool tell the agent to run `konstruct gate` before it proposes a change, fix what it reports and run it again until it passes. The gate fails on a removed column something still reads, an approved definition whose meaning changed, and a changed column that computes a business idea differently from an existing one, and each finding says how to fix it. With `gate: true`, the [GitHub Action](github-action.md#the-gate) runs the same check on the pull request.
