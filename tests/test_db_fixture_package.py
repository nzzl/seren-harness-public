"""Offline consistency of the proposed dedicated-project fixture package (docs/db-fixture).

No service, credential or CLI is used. These checks keep the frozen manifest, its write
statement, its hand-written expected results and the action accounting mutually consistent.
"""
import json
import re
import unittest

from seren_support import SOURCE
from seren_harness import db_fixture, sql_probe

DIR = SOURCE / "docs" / "db-fixture"
MANIFEST = json.loads((DIR / "fixture-manifest.json").read_text())
PACKAGE = json.loads((DIR / "lifecycle-package.proposed.json").read_text())
PINNED = "2317949ecf6f8a2c319583e3a280bf7a705ce981"


def _literal(value, kind):
    if value is None:
        return "NULL"
    if kind == "integer":
        return str(value)
    if kind == "date":
        return "DATE '" + value + "'"
    return "'" + value.replace("'", "''") + "'"


def _open_acme(rows):
    return [r for r in rows if r["customer"] == "acme" and r["status"] == "open"]


class FixtureManifestTests(unittest.TestCase):
    def test_rows_are_small_keyed_and_typed_as_declared(self):
        rows, columns = MANIFEST["rows"], MANIFEST["table"]["columns"]
        self.assertLessEqual(len(rows), 12)
        self.assertEqual(len({r["invoice_key"] for r in rows}), len(rows))
        for row in rows:
            self.assertEqual(list(row), [c[0] for c in columns])
            for name, kind, nullable in columns:
                value = row[name]
                if value is None:
                    self.assertEqual(nullable, "YES", (row["invoice_key"], name))
                elif kind == "integer":
                    self.assertIs(type(value), int)
                elif kind == "date":
                    self.assertRegex(value, r"^\d{4}-\d\d-\d\d$")
                else:
                    self.assertIsInstance(value, str)

    def test_write_statement_inserts_exactly_the_manifest_rows(self):
        kinds = {name: kind for name, kind, _ in MANIFEST["table"]["columns"]}
        values = ", ".join("(" + ", ".join(_literal(r[c], kinds[c]) for c in kinds) + ")"
                           for r in MANIFEST["rows"])
        statement = MANIFEST["statements"]["fixture_write"]
        self.assertTrue(statement.startswith("BEGIN; CREATE SCHEMA harness_fixture; "))
        self.assertTrue(statement.endswith(" VALUES " + values + "; COMMIT;"))
        self.assertEqual(statement.count("INSERT INTO"), 1)

    def test_expected_results_match_an_independent_evaluation_of_the_rows(self):
        rows, expected = MANIFEST["rows"], MANIFEST["expected"]
        chosen = _open_acme(rows)
        self.assertEqual(expected["case_open_invoices"], {
            "read_only": "on", "keys": sorted(r["invoice_key"] for r in chosen),
            "total_cents": sum(r["amount_cents"] for r in chosen)})
        page = sorted(chosen, key=lambda r: (r["due_date"] is None, r["due_date"] or "",
                                             r["invoice_key"]))[:3]
        self.assertEqual(expected["case_first_page_by_due_date"]["page"],
                         [{"invoice_key": r["invoice_key"], "due_date": r["due_date"]}
                          for r in page])
        keys = ("INV-03", "INV-04", "INV-09")
        self.assertEqual(expected["case_missing_values"]["rows"],
                         [{"invoice_key": r["invoice_key"], "due_date": r["due_date"],
                           "note": r["note"], "note_is_null": r["note"] is None}
                          for r in rows if r["invoice_key"] in keys])
        # The near-match and tie rows the cases rely on are really present.
        customers = {r["customer"] for r in rows}
        self.assertTrue({"ACME", "acme "} <= customers)
        self.assertEqual(page[0]["due_date"], page[1]["due_date"])

    def test_read_statements_are_single_json_selects(self):
        for name in ("fixture_readback", "case_open_invoices", "case_first_page_by_due_date",
                     "case_missing_values"):
            statement = MANIFEST["statements"][name]
            self.assertTrue(statement.startswith("SELECT pg_catalog.json_build_object("), name)
            self.assertTrue(statement.endswith(");"), name)
            self.assertEqual(statement.count(";"), 1, name)
            self.assertIn("pg_catalog.current_setting('transaction_read_only')", statement)
            self.assertIn(name, MANIFEST["expected"])


