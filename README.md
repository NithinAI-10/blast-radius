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

6. **TPC-DS shortcut** (`selector/tpcds_shortcut.py`, `tools/trace_phase2.py`,
   `tools/trace_phase3.py`). `test_execute_tpcds` parses each query, optimizes it
   inside the executor, runs it in pure Python and compares the rows with duckdb
   running `transpile(sql, write="duckdb")` (~38 s CPU; parse 0.9 s, optimize 3.3 s,
   transpile 0.5 s). On the pinned commit we traced the lines this test runs in each
   phase: parse, optimize, transpile, and everything else (imports, class setup,
   planning, execution, comparison). We also stored, per query, fingerprints of the
   parsed input and optimized plan (`selector/canon.py`, independent of sqlglot's own
   equality) and the duckdb SQL. When no changed line, and no line reading a changed
   class attribute or module constant, runs in the "everything else" phase, a query
   whose parsed input, plan and duckdb SQL all match the pinned commit's must run and
   compare the same, so only queries where one of them changed are executed. Optimize
   and transpile are re-run only when a changed line runs inside them; the CSV tables
   load only if some query executes. Queries run lazily, one at a time, and the first
   failure stops the test. Any non-Python or test-file change disables the shortcut.

7. **Verification** (`selector/runner.py`). Candidates whose running costs less
   than listing them are run with `unittest` in a forked child, against an
   overlay of the pinned tree with the patched files swapped in (symlinks
   elsewhere, so precompiled bytecode is reused). sqlglot's spawn process pools
   are replaced by an in-process pool that still pickles arguments and
   results. Anything that fails, errors, times out or never reports is printed.
   Whether to verify or just list is decided per commit from measured CPU per
   test (`tools/measure_cpu.py`) and a per-test failure prior (below).

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

**TPC-DS under the shortcut.** When the shortcut applies, `test_execute_tpcds` gets the
prior 0.2 instead of its per-test 0.4: of the 120 development mutants, 3 made it a
candidate with the shortcut applicable, and none failed it; smoothed like the others,
(0 + 4·0.35) / (3 + 4). `tools/tpcds_prior.py` lists those cases.

**How the planner uses them.** For each commit the selector estimates the cost of
listing every candidate (`partition`) against verifying some of them:
verification CPU + the prior × how much the worker schedule grows if that test fails
and is listed + the schedule of what is left. It takes the cheaper of two plans: the
best prefix of candidates ordered by CPU per listed second, and a greedy plan over the
heaviest tests plus the block of small ones.

Every verification cost the planner weighs (test CPU, class setup, per-child overhead)
is multiplied by `CPU_SCALE` = 1.2. The organizers' per-test seconds match the CPU we
measured for the same tests (ratio 0.92 over the 32 tests above 0.3 s, 103 s vs 101 s
over the whole suite), and the practice-board CPU of our four submissions fits a fixed
~2 s plus about 1.1x our local CPU. An earlier factor of 2.0 overcharged verification.

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
