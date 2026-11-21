"""Cheaper stand-ins for tests that are too slow to run in full.

tests/test_parser.py::TestParser::test_max_nodes parses a 2M-character query
whose node count is exactly 100*width + 1 against a limit of 100*width, so it
raises by a margin of one node. The stand-in keeps every assertion and the same
count-vs-limit relation (width 500, limit 50_000) at about 3% of the cost.
If the stand-in fails we list the real test; if it passes we trust it.
"""


def _max_nodes(mod):
    parse_one = mod.parse_one
    exp = mod.exp
    ParseError = mod.ParseError

    def test_max_nodes(self):
        self.assertIsInstance(parse_one("SELECT 1", max_nodes=1_000_000), exp.Select)

        cols = [f"col{i}" for i in range(500)]
        sql = f"SELECT {','.join(cols)} FROM t"
        self.assertIsInstance(parse_one(sql, max_nodes=1_000_000), exp.Select)

        depth = 100
        expr = "x" + "=x" * depth

        width = 500  # original: 10000 with max_nodes=1_000_000 (= width * depth)
        sql = f"SELECT {','.join([expr for _ in range(width)])}"
        with self.assertRaises(ParseError) as ctx:
            parse_one(sql, max_nodes=width * depth)
        self.assertIn("Maximum number of AST nodes", str(ctx.exception))

        width = 1000  # original: 10000; the 50_000 limit is still hit mid-query
        sql = f"SELECT {','.join([expr for _ in range(width)])}"
        with self.assertRaises(ParseError) as ctx:
            parse_one(sql, max_nodes=50_000)
        self.assertIn("(50000)", str(ctx.exception))

        with self.assertRaises(ParseError):
            parse_one("SELECT " + ",".join(f"x={i}" for i in range(100)), max_nodes=5)
        self.assertIsInstance(parse_one("SELECT 1, 2, 3"), exp.Select)

    return test_max_nodes


SURROGATES = {
    ("tests.test_parser", "TestParser", "test_max_nodes"): _max_nodes,
}
SURROGATE_CPU = {
    "tests/test_parser.py::TestParser::test_max_nodes": 1.6,
}


def apply(mod, cls, meth):
    import os

    if os.environ.get("BR_NO_SURROGATE") == "1":
        return
    f = SURROGATES.get((mod.__name__, cls.__name__, meth))
    if f is not None:
        setattr(cls, meth, f(mod))
