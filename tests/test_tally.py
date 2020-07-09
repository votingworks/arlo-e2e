import unittest
from io import StringIO
from datetime import timedelta
from multiprocessing import Pool, cpu_count

from hypothesis import settings, given, HealthCheck, Phase

from dominion import read_dominion_csv
from eg_tally import fast_tally_everything
from tests.dominion_hypothesis import dominion_cvrs


class TestFastTallies(unittest.TestCase):
    @given(dominion_cvrs())
    @settings(
        deadline=timedelta(milliseconds=50000),
        suppress_health_check=[HealthCheck.too_slow],
        max_examples=5,
        # disabling the "shrink" phase, because it runs very slowly
        phases=[Phase.explicit, Phase.reuse, Phase.generate, Phase.target],
    )
    def test_end_to_end(self, input: str):
        cvrs = read_dominion_csv(StringIO(input))
        self.assertIsNotNone(cvrs)

        _, ballots, _ = cvrs.to_election_description()
        assert len(ballots) > 0, "can't have zero ballots!"

        pool = Pool(cpu_count())
        tally = fast_tally_everything(cvrs, pool, verbose=True)
        self.assertTrue(tally.all_proofs_valid(verbose=True))
        pool.close()