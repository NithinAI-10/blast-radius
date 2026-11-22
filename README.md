# Blast Radius entry

Picks the sqlglot tests a change can break, verifies the cheap ones by running
them against the patched tree, and prints only what fails (plus anything too
costly to verify), packed onto the worker count that minimises each commit's cost.

## How it works

1. **Map of the pinned commit** (`selector/map.bin`). Every test in
   `tests.json` was run alone, in its own process, at sqlglot
   `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`, under `sys.monitoring`. For
   each test we kept:
   - the lines its function code executed, and separately the lines it ran as
     import-time code (module and class bodies),
   - the sqlglot classes it called methods on (whole MRO),
   - the files it opened (fixtures, sources read as text).

   Built by `tools/trace_tests.py` and compiled by `tools/build_map.py`. Test sets
   are stored as bitsets. The map comes only from the pinned commit: there is
   no table from patches or kinds of edit to tests.

2. **Analysis of a patch** (`selector/analyze.py`). The patch is applied to the
   pinned tree and each change is placed in its AST context:
   - inside a function or lambda body → tests that executed that statement,
   - a function header or decorator → tests that ran the function body,
   - a class-body assignment → tests that used the class, narrowed to tests that
     ran a line reading that attribute name,
   - other module-level code → tests that imported the module,
   - comment-only or docstring-only edits → nothing,
   - a data file (fixtures) → tests that opened it.

3. **Fixture items** (`tools/trace_items.py`). Heavy tests that loop over fixture
   files (TPC-H/TPC-DS execution, optimizer fixture suites, identity) were also
   traced once with every fixture list empty and once per fixture item. A change
   outside the empty-run lines can only reach the items whose runs executed it,
   so the test is re-run with the fixture loaders restricted to those items
   (`selector/fixtures_patch.py`). A changed fixture file restricts to the items
   that differ.

4. **Surrogate** (`selector/surrogates.py`). `test_max_nodes` spends ~27 s on one
   2M-character parse whose node count is exactly 100*width + 1 against a limit
   of 100*width. A stand-in keeps every assertion and that exact relation at
   width 500 (~1.6 s). If it fails, the real test is listed.

5. **Weak-mutation probes** (`tools/trace_probes.py`). Every single-operator
   comparison in sqlglot (`is`, `is not`, `<`, `<=`, `>`, `>=`) was instrumented
   on the pinned commit, recording per test whether swapping the operator
   (`is`→`==`, `is not`→`!=`, `<`↔`<=`, `>`↔`>=`, and `and`↔`or` on side-effect-free
   operands, and flipping one boolean operand) would ever have produced a
   different value. When a patch is exactly such a swap, only tests where the
   values diverged can change outcome. Comparisons evaluated at import time are
   excluded from this rule.