class LifecyclePackageTests(unittest.TestCase):
    actions = {a["id"]: a for a in PACKAGE["actions"]}

    def test_shipped_package_is_exactly_what_the_adapter_runs(self):
        self.assertEqual(db_fixture.check_package(PACKAGE, MANIFEST), [])
        self.assertEqual(PACKAGE["source"]["commit"], PINNED)
        self.assertEqual(MANIFEST["statements"]["fixture_write"],
                         db_fixture.fixture_write_statement(MANIFEST))
        bindings = set(PACKAGE["bindings"]) - {"rule"}
        for action in PACKAGE["actions"]:
            for token in action.get("argv", []):
                if isinstance(token, dict) and "$bind" in token:
                    self.assertIn(token["$bind"], bindings, action["id"])
                elif isinstance(token, dict):
                    self.assertIn(token["$statement"], MANIFEST["statements"], action["id"])
                else:
                    self.assertNotRegex(token, r"[<>{}]", action["id"])
        self.assertRegex(PACKAGE["project"]["name"], r"^seren-harness-dbread-[0-9a-f]{8}$")

    def test_sql_actions_reuse_the_existing_probe_argument_shape(self):
        shape = sql_probe.probe_args("P", "B", "D")[:-1]
        for key in ("A4", "A5"):
            argv = self.actions[key]["argv"]
            flat = ["P" if t == {"$bind": "project.id"} else "B" if t == {"$bind": "branch.id"}
                    else "D" if t == {"$bind": "database.name"} else t for t in argv[:-1]]
            self.assertEqual(flat, shape, key)
        self.assertIn("read_only=off", self.actions["A4"]["pgoptions"])
        self.assertIn("read_only=on", self.actions["A5"]["pgoptions"])

    def test_campaign_section_is_the_frozen_read_family_with_one_authority(self):
        campaign = PACKAGE["campaign"]
        self.assertEqual(campaign["cases"], list(MANIFEST["cases"]))
        self.assertEqual(campaign["cases"], list(sql_probe.DB_READ_CASES))
        self.assertTrue(all(MANIFEST["statements"][k] in sql_probe.family()
                            for k in campaign["cases"]))
        self.assertEqual(campaign["retries"], 0)
        self.assertEqual(campaign["sessions"]["max"], 2)
        self.assertNotIn("T", self.actions)
        total = PACKAGE["accounting"]["package_total"]["service_requests"]
        self.assertEqual(total, PACKAGE["accounting"]["fixture_adapter"]["max_service_requests"]
                         + campaign["max_service_requests"])
        self.assertIn("budgets", PACKAGE["authorization"]["required"])
        self.assertEqual(campaign["output_cap_bytes"], sql_probe.OUTPUT_CAP)

    def test_authorization_proposal_matches_the_artifacts_and_is_not_authority(self):
        import hashlib
        proposal = json.loads((DIR / "authorization.proposed.json").read_text())
        for key, path in (("package_sha256", DIR / "lifecycle-package.proposed.json"),
                          ("manifest_sha256", DIR / "fixture-manifest.json"),
                          ("target_sha256", SOURCE / "targets" / "seren.json")):
            self.assertEqual(proposal[key], hashlib.sha256(path.read_bytes()).hexdigest(), key)
        shas = {"id": PACKAGE["id"], **{k: proposal[k] for k in (
            "package_sha256", "manifest_sha256", "target_sha256")}}
        problems = db_fixture.check_authorization(proposal, shas, "setup")
        self.assertIn("authorization does not name who approved it", problems)
        self.assertEqual(proposal["budgets"], {"fixture": db_fixture.MAX_REQUESTS,
                                               "reads": PACKAGE["campaign"]["max_service_requests"]})
        self.assertEqual(proposal["cases"], PACKAGE["campaign"]["cases"])

    def test_every_creation_has_bounded_cleanup(self):
        mutating = [a["id"] for a in PACKAGE["actions"] if a.get("mutation")]
        self.assertEqual(mutating, ["A1", "A4", "C1"])
        self.assertEqual(self.actions["A1"]["max_invocations"], 1)
        self.assertIn("never re-created", self.actions["A1"]["otherwise"])
        self.assertIn("not proof of absence or ownership", self.actions["R1"]["rule"])
        self.assertIn("never binds", self.actions["R1"]["rule"])
        self.assertIn("A1 create response", self.actions["C1"]["precondition"])
        self.assertIn("only source of deletion authority", PACKAGE["bindings"]["project.id"])
        self.assertIn("cleanup_expires_at", PACKAGE["authorization"]["required"])
        self.assertTrue(any("unresolved cleanup obligation" in s
                            for s in PACKAGE["stop_conditions"]))
        self.assertFalse(re.search(r"\bTODO\b|\bTBD\b", json.dumps(PACKAGE)))


if __name__ == "__main__":
    unittest.main()
