from __future__ import annotations

import json
import unittest
from dataclasses import replace

from world_sim.models import SelectionMode, VerificationMode, WorldConfig
from world_sim.selection import (
    LineageConfig,
    _selected_parent_indices,
    default_population,
    replay_generation,
    run_lineage_experiment,
    run_selection_matrix,
)


class LineageSelectionTests(unittest.TestCase):
    def test_lineage_run_is_byte_replayable(self) -> None:
        config = LineageConfig(
            generations=3,
            turns_per_generation=8,
            population_size=8,
            parent_count=2,
            mutation_rate=0.35,
        )

        first = run_lineage_experiment(seed=17, config=config)
        second = run_lineage_experiment(seed=17, config=config)

        self.assertEqual(json.dumps(first.to_dict(), sort_keys=True), json.dumps(second.to_dict(), sort_keys=True))
        self.assertEqual(first.content_sha256, second.content_sha256)

    def test_each_generation_replays_from_its_recorded_action_tape(self) -> None:
        config = LineageConfig(generations=3, turns_per_generation=8, population_size=8, parent_count=2)
        experiment = run_lineage_experiment(seed=71, config=config)

        for generation in experiment.generations:
            replay = replay_generation(generation, config=config)
            self.assertEqual(
                json.dumps(generation.result.to_dict(), sort_keys=True),
                json.dumps(replay.to_dict(), sort_keys=True),
            )

    def test_replay_rejects_a_tape_when_its_recorded_view_hash_is_tampered(self) -> None:
        config = LineageConfig(generations=1, turns_per_generation=4, population_size=8, parent_count=2)
        record = run_lineage_experiment(seed=71, config=config).generations[0]
        altered_decisions = list(record.decisions)
        altered_decisions[0] = replace(altered_decisions[0], view_sha256="0" * 64)
        altered_record = replace(record, decisions=tuple(altered_decisions))

        with self.assertRaisesRegex(RuntimeError, "action provider failed") as error:
            replay_generation(altered_record, config=config)
        self.assertIsInstance(error.exception.__cause__, RuntimeError)
        self.assertIn("expected world view", str(error.exception.__cause__))

    def test_no_selection_parent_choice_ignores_outcome_data(self) -> None:
        config = LineageConfig(
            selection_mode=SelectionMode.NONE,
            generations=1,
            turns_per_generation=4,
            population_size=8,
            parent_count=2,
        )
        population = default_population(config.population_size)
        outcomes = run_lineage_experiment(
            seed=23,
            config=config,
            initial_population=population,
        ).generations[0].outcomes

        selected = _selected_parent_indices(
            population=population,
            outcomes=outcomes,
            selection_seed=91,
            config=config,
        )
        altered_outcomes = tuple(
            replace(outcome, fitness=(index + 1) * 100, survived=False)
            for index, outcome in enumerate(outcomes)
        )
        selected_after_outcome_change = _selected_parent_indices(
            population=population,
            outcomes=altered_outcomes,
            selection_seed=91,
            config=config,
        )

        self.assertEqual(selected, selected_after_outcome_change)
        self.assertEqual(len(selected), config.parent_count)
        self.assertEqual(len(set(selected)), config.parent_count)

    def test_individual_selection_chooses_the_highest_objective_fitness(self) -> None:
        config = LineageConfig(
            selection_mode=SelectionMode.INDIVIDUAL,
            generations=1,
            turns_per_generation=4,
            population_size=8,
            parent_count=2,
        )
        population = default_population(config.population_size)
        outcomes = run_lineage_experiment(
            seed=23,
            config=config,
            initial_population=population,
        ).generations[0].outcomes
        ranked_outcomes = tuple(
            replace(outcome, fitness=(index + 1) * 10, survived=True)
            for index, outcome in enumerate(outcomes)
        )

        selected = _selected_parent_indices(
            population=population,
            outcomes=ranked_outcomes,
            selection_seed=91,
            config=config,
        )

        self.assertEqual(set(selected), {6, 7})

    def test_parent_bottleneck_and_clone_count_are_fixed(self) -> None:
        config = LineageConfig(generations=3, turns_per_generation=8, population_size=8, parent_count=2)
        experiment = run_lineage_experiment(seed=17, config=config)

        self.assertEqual(len(experiment.selections), config.generations - 1)
        for selection in experiment.selections:
            self.assertEqual(len(selection.parent_bundle_ids), config.parent_count)
            self.assertEqual(len(set(selection.parent_bundle_ids)), config.parent_count)
            self.assertEqual(
                set(selection.offspring_by_parent.values()),
                {config.population_size // config.parent_count},
            )
            edges = [
                edge
                for edge in experiment.lineage_edges
                if edge.child_generation == selection.child_generation
            ]
            self.assertEqual(len(edges), config.population_size)

    def test_mutation_schedule_matches_the_fitness_blind_control(self) -> None:
        shared = dict(generations=3, turns_per_generation=8, population_size=8, parent_count=2, mutation_rate=1.0)
        selected = run_lineage_experiment(
            seed=101,
            config=LineageConfig(selection_mode=SelectionMode.INDIVIDUAL, **shared),
        )
        control = run_lineage_experiment(
            seed=101,
            config=LineageConfig(selection_mode=SelectionMode.NONE, **shared),
        )

        self.assertEqual(
            [edge.mutation_seed for edge in selected.lineage_edges],
            [edge.mutation_seed for edge in control.lineage_edges],
        )
        self.assertTrue(all(edge.mutation is not None for edge in selected.lineage_edges))
        self.assertTrue(all(edge.mutation is not None for edge in control.lineage_edges))

    def test_matrix_runs_the_four_core_conditions(self) -> None:
        matrix = run_selection_matrix(
            seed=17,
            generations=2,
            turns_per_generation=6,
            population_size=8,
            parent_count=2,
        )

        self.assertEqual(
            set(matrix.conditions),
            {"individual_proxy", "individual_receipts", "none_proxy", "none_receipts"},
        )
        self.assertEqual(
            matrix.conditions["individual_proxy"].generations[0].population,
            matrix.conditions["none_receipts"].generations[0].population,
        )
        self.assertEqual(
            matrix.conditions["individual_proxy"].config.world_config.verification_mode,
            VerificationMode.PROXY,
        )
        self.assertEqual(
            matrix.conditions["none_receipts"].config.selection_mode,
            SelectionMode.NONE,
        )


if __name__ == "__main__":
    unittest.main()
