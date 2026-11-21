# Blast Radius entry

Selects which sqlglot tests a patch can break, using maps built from the pinned commit
(`eb11aec4be8cb2122afe713a4816662c3f0fb2fa`). No tests are run at selection time.

## How it decides

| Change | Tests chosen |
| --- | --- |
| line inside a function | tests that executed that line (per-test line coverage), widened to the whole statement / `if` header so short-circuited lines count; a line no test runs selects nothing |
| line that runs once (import-time builder code, caches) | everything its outermost enclosing function and closures reach |
| class-level dict / set entry (`FUNCTIONS`, `TRANSFORMS`, token sets...) | tests that looked that key up on that class or a subclass (runtime trace) |
| class-level scalar setting (`WEEK_OFFSET = -1`) | tests that read that attribute on that class or a subclass (runtime trace) |
| expression class setting (`arg_types`, names, flags) | tests that instantiate that expression class or a subclass (runtime trace) |
| docstring text | nothing |
| `tests/fixtures/...` file | tests that opened that file; data read during class setup goes only to the tests in that class that name it (e.g. `tpc-h` data to the `tpch` tests) |
| test file | the edited test functions |
| removed import | tests running lines in that file that still use the name |
| comment / docstring / new unused helper | nothing (unless the comment lands inside a multi-line string) |
| `x is None` <-> `x == None`, `is` <-> `==` against an enum member | nothing: the two can't behave differently |
| one-line dict (`{"x": ..., "u": ...}`) | only the entry whose columns changed |
| `sqlglot/typing/`, core imports, `sqlglotc/` | also the few tests that work in subprocesses or read source files |
| any `sqlglot/` or `tests/` `.py` file | rebuilt from the bundled pinned sources (`src.pkl.gz`) and each changed region compared as an AST inside its smallest enclosing def / statement: a file that no longer parses selects every test; an identical AST (comments, formatting, parentheses, docstrings, annotations under `from __future__ import annotations`) selects nothing |
| test module outside a test (`setUp`, helpers, `Validator`) | every test of that class and its subclasses; module level (`tests/helpers.py`...): the module and the importers that use the changed name |
| class header (`class X(Base):`) | every test that uses the class or a subclass |
| decorator added above an existing def | every test that ran that def |
| new module-level def / class / import | lines using a name it shadows (and the module's classes when the name is also used at import time); `from sqlglot.x import y` of a missing name counts as the module failing to import |
| pure rename / binary file | the old path counts as deleted / tests that read the file |
| enum member | removed or renamed while referenced: everything; new value: tests mentioning it and users of the class it names |
| entry of a module-level table (`DIALECTS`) | what uses that key; other module-level assignments follow the names computed from them |
| `test_execute_tpcds` (21 s) selected | dropped when every change sits in function bodies that the test runs only while parsing the fixture SQL, optimizing it, or transpiling it for duckdb (stage map: `tools/trace_tpcds_phases.py`), and a forked child shows each stage still produces identical output for every query (parsed / optimized trees incl. types, duckdb SQL), against a baseline the image build computes with the same code (`tpcds_gate_child.py`); only queries whose optimize() runs a changed line are optimized (`tools/trace_tpcds_queries.py`) |
| function body that runs while classes are built (metaclass `__new__`, `__init_subclass__`, helpers called from class bodies) | every test using the classes under construction when it ran, and their subclasses (`classbuild_map.pkl.gz`, `tools/trace_classbuild.py`) |
| signature line (parameters, defaults on their own line, decorators) | every test that ran the function: defaults are evaluated once at def time |
| entry of a type-metadata table (`sqlglot/typing/`) | tests that create nodes of the expression classes the entry names (and subclasses); for `test_execute_tpcds`, an optimize() check over every query (only optimize() annotates types there) |
| line that runs at import (`import sqlglot`, a test module's collection, module / class level) | if the selection lacks the tests that would fail, the patched modules are imported in a subprocess against a precompiled pinned tree (`collect_map.pkl.gz`, `tools/trace_collect.py`); when that raises: those tests, or everything for the package import |

All helper processes import `unittest` first: sqlglot turns on extra validation (`UNITTEST`) under a test runner.

Line comparison (`equivalent`) is whitespace-insensitive only between tokens: indentation and the text of string
literals must match exactly.

Once a commit has a worker, every test recorded at 0 ms is added too: it costs nothing and
can only prevent a miss.

Coverage and traces were recorded with the optimizer/executor process pools replaced by an
in-process pool that still pickles arguments and results, so work done in child processes
(and serde used by pickling) is attributed to the right test.

Tests are split across workers with longest-first packing, choosing the worker count (1-8)
that minimises `max load + 2 s x workers`.

## Files

- `main.py`, `select_tests.py`: the selector (standard library only)
- `index.pkl.gz`: the maps (about 2.4 MB); `prep_index.py` decompresses it and precomputes derived tables at image build
- `src.pkl.gz`: pinned sqlglot + test sources and statement spans (`tools/build_src.py`)
- `test_struct.json`: test-module imports, classes, helpers (`tools/build_test_struct.py`)
- `tools/`: scripts that build the maps from the pinned commit

## Rebuilding the maps

```sh
git clone https://github.com/tobymao/sqlglot && cd sqlglot && git checkout eb11aec4be8cb2122afe713a4816662c3f0fb2fa
pip install -r ../requirements.lock pytest-cov
# coverage with per-test contexts (writes .coverage per tools/.coveragerc; edit data_file path)
SKIP_INTEGRATION=0 PYTHONPATH=../tools python -m pytest tests -p inline_pool --cov --cov-config=../tools/.coveragerc --cov-report= --durations=0 > durations.txt
# attribute reads / key lookups / file reads per test
SKIP_INTEGRATION=0 PYTHONPATH=../tools TRACE_OUT=trace.pkl python -m pytest tests -p inline_pool -p conftest_trace
python ../tools/export_map.py            # .coverage -> covmap.json (edit paths at top)
python ../tools/build_index.py  . covmap.json durations.txt index.pkl
python ../tools/build_index2.py . trace.pkl index.pkl
python ../tools/build_index3.py . trace.pkl index.pkl   # also writes index.pkl.gz
# second coverage run in reverse order with another hash seed (-p reverse_order, PYTHONHASHSEED=12345),
# then compare and merge: lines whose tests change with order run once (imports, caches)
python ../tools/cmp_order.py && python ../tools/build_index4.py index.pkl order.pkl odep.pkl
```

## Checks done

- sample: 0 misses, 103.9 simulated (the floor, picking exactly the failing tests, is 94.4)
- about 80 bugs injected the same way as the sample (operator flips, constants, fixture edits,
  comments), labelled by running the suite: 0 misses
- 405 patches (sample, injected, and real sqlglot history) run through the selector without an error

## Local check

`make sample` (needs Docker). Sample result: 0 misses, 103.9 simulated seconds,
about 0.25 s CPU.
