# The GitHub Action

Konstruct is also a GitHub Action. It reviews every pull request column by column, posts the review as a comment (updated in place on each push), writes it to the job summary, annotates each breaking change, and fails the check when a change removes a column that something downstream still reads:

```yaml
# .github/workflows/konstruct.yml
name: konstruct
on: pull_request
permissions:
  contents: read
  pull-requests: write
jobs:
  lineage:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: kodexlabsio/konstruct@v0
```

| Input | Default | Purpose |
|-------|---------|---------|
| `working-directory` | `.` | Where the dbt project, SQL or DAGs are |
| `base` | the pull request's base branch | Commit to compare with |
| `comment` | `true` | Post the review on the pull request |
| `fail-on-breaking` | `true` | Fail the check on a breaking change |
| `gate` | `false` | Run [`konstruct gate`](#the-gate) and fail the check when it fails |
| `build-args` | `--no-warehouse --no-dbt` | Arguments for `konstruct build`. The default needs no credentials and never runs the pull request's dbt macros. Without `--no-warehouse`, see [Reading a Warehouse](#reading-a-warehouse) |
| `extras` | none | Konstruct extras, such as `snowflake` |

The outputs `breaking`, `gate`, `summary`, `report` (markdown) and `json` let later steps act on the result. The Action installs Konstruct from its own checkout, which compiles it: it sets up a toolchain (`dtolnay/rust-toolchain`) for that, and later steps in the job see it. Pin a release (`@v0.7.0`) for a fixed version; `@v0` moves to each new 0.x release. [`docs/examples/pull-request.yml`](../examples/pull-request.yml) shows the same steps written out, for adding a `dbt parse` or other setup first.

## Reading a Warehouse

With `--no-warehouse` left out of `build-args`, the build reads the warehouses your own profile names, but skips any that the repository's `konstruct.toml` or `profiles.yml` names, because the pull request under review could change them (see [Trusting a Project's Connections](../reference/configuration.md#trusting-a-projects-connections)). For pull requests, write the profile from the workflow's secrets outside the checkout and point `DBT_PROFILES_DIR` at it:

```yaml
      - name: Write the dbt profile
        run: printf '%s' "$PROFILE" > "$RUNNER_TEMP/profiles.yml"
        env:
          PROFILE: ${{ secrets.DBT_PROFILE }}
      - uses: kodexlabsio/konstruct@v0
        with:
          build-args: --no-dbt
          extras: snowflake
        env:
          DBT_PROFILES_DIR: ${{ runner.temp }}
```

On a branch whose content is already reviewed, such as a push to the default branch, `--trust-config` in `build-args` (or `KONSTRUCT_TRUST_CONFIG: "1"` in `env`) uses the repository's own connections and, without `--no-dbt`, runs its dbt project. Never set either for pull requests from forks, or for any pull request whose `konstruct.toml` and `profiles.yml` have not been reviewed.

## The Gate

`konstruct gate` is the check a change must pass before it is proposed. A coding agent runs it before opening a pull request (the Konstruct skills, the plugin's hook and the `gate` MCP tool tell it to), and `gate: true` makes the Action run the same check:

```yaml
      - uses: kodexlabsio/konstruct@v0
        with:
          gate: true
```

It fails when the change:

| Rule | Fails when |
|------|------------|
| `breaking` | It removes a column that a model, task or dashboard still reads, whatever `fail-on-breaking` says |
| `approved-definition-changed` | A term approved in `definitions.yml` gets a new meaning: its wording, columns, filters or grain change, or a column it names, or one upstream of it, is computed differently, removed, or gains or loses a row condition. The fix is for the term's owner to re-approve it |
| `conflicting-definition` | A column it adds or changes, or one that changes because of it (a new filter on its relation, a change upstream), computes a business idea differently from an existing column (another filter, source, join or aggregation), as [`konstruct conflicts`](conflicts.md) finds, unless the two are bound to different terms |
| `data-law-broken` | A data law that `konstruct verify --laws` found to hold on all the data no longer follows from the SQL of a model the change alters: a join on a key no test covers, a new filter, another factor, a `SUM` become an `AVG`, a removed model. The laws are read from `konstruct-out/laws.json` as committed at the base, so run `konstruct verify --laws` on the main branch and commit that file; the result says where the laws came from, or that none was checked (see [Laws in the Gate](verify.md#laws-in-the-gate)). The finding names the model |
| `consumer-pin-changed` | A column a repository listed in `repositories` pins in its `konstruct.lock` is altered by the change: its formula, row conditions, sources or type, or the approved term bound to it (see [Pinning What You Read](reviewing-changes.md#pinning-what-you-read)). The finding names the consumer and the command it runs to accept the change |

A conflict between columns the change does not touch does not fail it; a change to either side of an existing conflict does, until the two agree. Two columns bound to different terms in `definitions.yml`, such as net and gross revenue, are two definitions on purpose and do not fail it. In the Action the gate uses the graph built with `build-args`, so it runs no dbt the build did not. Each finding names the file and says how to fix it; the Action adds the findings to the review and annotates each one.

```text
$ konstruct gate
Gate failed: 1 finding to fix before proposing the change.

conflicting-definition: gross.revenue computes 'revenue' differently from ltv.revenue (filter
  WHERE: orders.status <> 'void')
  file  sql/gross.sql
  fix   Compute it the way the existing column does, or name the difference in definitions.yml.
```
