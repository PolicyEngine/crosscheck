# crosscheck

A shared row format for comparing tax-benefit models case by case.

Each comparison tool keeps running its own models and exports its answers as rows in one format. crosscheck holds that format, a store for the rows, and the reads that compare them. It runs no model and imports no engine: it depends on `pyarrow` and `jsonschema` and runs on Python 3.10 or later.

crosscheck stores nothing pairwise. A comparison is a query over rows: two runs' values for one cell, judged under a tolerance profile that the reader names when reading.

## What crosscheck is and is not

crosscheck is:

- a set of typed records (`crosscheck.records`) whose constructors refuse a row that breaks a rule, with a JSON Schema generated from them (`crosscheck.schema`, committed under `src/crosscheck/schemas/`);
- a `Table` of those records that sorts itself by key, lists every broken reference and merges with other tables (`crosscheck.table`);
- a registry that names each model once, so two tools' tables describe one model in the same words (`crosscheck.registry`);
- a store on disk: Parquet rows, a JSON registry file and a manifest of hashes (`crosscheck.store`);
- reads that compare runs under a tolerance profile (`crosscheck.tolerance`), group runs into lineages (`crosscheck.lineage`) and give one run's answer in one cell a status (`crosscheck.status`);
- exporters that turn two existing tools' output into rows (`crosscheck.exporters`).

crosscheck is not a model, a model runner or a judge of which model is right. It records what runs answered, how each case became each run's inputs, and why runs differ when someone has said why. It reports what other runs did; it never says an answer is right.

## Records

Every record is a frozen dataclass. A record accepts an enum member or its string value, refuses NaN and infinity, and raises `RecordError` naming the record, the field and the rule when a value breaks one. Equality is strict: `-0.0` differs from `0.0`, and `True` differs from `1.0`.

| Record | Key | What it holds |
|---|---|---|
| `PolicyWorld` | `id` | The law that models answer a case under: a jurisdiction, the date or year the law is read at (null when the source does not state it), and stated conventions. |
| `Case` | `(case_set, case_id, policy_world)` | One case of a case set under one policy world, its stated `Fact`s, and whether those facts are complete. |
| `Quantity` | `id` | What models answer: an amount, a boolean or a category, with its unit, period and entity type when the source states them, and each model's or tool's own name for it (`Alias`). |
| `Model` | `id` | A model: its kind (rules model, AI model, AI model with tools, official calculator, worked example), maintainer, engine family and known releases. |
| `Run` | `id` | One run of a model: its release, view, seed, sample or batch, the run it derives from, other engines behind its values (`EngineLink`: host, tool or gate), convention files, settings (`Switch`), whether the source kept every row, and its source files, each with a role (`Source`). |
| `Binding` | `(case_set, case_id, policy_world, run)` | How one case became one run's inputs: the input map layers and their author, what the map did and which facts the model could not take (`ScopedNote`), and inputs other runs computed (`BorrowedInput`). |
| `Value` | `(case_set, case_id, policy_world, quantity, entity, run)` | One run's answer in one cell: a status (`answered`, `not_covered`, `error`, `refused`, `unparsed`), the raw text, the parsed value, the source's own status word, and who produced it (`model`, `harness:<formula>`, or `not_stated`). |
| `LedgerEntry` | `id` | A cause of differences between runs: its kind, the runs and cells it covers (`LedgerCell`, which can name the run whose answer the cause moves and the size it predicts), citations to law, an issue link, who errs, and whether it is open or closed. |
| `ToleranceProfile` | `id` | A named tolerance: a method and one `ToleranceRule` (absolute and relative) per quantity or for every quantity (`"*"`). |

JSON Schema cannot state the rules that tie fields together, so the records enforce them:

- A value carries a parsed answer exactly when its status is `answered`, and an answered value keeps the source's raw text.
- No `Quantity`, `Value` or `Run` carries a tolerance.
- A ledger entry that names who errs gives grounds: a citation to law or an issue link.
- An AI author may open a ledger entry and may not close one. Only a person closes an entry: `close_entry(entry, by=..., by_kind="person")` returns a closed copy, any other author kind raises `LedgerRuleError`, and no record holds an entry that someone other than a person closed.

A ledger entry's cause is one of:

- `model_error`: a model departs from the law it sets out to apply.
- `missing_provision`: a model has no input for a stated fact, or no provision for the law of the year, so its answer leaves the provision out.
- `law_reading`: the law supports more than one reading and the models take different ones.
- `fact_reading`: the answer turns on how a fact of the case, stated or unstated, was read or filled in.
- `convention`: the models apply different stated conventions (take-up, uprating, a rounding rule).
- `input_map`: the difference arises where a case's facts become a model's inputs.
- `scope`: a model does not cover the quantity, or defines it differently.
- `policy_world`: the models answered under different law.
- `rounding`: numerical precision only.
- `explained_other`: the source tool marks the difference explained and gives no finer kind crosscheck can read.
- `unexplained`: nobody has explained it yet.

`Table.check()` raises `IntegrityError` listing every repeated key and every reference to a record the table does not hold (a value's run, case or quantity; a run's model and release; a derives-from cycle; a binding's borrowed run; a ledger entry's runs and cells; a tolerance rule's quantity). `Table.select_runs` cuts out the rows of some runs, and `Table.content_sha256()` hashes the table's canonical JSON, whatever order the records were built in.

## One table from several tools

`Table.merge(a, b, ...)` unions tables. Identical records collapse, and two different records with one key raise `IntegrityError`, with one exception: two `Model` records that differ only in their `releases` merge into one that lists both.

Two tools that describe one model in their own words would build two different records. `crosscheck.registry` gives each model one record (`known_model(id, releases)`) and translates each tool's own names (`registry_id(tool, name)`, `tool_name(tool, id)`). Both exporters take their models from it, so their tables merge. Where no source names a model's maintainer, the registry writes `not recorded`.

PolicyEngine's US and UK packages, and a source that names PolicyEngine without its country package, are three registry ids with one engine, `policyengine`. A wrong split would let one PolicyEngine run count as another lineage for a PolicyEngine cell; a wrong join only costs a reproduction.

## Cell status

`cell_status(table, subject_run, cell, profile)` gives one run's answer in one cell a status. It computes the status on read and stores nothing:

- `reproduced`: at least one run of another lineage, not an AI model, agrees, and none disagrees.
- `contested`: at least one run of another lineage, not an AI model, disagrees; or a closed ledger entry names the subject run as erring in the cell.
- `resting_on_one_model`: no run of another lineage, not an AI model, answered.

By default the candidates are the runs that hold an answer in the cell. The report lists the runs behind the status: `agreeing`, `disagreeing`, `same_lineage` (runs that share the subject's lineage, which count for nothing), `flagged_by` (AI-model runs that answered and disagree) and `set_aside`, with the ids of the open and closed ledger entries that cover the cell. An AI model's answer never supports or contests a cell; when it disagrees, it flags the cell for review.

Only a closed ledger entry changes a status, and only a person closes one. A closed entry names a run as erring in a cell when the run is in the entry's `who_errs` and one of the entry's cells for that cell names the run, or names no run. crosscheck sets a run named that way aside whether it agrees with the subject or not: its answer neither supports nor contests the cell. An open entry, whoever wrote it, changes nothing but the list of open entries.

No status says more than what other runs did. Two runs of separate lineages can still read the law the same way and both err, so agreement shows reproduction, not correctness. The package therefore never calls a value confirmed or verified: those two words appear nowhere under `src/`, and `tests/test_no_banned_words.py` fails if either does.

## Lineage

1. Each run carries tokens: `engine:` for its model's engine and every engine it links to, `conv:` for each convention file, `run:` for itself, and every token of the run it derives from.
2. Its bindings add `map:` for each input map layer and `run:` for each run it borrowed an input from, unless the binding says the input does not bear on the answer or names only other quantities.
3. Two runs share a lineage directly when their tokens meet; within a run set, lineage is the transitive closure of that, and links pass only through runs in the set.
4. `lineages(table, run_ids, case=..., quantity=...)` returns the partition, and `cell_status` scopes it to the cell's case and quantity.

crosscheck computes lineage from runs and bindings; no model record states it.

## Tolerance profiles

Tolerance lives in a named `ToleranceProfile`, and a comparison applies it on read. `rule_for(profile, quantity_id)` takes the rule for the quantity, else the `"*"` rule, else raises `ToleranceError`: there is no silent default.

`agree(profile, quantity, left, right)` returns false unless both values hold answers of the same Python type. Two amounts agree under the profile's method:

- `isclose`: `math.isclose(l, r, rel_tol=relative, abs_tol=absolute)`;
- `abs_or_ratio`: with `d = abs(l - r)` and `m = max(abs(l), abs(r))`, `d <= absolute`, or `m > 0` and `d / m <= relative`.

Booleans and categories agree only when equal. `compare_runs(table, left_run, right_run, profile)` returns one `Comparison` per cell where either run has a row, joined on case set, case, policy world, quantity and entity. `across_worlds=True` drops the policy world from the join, for tools that compare a model under one year's law with a model under another's; the caller asks for it by name. Nothing in `crosscheck.tolerance` writes anything or changes a table.

## Storage

```
<root>/manifest.json               schema, crosscheck version, table hash, one entry per file
<root>/registry.json               policy worlds, quantities, models, runs, ledger, profiles
<root>/cases/<case_set>.parquet    one file per case set
<root>/values/<run_id>.parquet     one file per run, written for every run
<root>/bindings/<run_id>.parquet   one file per run that has bindings
<root>/.gitignore                  "*.parquet", so the rows stay out of git
```

`write_store(table, root)` checks the table, writes every file and returns the manifest. It writes only into a root that is absent, empty or holds a previous store, and it gives byte-identical files for the same table under a fixed `pyarrow` version. `read_store(root)` checks every file against the manifest (sha256, bytes, rows and the table's content hash) and returns the table that `write_store` wrote, floats bit for bit. `check_manifest(root)` lists every difference, every missing file and every Parquet file the manifest does not list.

Commit `manifest.json` and `registry.json`. Never commit the Parquet rows: the store's own `.gitignore` keeps them out, and the manifest's hashes let anyone who holds the rows check that they are the rows it describes.

```python
from crosscheck import cell_status, compare_runs, read_store

table = read_store("rows/example")  # raises StoreError when a file differs from the manifest
for comparison in compare_runs(table, "run-a", "run-b", "profile-1"):
    if not comparison.agrees:
        print(comparison.case_id, comparison.quantity, comparison.difference)

report = cell_status(table, "run-a", ("cases-1", "c1", "us.2026", "income_tax", ""), "profile-1")
print(report.status, report.agreeing, report.disagreeing, report.set_aside, report.flagged_by)
```

## Command line

```
crosscheck schema --out DIR      write the JSON Schema of every record type
crosscheck check ROOT            check a store against its manifest; exit 1 on any problem
crosscheck summary ROOT          count a store's records per type and per run
crosscheck export <tool> ...     run an exporter (below)
```

## Exporters

Both exporters write `not_stated` as every value's producer, because neither source says which values the model itself produced and which code outside the model computed. Their test fixtures are synthetic: invented ids and numbers in the real shapes. The files under `evidence/` hold counts, hashes and run ids from passes over the real sources, and no value from them.

### axiom-oracles comparison reports

```
crosscheck export axiom-oracles REPORT.json --out ROOT [--source-uri URI]
```

`crosscheck.exporters.axiom_oracles` reads a comparison report from TheAxiomFoundation/axiom-oracles in one of three shapes: `pairwise` (a left and a right engine, with each case's matches and mismatches), `pairwise_rows` (one number per engine per row, with a row-level match) and `grid` (one number per engine per row, with one object per leg). It writes a run per engine side, a value per case, concept and side, a tolerance profile from the report's own numbers, a binding where the report keeps a model's inputs, and a ledger entry per disposition id. Any other shape raises `UnsupportedReport`, whose `reason` names why.

The command writes the store, reads it back, and runs `check_round_trip`, which walks the report again and checks that every compared value sits in the rows, that every verdict the report records recomputes under the report's own tolerance, and that every stated count recomputes when the report holds every comparison. A report that keeps only its mismatches exports as partial evidence: the rows hold what the report holds, which is too little to recompute its match counts.

`evidence/axiom_oracles_round_trips.json` (which `scripts/axiom_oracles_census.py` writes) records a pass over the 294 files on axiom-oracles' `oracle-data` branch at commit `d536cb8a4c9ddb1b690af0ea2d17e73d287dd0b0`:

| Outcome | Files |
|---|---:|
| Not a comparison report | 24 |
| Exported with full evidence; every value, verdict and stated count recomputes | 109 |
| Exported with partial evidence; every held value and verdict recomputes | 46 |
| Unsupported, with a reason | 115 |

The 109 comprise 49 pairwise reports, 49 grids and 11 row grids. Across the 155 exports the rows hold 25,204 report values, and 12,719 verdicts recompute with no difference. Of the 115 unsupported reports, 91 hold no compared values, 11 name an engine the registry does not know, 7 state no relative tolerance for an amount, 3 repeat a case row, 2 use a comparison kind other than amount or eligibility, and 1 compares several outputs within one concept. All 155 exports merge into one table that passes `Table.check()` (349 runs, 8 models).

### Key-check tables

```
crosscheck export keycheck RESULTS_DIR --out ROOT [--source-uri URI] [--models registry|as_written]
```

`crosscheck.exporters.keycheck` reads the `answers.csv`, `runs.csv` and `models.csv` tables of PolicyEngine/policybench-key-check, with `bindings.csv`, `causes.csv` and `cause_cells.csv` when they exist. The key-check built those tables by hand before this schema existed, so they test the schema. `differential` exports the tables, writes and reads a store, projects the rows back to the tables' columns and compares them cell by cell; `SCHEMA_GAPS` names every place where the tables and the schema do not meet.

`evidence/keycheck_differential.json` (which `scripts/keycheck_differential.py` writes) records the differential on branch `key-check-v1` at commit `127804602d9e8e9ec5fd70839111cb3aa45dfc28`. The key-check marks every table there interim, so each figure below describes that commit only, and none is a finding about any model's answers.

- The tables hold 5,400 answers, 9 runs, 3 models, 900 bindings and 31 causes. Every cell of `answers.csv` and `models.csv` comes back, and the table survives the store unchanged.
- All 1,893 cells that differ sit in seven columns the schema does not hold: `records_sent`, `model_itemizes`, `unexpressed_facts` and `map_computed` in `bindings.csv`, and `input_map`, `inputs_from_other_runs` and `shares_with_other_runs` in `runs.csv`.
- The 31 causes become ledger entries with 177 cells. Their ids, kinds, summaries, law citations, authors and statuses come back exactly. Five columns of `causes.csv` have no field (`release_or_build`, `fixed_in`, `law_figure`, `who_is_right`, `evidence`), as does `answerer` in `cause_cells.csv`. The exporter never infers who errs from prose, so no entry names a run as erring.
- The file lists 23 schema gaps in all.

The bindings say where a borrowed input bears on the answer, so lineage differs by case. Among the tax year 2026 runs, the Tax-Calculator and TAXSIM runs form one lineage apart from the PolicyEngine runs in 91 of 100 cases, and all six runs form one lineage in the other 9. The Tax-Calculator and TAXSIM runs share an input map layer in every case, so they never count as two lineages.

`compare_cell_status` sets the key-check's own hand-built status for 500 cells beside the status crosscheck computes. The two agree on 444 cells and differ on 56, each for a named rule:

- 36 cells: crosscheck joins two runs' lineages where one takes an input the other computed, and does so for every quantity of the case, because `bindings.csv` names no quantity for the input. The key-check joins runs only on a shared engine, convention file or input map file, and marks a reproduction that rests on the borrowed amount on the income tax lines.
- 16 cells: one run of the other lineage agrees and another stands apart. The key-check calls the cell reproduced; crosscheck calls it contested until a person closes a ledger entry that names the run standing apart.
- 5 cells: the key-check contests a cell on its cause entries or on the board's exclusion records, even where a run of another lineage agrees. Every cause entry is open, and in crosscheck an open entry changes no status.

One cell falls under two of those rules.

## Invariants and their tests

Each invariant has Hypothesis property tests in `tests/test_properties.py`, over tables that `tests/strategies.py` generates (non-ASCII text, `-0.0`, extreme floats, every enum value, derived runs, borrowed inputs, open and closed ledger entries). Each test's docstring states its invariant in one sentence.

| Invariant | Property tests |
|---|---|
| Export then read is the identity: `read_store` after `write_store` gives the table back, and every record survives JSON text. | `test_a_store_gives_back_the_table_it_was_given`, `test_table_json_text_gives_back_the_table`, `test_a_record_survives_json_text` |
| Row counts conserve: the manifest's counts sum to the table's rows, each run's file holds exactly its rows, the per-run sub-tables merge back with the registry, and an exported report keeps every compared value and its match counts. | `test_a_store_holds_every_row_once_in_its_own_file`, `test_per_run_sub_tables_and_the_registry_merge_back_into_the_table`, `test_an_exported_report_keeps_every_compared_value_and_its_match_counts` |
| Lineage is symmetric and transitive within a run set, and equals the closure of direct sharing. | `test_lineage_tokens_are_the_ones_the_spec_lists`, `test_lineages_partition_the_run_set`, `test_lineages_are_the_transitive_closure_of_direct_sharing`, `test_same_lineage_is_reflexive_symmetric_and_transitive`, `test_a_shared_token_never_splits_a_lineage` |
| A tolerance profile changes no stored value, and widening one never turns agreement into disagreement. | `test_comparing_under_a_profile_changes_no_stored_value`, `test_widening_a_profile_never_turns_agreement_into_disagreement`, `test_compare_runs_agrees_exactly_where_the_profile_rule_says` |
| Only a person closes a ledger entry. | `test_only_a_person_closes_a_ledger_entry`, `test_no_record_holds_an_entry_closed_by_anyone_but_a_person` |
| A cell status is one of three words; neither an AI model nor a run of the subject's lineage supports or contests a cell; an open ledger entry changes no status; closing one moves only the runs it names. | `test_cell_status_follows_the_spec_for_every_cell_and_subject`, `test_an_open_ledger_entry_changes_no_cell_status`, `test_closing_an_entry_moves_only_the_runs_it_names` |
| The content hash ignores build order. | `test_build_order_changes_no_hash_and_no_file` |
| Merge is commutative and associative, and joins a model's release lists. | `test_merge_is_commutative_and_associative`, `test_a_merge_lists_the_union_of_each_models_releases`, `test_tables_that_differ_only_in_releases_merge_into_their_union` |

Where two implementations of one rule exist, a differential test sets them side by side: `cell_status` against a second statement of its rule and `lineages` against a naive closure (`tests/test_properties.py`), `agree` against the two formulas above (`tests/test_tolerance.py`), each axiom-oracles export against the report's own verdicts and counts (`check_round_trip`), and the key-check export against the hand-built tables (`differential`). `tests/test_cross_tool.py` merges both exporters' tables into one.

The unit tests beside them (`tests/test_records.py`, `test_schema.py`, `test_table.py`, `test_registry.py`, `test_store.py`, `test_tolerance.py`, `test_lineage.py`, `test_status.py`, `test_cli.py` and the exporters' tests) pin each module's examples and refusals.

```
python -m pytest -q
```

Tests that read the real sources skip unless `CROSSCHECK_AXIOM_ORACLES_DATA` (a checkout of axiom-oracles' `dashboard/public/data`) or `CROSSCHECK_KEYCHECK_DIR` (a key-check `results` directory) names one.

## What the first slice does not do

- No dashboard: crosscheck holds and reads rows; it draws nothing.
- No exporter yet for policyengine-taxsim's comparisons or for the PRD comparison.
- No gates between models. An exporter adds no `gate` engine link by itself, because the sources do not name the engine, if any, that a model had to match. So an agreement between an Axiom run and a PolicyEngine run reads as a reproduction unless the caller declares the gate (`engine_links` in `export_report`), and a reader should not take it as one until then.
- No inference of who errs. The exporters read `who_errs` from no prose, so every exported ledger entry is open and names nobody; the ledger is a first cut.
- No input map, policy world or period on a run. A run-level input map lives on the run's bindings, and a run with no rows loses its law year.
- No field that says which quantities a borrowed input bears on unless the source states them; without that, the input joins lineages for every quantity of the case.
- No quantity registry across tools: each exporter names its own quantities, with each model's own names as aliases.
- No comparison kinds beyond amounts and exact equality, no output within a concept, and no sub-row within a case; the exporter refuses reports that need them.
- No tolerance relative to a case quantity (for example, a tolerance as a share of a case's income).
- No release on PyPI: another project holds the name `crosscheck` there.

## License

MIT.
