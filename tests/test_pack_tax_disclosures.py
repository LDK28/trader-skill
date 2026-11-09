import copy
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def load(name, skill, filename):
    path = ROOT / "skills" / skill / "scripts" / filename
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TAX = load("pack_tax", "trader-tax", "lots.py")
DISCLOSURES = load("pack_disclosures", "trader-pelosi", "disclosures.py")
TAX_EXAMPLE = ROOT / "skills/trader-tax/examples/transactions.synthetic.json"
DISCLOSURE_EXAMPLE = ROOT / "skills/trader-pelosi/examples/disclosures.synthetic.json"


def tax_fixture():
    return json.loads(TAX_EXAMPLE.read_text(encoding="utf-8"))


def disclosures_fixture():
    return json.loads(DISCLOSURE_EXAMPLE.read_text(encoding="utf-8"))


class RussianFifoTests(unittest.TestCase):
    def test_fifo_partial_sale_includes_buy_and_sell_fees(self):
        result = TAX.evaluate(tax_fixture())
        self.assertEqual(result["status"], "LEDGER_CALCULATED")
        sale = result["sales"][0]
        self.assertEqual(sale["fifo_basis_rub"], "1312")
        self.assertEqual(sale["realized_gain_rub"], "476")
        self.assertEqual([row["quantity"] for row in sale["fifo_allocations"]], ["10", "2"])
        self.assertEqual(result["remaining_lots"][0]["quantity"], "3")
        self.assertEqual(result["remaining_lots"][0]["basis_rub"], "453")

    def test_full_residual_sale_conserves_basis(self):
        data = tax_fixture()
        extra = copy.deepcopy(data["transactions"][2])
        extra.update(id="sell-rest", occurred_at="2026-02-02T10:00:00+03:00", tax_date="2026-02-02",
                     quantity="3", gross_amount="600", fees="3")
        data["transactions"].insert(3, extra)
        result = TAX.evaluate(data)
        self.assertEqual(result["status"], "LEDGER_CALCULATED")
        self.assertEqual(result["remaining_lots"], [])
        self.assertEqual(sum(Decimal(row["fifo_basis_rub"]) for row in result["sales"]), Decimal("1765"))
        self.assertEqual(result["basket_year_totals"][0]["realized_gain_rub"], "620")

    def test_historical_fx_is_per_transaction_and_not_revalued(self):
        data = tax_fixture()
        buy = copy.deepcopy(data["transactions"][0])
        buy.update(quantity="1", gross_amount="100", fees="1", currency="USD",
                   fx_to_rub={"rate": "80", "date": buy["tax_date"], "source_url": "https://www.cbr.ru/example-buy"})
        sale = copy.deepcopy(data["transactions"][2])
        sale.update(quantity="1", gross_amount="120", fees="1", currency="USD",
                    fx_to_rub={"rate": "90", "date": sale["tax_date"], "source_url": "https://www.cbr.ru/example-sale"})
        data["transactions"] = [buy, sale]
        result = TAX.evaluate(data)
        self.assertEqual(result["sales"][0]["fifo_basis_rub"], "8080")
        self.assertEqual(result["sales"][0]["realized_gain_rub"], "2630")

    def test_dividend_not_mixed_into_realized_gain(self):
        result = TAX.evaluate(tax_fixture())
        self.assertEqual(result["basket_year_totals"][0]["realized_gain_rub"], "476")
        self.assertEqual(result["separate_income_events"][0]["gross_rub"], "50")
        self.assertEqual(result["separate_income_events"][0]["tax_treatment"], "NOT_AUTOMATICALLY_DETERMINED")
        self.assertFalse(result["cross_basket_loss_netting_applied"])

    def test_separate_baskets_remain_separate_and_year_buy_carried_forward(self):
        data = tax_fixture()
        extra_buy, extra_sell = copy.deepcopy(data["transactions"][1:3])
        for row in (extra_buy, extra_sell):
            row["instrument_uid"], row["tax_basket"] = "other", "other_basket"
            row["quantity"] = "1"
            row["fees"] = "0"
        extra_buy.update(id="other-buy", occurred_at="2026-04-01T10:00:00Z", tax_date="2026-04-01", gross_amount="200")
        extra_sell.update(id="other-sell", occurred_at="2026-04-02T10:00:00Z", tax_date="2026-04-02", gross_amount="100")
        data["transactions"].extend([extra_buy, extra_sell])
        result = TAX.evaluate(data)
        self.assertEqual(len(result["basket_year_totals"]), 2)
        self.assertEqual({row["realized_gain_rub"] for row in result["basket_year_totals"]}, {"476", "-100"})
        self.assertTrue(all(row["tax_year"] == 2026 for row in result["sales"]))

    def test_incomplete_or_bad_basis_blocks_whole_ledger(self):
        for change in ("oversell", "missing-buy", "duplicate", "reverse", "unknown", "negative", "float", "missing-fees"):
            with self.subTest(change=change):
                data = tax_fixture()
                if change == "oversell": data["transactions"][2]["quantity"] = "16"
                elif change == "missing-buy": del data["transactions"][0]
                elif change == "duplicate": data["transactions"][1]["id"] = data["transactions"][0]["id"]
                elif change == "reverse": data["transactions"][0:2] = reversed(data["transactions"][0:2])
                elif change == "unknown": data["transactions"][1]["kind"] = "SPLIT"
                elif change == "negative": data["transactions"][0]["quantity"] = "-1"
                elif change == "float": data["transactions"][0]["gross_amount"] = 1000.0
                else: del data["transactions"][0]["fees"]
                result = TAX.evaluate(data)
                self.assertEqual(result["status"], "BLOCK")
                self.assertNotIn("sales", result)

    def test_missing_or_wrong_fx_date_blocks(self):
        for fx in (None, {"rate": "90", "date": "2026-01-01", "source_url": "https://www.cbr.ru/example"}):
            data = tax_fixture()
            data["transactions"][2]["currency"] = "USD"
            if fx is not None: data["transactions"][2]["fx_to_rub"] = fx
            self.assertEqual(TAX.evaluate(data)["status"], "BLOCK")

    def test_tax_date_cannot_reverse_documented_acquisition_and_sale(self):
        data = tax_fixture()
        data["transactions"][2]["tax_date"] = "2025-01-01"
        self.assertEqual(TAX.evaluate(data)["status"], "BLOCK")

    def test_tax_policy_missing_or_wrong_does_not_destroy_ledger(self):
        for policy in (None, {}, {"verified": True, "tax_year": 2030}):
            data = tax_fixture()
            data["estimate_policy"] = policy
            result = TAX.evaluate(data)
            self.assertEqual(result["status"], "LEDGER_CALCULATED")
            self.assertEqual(result["tax_estimate"]["status"], "TAX_ESTIMATE_UNSUPPORTED")
            self.assertEqual(result["sales"][0]["realized_gain_rub"], "476")

    def test_explicit_fts_reference_only_applies_to_separately_verified_base(self):
        data = tax_fixture()
        policy = {"profile_id": TAX.RATE_PROFILE, "tax_year": 2026, "residency": "ru_tax_resident",
                  "scope": "verified_aggregate_passive_base", "verified": True, "residency_verified": True,
                  "all_relevant_income_accounted": True, "taxable_base_verified": True,
                  "basket_and_loss_rules_verified": True, "taxable_base_rub": "2500000",
                  "taxable_base_source_ref": "SYNTHETIC separately verified aggregate base",
                  "source_url": TAX.RATE_SOURCE, "source_checked_on": TAX.RATE_CHECKED_ON}
        data["estimate_policy"] = policy
        estimate = TAX.evaluate(data)["tax_estimate"]
        self.assertEqual(estimate["status"], "ILLUSTRATIVE_TAX_ESTIMATE")
        self.assertEqual(estimate["amount_rub"], "327000")
        self.assertFalse(estimate["base_derived_automatically_from_ledger"])
        policy["residency_verified"] = False
        self.assertEqual(TAX.evaluate(data)["tax_estimate"]["status"], "TAX_ESTIMATE_UNSUPPORTED")

    def test_input_not_mutated_and_future_tax_year_rows_rejected(self):
        data = tax_fixture()
        original = copy.deepcopy(data)
        TAX.evaluate(data)
        self.assertEqual(data, original)
        data["transactions"][-1]["tax_date"] = "2027-01-01"
        self.assertEqual(TAX.evaluate(data)["status"], "BLOCK")


