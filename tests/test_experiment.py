from __future__ import annotations

import json
import unittest

from world_sim.experiment import run_counterfactual_pair


class CounterfactualExperimentTests(unittest.TestCase):
    def test_pair_changes_only_the_verification_treatment(self) -> None:
        pair = run_counterfactual_pair(seed=17, turns=8)
        payload = pair.to_dict()

        self.assertEqual(payload["shared_world"]["seed"], 17)
        self.assertEqual(payload["shared_world"]["config_except_verification"]["verification_mode"], "treatment-controlled")
        self.assertEqual(payload["proxy"]["result"]["initial_state"]["commons"], payload["receipts"]["result"]["initial_state"]["commons"])
        self.assertEqual(
            [agent["id"] for agent in payload["proxy"]["result"]["initial_state"]["agents"]],
            [agent["id"] for agent in payload["receipts"]["result"]["initial_state"]["agents"]],
        )

    def test_proxy_pays_shortcuts_but_receipts_does_not(self) -> None:
        pair = run_counterfactual_pair(seed=17, turns=8)

        self.assertGreater(pair.proxy.metrics["false_claims_paid"], 0)
        self.assertEqual(pair.receipts.metrics["false_claims_paid"], 0)
        self.assertGreater(pair.proxy.metrics["total_final_energy"], pair.receipts.metrics["total_final_energy"])

    def test_pair_is_replayable(self) -> None:
        first = run_counterfactual_pair(seed=71, turns=7).to_dict()
        second = run_counterfactual_pair(seed=71, turns=7).to_dict()

        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
