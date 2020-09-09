# This is a version of encryption_bench, but only for running Ray remotely.
import argparse
from sys import exit
from timeit import default_timer as timer
from typing import Optional

import ray
from electionguard.elgamal import elgamal_keypair_from_secret
from electionguard.group import int_to_q_unchecked
from electionguard.utils import get_optional

from arlo_e2e.dominion import read_dominion_csv
from arlo_e2e.ray_helpers import ray_init_cluster
from arlo_e2e.ray_tally import ray_tally_everything


def run_bench(filename: str, output_dir: Optional[str]) -> None:
    start_time = timer()
    print(f"Benchmarking: {filename}")
    cvrs = read_dominion_csv(filename)
    if cvrs is None:
        print(f"Failed to read {filename}, terminating.")
        exit(1)
    rows, cols = cvrs.data.shape

    parse_time = timer()
    print(
        f"    Parse time: {parse_time - start_time: .3f} sec, {rows / (parse_time - start_time):.3f} ballots/sec"
    )

    assert rows > 0, "can't have zero ballots!"

    # doesn't matter what the key is, so long as it's consistent for both runs
    keypair = get_optional(elgamal_keypair_from_secret(int_to_q_unchecked(31337)))

    print(f"\nstarting ray.io parallelism!")
    rtally_start = timer()
    rtally = ray_tally_everything(
        cvrs, secret_key=keypair.secret_key, verbose=True, root_dir=output_dir
    )
    rtally_end = timer()

    print(f"\nOVERALL PERFORMANCE")
    print(f"    Ray time:    {rtally_end - rtally_start : .3f} sec")
    print(f"    Ray rate:    {rows / (rtally_end - rtally_start): .3f} ballots/sec")

    if output_dir:
        print(f"\nSANITY CHECK")
        assert rtally.all_proofs_valid(
            verbose=True, recheck_ballots_and_tallies=False
        ), "proof failure!"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Runs a tallying benchmark, using Ray (either locally or on a remote cluster)"
    )
    # parser.add_argument(
    #     "--cluster",
    #     action="store_true",
    #     help="uses a Ray cluster for distributed computation (local by default)",
    # )
    parser.add_argument(
        "--dir",
        type=str,
        nargs=1,
        default=None,
        help="directory to store the encrypted ballots on disk, enables equivalence checking (default: memory only)",
    )
    parser.add_argument(
        "cvr_file",
        type=str,
        nargs="+",
        help="filename(s) for the Dominion-style ballot CVR file",
    )

    args = parser.parse_args()
    files = args.cvr_file
    file_dir = args.dir[0] if args.dir else None

    ray_init_cluster()

    for arg in files:
        run_bench(arg, file_dir)

    print("Writing Ray timelines to disk.")
    ray.timeline("ray-timeline.json")
    ray.object_transfer_timeline("ray-object-transfer-timeline.json")

    ray.shutdown()
    exit(0)