6. **TPC-DS shortcut** (`selector/tpcds_shortcut.py`, `tools/trace_phase.py`).
   `test_execute_tpcds` parses and optimizes each query (~3 s in all), then
   plans and runs it in pure Python (~27 s). We traced which lines this test runs
   outside parse/optimize. When a patch touches none of them, a query whose
   parsed input and optimized output fingerprint (`selector/canon.py`, independent
   of sqlglot's own equality) match the pinned commit's must execute the same, so
   only queries with a changed plan are executed. Queries run lazily, one at a
   time, and the first failure stops the test.

7. **Verification** (`selector/runner.py`). Candidates whose running costs less
   than listing them are run with `unittest` in a forked child, against an
   overlay of the pinned tree with the patched files swapped in (symlinks
   elsewhere, so precompiled bytecode is reused). sqlglot's spawn process pools
   are replaced by an in-process pool that still pickles arguments and
   results. Anything that fails, errors, times out or never reports is printed.
   Whether to verify or just list is decided per commit from measured CPU per
   test (`tools/measure_cpu.py`) and a per-test failure prior (below).

## Lookup-table entries

sqlglot keeps much of its dialect behaviour in dicts defined in class bodies
(`SHOW_PARSERS`, `NUMERIC_ESCAPES`, time-format and type mappings). A patch that only
changes constants inside one entry's value can only change tests that read that entry.

**The map** (`tools/trace_dictkeys.py`, folded in by `tools/build_dictkeys.py`). On the
pinned commit, class creation is hooked before sqlglot is imported: once a class body
has run, each dict it defines is replaced by a recording subclass with the same items,
so metaclasses and later class bodies read through it too. Every test then runs in its
own forked child, and we record which keys of which named tables it reads by value
(`d[k]`, `get`, `setdefault`, `pop`), and which tables it reads whole (`items`, `values`,
copies, comparisons, pickling). Membership tests and key iteration are not recorded,
since a changed value cannot change them. All 1620 tests pass under the recording dicts.

**When it is used** (`selector/analyze.py`, `dict_entry_changes`). Only when the old and
new dict literal have the same keys and differ only in constants inside entry values,
and the table is not flagged. A table is flagged when: something besides its class
still references the original dict after the swap; its own class body uses its bare
name other than for keys (that read happens before the swap, e.g.
`TYPE_MAPPING = {**STRING_TYPE_MAPPING}`); or any of its values are read while sqlglot
imports, other than by a `{**Parent.NAME, ...}` copy into a table of the same name.
A key read at import that way flags just that key. Candidates are then the tests that
read one of the changed keys from any table of that name, plus tests that read such a
table whole.

**Validation.** `tools/gen_dict_mutants.py` wrote 90 such mutants in two batches (seeds
11 and 23). Each was labeled by running every candidate of the previous analysis
against it; the in-body rule above came from the first batch. On the second batch, 46
of 50 were narrowed, 39 broke at least one test, and none missed a failing test. The
two official-sample commits of this kind go from 273 and 32 candidates to 1 and 0.

## Required-argument flags

An expression class's `arg_types` maps each argument to whether it is required. Its
values are read in exactly one place: when the class is created, to build
`required_args`, which only `Expression.error_messages` uses, to report a required
argument that is missing (`tools/build_required.py` checks this on the source and
disables the rule otherwise). So flipping `arg_types[k]` of a class can only change a
test that validates an instance of an affected class while `k` is missing.

`tools/trace_required.py` records, per test, every (class, argument) that was missing
whenever `error_messages` ran. The affected classes are the class itself, subclasses
that inherit its `arg_types`, and classes whose `arg_types` copies it with
`{**X.arg_types, ...}`, transitively. When a patch changes only constants among the
values of one `arg_types` literal, the candidates are the tests that validated an
affected class with a flipped argument missing; a value change that keeps the same
truth value selects nothing. All 1620 tests pass under the trace.

**Validation.** 18 such commits in our two labeled mutant batches and the sample, and a
fresh batch of 40 (`ONLY_NAME=arg_types tools/gen_dict_mutants.py`, seed 31, labeled by
running every candidate of the previous analysis): no missed failing test; 24 of the
40 broke at least one test.

## Class-level constants

Dialects, parsers, generators and tokenizers are configured by class-level constants
(`SAFE_DIVISION = True`, `WEEK_OFFSET = -1`, ...). Each dialect sets its own, and a
lookup on a dialect resolves to the nearest class that defines the name, so a change to
one class's constant matters only to tests whose lookups reach that class's value.

`tools/trace_attrs.py` hooks class creation before sqlglot is imported and puts each
such constant into its class as a descriptor that records the read and returns the same
value, keyed by the defining class (`module:Qualname.NAME`). Reads by metaclasses,
`__init_subclass__` and any code run while importing (such as constants parsed at
import with the default dialect) are recorded separately, and those keys are never
narrowed, nor are constants their own class body reads by bare name (that read happens
before the descriptor exists). Enums and the four modules with dataclasses or
NamedTuples are not instrumented, and only instrumented constants are narrowed. All
1620 tests pass under the trace.

When a patch changes only the value of one such constant (a literal or a signed
number), the candidates are the tests that read that class's value.

**Validation.** 26 such commits in our labeled mutant batches, and a fresh batch of 50
(`tools/gen_attr_mutants.py`, seed 41, labeled by running every candidate of the
previous analysis; 38 broke at least one test): no missed failing test.

## Running only the checks a change reaches

Most dialect tests are long runs of independent checks (`validate_identity`,
`validate_all`, `validate_transpile` from `tests/dialects/test_dialect.py`).
`tools/trace_calls.py` runs every check of every such test in its own forked child,
starting from a state where sqlglot and the tests are imported but no check has run,
with all other checks skipped, and records the sqlglot functions it starts and the
class-level constants it reads. Since each check is traced from scratch, whatever it
needs that caches would otherwise carry over from earlier checks is included. Code the
test runs outside its checks is recorded separately, and a test whose outcome changes
when checks are skipped is never restricted (25 of 1020).

When a change is plain runtime code (or a class-level constant, above), each changed
line maps to the innermost function holding it, and a candidate Validator test is
verified running only the checks that start one of those functions (or read that
constant); the rest are skipped (`selector/callskip.py`). A skipped
`validate_identity` returns a stand-in that runs the real check if the test touches
its result. If a changed function also runs outside the checks, was never seen from
this test's checks, or the change is anything else, the test runs whole.

## Failure priors and the planner

**What the priors are.** `map.bin` holds one number per test: how likely that test is
to fail *when it is a candidate*. It is keyed by test only: no patch, file, line or
kind of edit appears in it, and it never decides which tests are listed, only whether
listing or verifying a candidate is cheaper.

**How they were generated.** `tools/gen_mutants.py` wrote 120 mutants of the pinned
sqlglot (operator swaps, constant changes, comment and fixture edits; seed 7). Each was
labeled by running the pinned test suite against it (`tools/label.py`). Then
`NO_SAMPLE=1 python tools/calib.py <those 120 labels>` ran our own candidate analysis on
each mutant and, per test, smoothed failures / candidacies toward a size prior
(`p = (fails + 4·p0) / (candidacies + 4)`, `p0` = 0.35 for tests over 0.3 s, else 0.06;
fixed `p0` for the three slowest tests, which the labels skip). The organizers'
sample answers and our second, held-out mutant batch were not used.

**Fixture-edit candidates.** A test that is a candidate only because a fixture file it
reads was edited gets one shared prior instead of its per-test one. It was measured the
same way, on the same 120 development mutants: 31 of the 32 such candidates failed,
smoothed to (31 + 4·0.5) / (32 + 4). Like every prior, it only decides whether
verifying such a test is worth the CPU, never whether the test is listed.

**How the planner uses them.** For each commit the selector estimates the cost of
listing every candidate (`partition`) against verifying some of them:
verification CPU + the prior × the listed seconds of each verified test (it is
listed anyway if it fails) + the schedule of what is left. It takes the cheaper of
two plans: the best prefix of candidates ordered by CPU per listed second, and a
greedy plan over the heaviest tests plus the block of small ones, which counts how
much the actual worker schedule shrinks when each is verified and passes.

Every verification cost the planner weighs (test CPU, class setup, per-child overhead)
is multiplied by `CPU_SCALE` (1.5), set from practice-board runs of this same selector at
different values (a diagnostic run that verified nearly everything showed how much
listed time verification can remove).

**Mop-up.** After verification, a commit whose verified tests all passed has probably
broken nothing, so the cheap tests still listed (each listing costs at least a 2 s
worker) are reconsidered. Commits break something or nothing as a whole, so we update
a commit-level break rate on the evidence: of the 120 development mutants, 88 had
candidates and 77 of those failed one, smoothed to (77 + 1) / (88 + 2). With every
verified test passed, the chance the rest pass rises, and they are verified if that
costs less than the listing it is expected to save. Per-test timeouts use real CPU
estimates, not the planning weight.

## Dependencies

- sqlglot at commit `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`, downloaded from
  GitHub during the build and checked against `selector/base.sha256`.
- The packages in `requirements.lock` (the organizers' labeled environment), from PyPI.
- Python standard library only in the selector itself.

## Try it

```sh
make sample
```

## License

MIT. See `LICENSE`.
