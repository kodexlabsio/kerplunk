# Business Definitions

Lineage says where a number comes from; it does not say what the business means by it. `definitions.yml` records that in the repository, next to the models, reviewed in pull requests like code:

```yaml
version: 1
terms:
  - name: Customer lifetime value
    kind: measure                 # entity, measure or term; inferred when left out
    synonyms: [lifetime value, LTV, CLV]
    definition: Total value of every payment a customer has made, in Australian dollars.
    owner: {name: Priya Shah, role: Head of Finance}
    status: approved              # draft, agreed, approved or retired
    approved: 2026-10-30
    grain: one row per customer
    columns: [customers.customer_lifetime_value, customer_ltv.lifetime_value]
    filters: ["orders.status <> 'returned'"]   # filters any answer must apply
    unit: currency
    currency: AUD
    related: [Order amount]
    broader: [Lifetime metric]    # the wider concepts it is a kind of
    exceptions: Refunds are not netted off.
    sensitivity: personal         # only for a term whose columns hold sensitive data
```

The build attaches each term to the columns it names, so the definition travels with the lineage:

```text
$ konstruct define lifetime value
Customer lifetime value  [approved, Priya Shah (Head of Finance), approved 2026-10-30]
  Total value of every payment a customer has made, across all orders and payment methods, in Australian dollars.
  also called: lifetime value, LTV, CLV
  grain: one row per customer
  unit: currency AUD
  exceptions: Refunds are not netted off; returned orders still count.
  related: Order amount, Customer
  defined in: definitions.yml:5
  columns:
    customer_ltv.lifetime_value  DOUBLE
    customers.customer_lifetime_value  DOUBLE
```

- **Agents get the agreed answer.** The `define` MCP tool returns the approved definition, owner, required filters and columns for a term or a column, and `query` puts definitions first, so an agent asked "what is our lifetime value?" starts from the wording the business approved.
- **Changes show what they touch.** `impact` lists the business terms a change reaches, so changing `stg_payments.amount` names *Customer lifetime value* and *Order amount*. `describe` shows the terms that define a column. graph.html searches terms by name and synonym and highlights their columns.
- **Definitions cannot drift silently.** The build warns, and fails under `--strict`, when a bound column or a column in a required filter no longer exists (naming the likely rename), when an approved term has no owner or approval date, when an agreed or approved term is bound to nothing, when two terms share a name or synonym, when a related or broader term is not defined, and when broader terms form a cycle. `REPORT.md` lists every term with its status, owner and columns.
- **Pull requests review them.** `konstruct diff` reports terms added, removed and changed field by field, and flags an approved term whose definition, grain, columns or filters changed while it stayed approved with the same date.

- **They mark sensitive data.** A term with a `sensitivity` marks the columns it binds as holding that kind of data, and the column lineage carries the mark to every column computed from them; `konstruct gate` fails a change that moves it into a model not marked to hold it, and `diff` reports a mark added or taken away. [Sensitive Data and Erasure](sensitivity.md)
- **They feed the ontology.** A term of kind `entity` names a business object, and a term of kind `measure` (or one bound to a computed column) is a measure, with each column's formula and verification in `ontology.yml`. The build warns when a measure's columns compute it differently. [The Ontology](ontology.md)

Konstruct reads every `definitions.yml` (or `.yaml`) in the repository; `[definitions] paths` in `konstruct.toml` pins the files. The example project has six terms in [`examples/jaffle_shop/definitions.yml`](https://github.com/kodexlabsio/konstruct/tree/main/examples/jaffle_shop/definitions.yml).

## Vocabularies

A company that already keeps its vocabulary in SKOS or OWL can point Konstruct at it instead of writing it again:

```toml
[vocabularies]
paths = ["vocabulary/business.ttl"]   # Turtle, N-Triples or RDF/XML; needs konstruct[rdf]
```

Each named `skos:Concept` and `owl:Class` becomes a term: its name is its English `skos:prefLabel` or `rdfs:label` (any English variant, then an untagged label), its other labels are synonyms, its definition is its `skos:definition` or `rdfs:comment`, and its broader terms are the concepts it names with `skos:broader` or `rdfs:subClassOf`, or that name it with `skos:narrower`, in any of the files listed. An OWL class is an entity; an anonymous class, such as an `owl:unionOf`, is not a term. Where `definitions.yml` already has a term of that name or synonym, the vocabulary only fills in what it leaves out (synonyms, broader terms, a definition), so the project's own wording, owner, status and columns win, and a vocabulary label that is already another term's name is not added; the vocabulary's other terms are added as drafts, bound to no column until a `definitions.yml` term of the same name binds them. A term from a vocabulary is shown with its file and IRI. The build warns about a broader concept no file defines, two concepts with the same label, and a file with no named concepts. Nothing a vocabulary names is fetched, and an RDF/XML file that declares XML entities is not read.