class PublicDisclosureTests(unittest.TestCase):
    def test_index_preserves_ranges_unknown_dates_and_document_sources(self):
        result = DISCLOSURES.review(disclosures_fixture(), new_only=False)
        self.assertEqual(result["status"], "DISCLOSURES_REVIEWED")
        self.assertEqual(result["count"], 2)
        first = next(row for row in result["events"] if row["event_date"] is not None)
        self.assertEqual(first["publication_lag_calendar_days"], 2)
        self.assertEqual(first["amount_range"], {"currency": "RUB", "lower": "1000000", "upper": "2000000"})
        self.assertFalse(first["is_proven_trade"])
        self.assertIsNone(result["events"][0]["event_date"])
        self.assertFalse(result["rf_congress_trade_registry_claimed"])

    def test_seen_state_deduplicates_and_surfaces_revision(self):
        data = disclosures_fixture()
        first = DISCLOSURES.review(data)
        state = first["next_state"]
        original_state = copy.deepcopy(state)
        self.assertEqual(DISCLOSURES.review(data, state)["count"], 0)
        data["events"][0]["title"] = "Исправленная синтетическая публикация"
        updated = DISCLOSURES.review(data, state)
        self.assertEqual(updated["count"], 1)
        self.assertEqual(updated["events"][0]["change"], "UPDATED")
        self.assertIn("previous_fingerprint", updated["events"][0])
        self.assertEqual(state, original_state)

    def test_issuer_person_and_since_filters_do_not_mark_other_rows_seen(self):
        data = disclosures_fixture()
        result = DISCLOSURES.review(data, issuer="учебный", person="участник", since="2026-11-08")
        self.assertEqual(result["count"], 1)
        self.assertEqual(len(result["next_state"]["seen"]), 1)
        self.assertEqual(DISCLOSURES.review(data, result["next_state"])["count"], 1)
        self.assertEqual(DISCLOSURES.review(data, issuer="absent")["count"], 0)

    def test_same_id_other_source_is_not_collapsed(self):
        data = disclosures_fixture()
        state = DISCLOSURES.review(data)["next_state"]
        data["source_id"] = "another-public-provider"
        self.assertEqual(DISCLOSURES.review(data, state)["count"], 2)

    def test_exact_duplicate_collapses_but_conflicting_duplicate_blocks(self):
        data = disclosures_fixture()
        data["events"].append(copy.deepcopy(data["events"][0]))
        self.assertEqual(DISCLOSURES.review(data)["count"], 2)
        data["events"][-1]["title"] = "Conflicting same ID"
        self.assertEqual(DISCLOSURES.review(data)["status"], "BLOCK")

    def test_nonpublic_missing_source_or_impossible_range_blocks(self):
        for change in ("source", "private", "future", "range", "mode", "float-range"):
            data = disclosures_fixture()
            if change == "source": del data["events"][0]["source_url"]
            elif change == "private": data["events"][0]["public"] = False
            elif change == "future": data["events"][0]["published_at"] = "2026-11-10T00:00:00Z"
            elif change == "range": data["events"][0]["amount_range"]["lower"] = "3000000"
            elif change == "mode": data["mode"] = "us_ptr"
            else: data["events"][0]["amount_range"]["lower"] = 1000000.0
            self.assertEqual(DISCLOSURES.review(data)["status"], "BLOCK", change)

    def test_older_capture_blocks_without_state_mutation(self):
        data = disclosures_fixture()
        state = DISCLOSURES.review(data)["next_state"]
        prior = copy.deepcopy(state)
        data["captured_at"] = "2026-11-09T08:59:59Z"
        self.assertEqual(DISCLOSURES.review(data, state)["status"], "BLOCK")
        self.assertEqual(state, prior)

    def test_future_announced_event_is_not_called_late(self):
        data = disclosures_fixture()
        data["events"][0]["event_date"] = "2026-12-01"
        row = next(row for row in DISCLOSURES.review(data)["events"] if row["amount_range"])
        self.assertEqual(row["publication_lag_calendar_days"], -23)

    def test_cli_state_is_only_written_explicitly_and_second_run_is_empty(self):
        script = ROOT / "skills/trader-pelosi/scripts/disclosures.py"
        with tempfile.TemporaryDirectory() as folder:
            state_path = Path(folder) / "state.json"
            command = [sys.executable, str(script), "new", str(DISCLOSURE_EXAMPLE), "--state", str(state_path)]
            preview = subprocess.run(command, capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(preview.stdout)["count"], 2)
            self.assertFalse(state_path.exists())
            subprocess.run(command + ["--write-state"], capture_output=True, text=True, check=True)
            again = subprocess.run(command, capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(again.stdout)["count"], 0)

    def test_cli_cannot_overwrite_source_index_with_seen_state(self):
        script = ROOT / "skills/trader-pelosi/scripts/disclosures.py"
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "index.json"
            original = DISCLOSURE_EXAMPLE.read_text(encoding="utf-8")
            source.write_text(original, encoding="utf-8")
            result = subprocess.run([sys.executable, str(script), "new", str(source),
                                     "--state", str(source), "--write-state"],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(source.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
