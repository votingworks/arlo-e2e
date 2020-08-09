from multiprocessing.pool import Pool
from os import path, mkdir
from typing import Final, Optional, TypeVar, List, cast

from electionguard.ballot import CiphertextAcceptedBallot
from electionguard.election import (
    ElectionConstants,
    ElectionDescription,
    CiphertextElectionContext,
)
from electionguard.logs import log_error, log_info
from electionguard.serializable import set_deserializers, Serializable, set_serializers
from tqdm import tqdm

from arlo_e2e.manifest import make_fresh_manifest, make_existing_manifest
from arlo_e2e.metadata import ElectionMetadata
from arlo_e2e.tally import FastTallyEverythingResults, SelectionTally
from arlo_e2e.utils import mkdir_helper, load_json_helper, all_files_in_directory

T = TypeVar("T")
U = TypeVar("U", bound=Serializable)

ELECTION_METADATA: Final[str] = "election_metadata.json"
ELECTION_DESCRIPTION: Final[str] = "election_description.json"
ENCRYPTED_TALLY: Final[str] = "encrypted_tally.json"
CRYPTO_CONSTANTS: Final[str] = "constants.json"
CRYPTO_CONTEXT: Final[str] = "cryptographic_context.json"


# Contents of this file loosely based on ElectionGuard's publish.py, and leveraging all the serialization
# built into ElectionGuard. We're doing a bit more error checking than they do, and we're trying to
# avoid directories with a million files if there are a million ballots.


def write_fast_tally(results: FastTallyEverythingResults, results_dir: str) -> None:
    """
    Writes out a directory with the full contents of the tally structure. Each ciphertext ballot
    will end up in its own file. Everything is JSON.
    """
    set_serializers()
    set_deserializers()

    results_dir = results_dir
    log_info("write_fast_tally: starting!")
    if not path.exists(results_dir):
        mkdir(results_dir)

    manifest = make_fresh_manifest(results_dir)

    log_info("write_fast_tally: writing election_description")
    manifest.write_json_file(ELECTION_DESCRIPTION, results.election_description)

    log_info("write_fast_tally: writing crypto context")
    manifest.write_json_file(CRYPTO_CONTEXT, results.context)

    log_info("write_fast_tally: writing crypto constants")
    manifest.write_json_file(CRYPTO_CONSTANTS, ElectionConstants())

    log_info("write_fast_tally: writing tally")
    manifest.write_json_file(ENCRYPTED_TALLY, results.tally)

    log_info("write_fast_tally: writing metadata")
    manifest.write_json_file(ELECTION_METADATA, results.metadata)

    log_info("write_fast_tally: writing ballots")

    for ballot in tqdm(results.encrypted_ballots, desc="Writing ballots"):
        ballot_name = ballot.object_id

        # This prefix stuff: ballot uids are encoded as 'b' plus a 7-digit number.
        # Ballot #3 should be 'b0000003'. By taking the first 4 digits, and making
        # that into a directory, we get a max of 10,000 files per directory, which
        # should be good enough. We never want to type 'ls' in a directory and have
        # it wedge because it's trying to digest a million filenmes.

        ballot_name_prefix = ballot_name[0:4]  # letter b plus first three digits
        manifest.write_json_file(
            ballot_name + ".json", ballot, ["ballots", ballot_name_prefix]
        )

    log_info("write_fast_tally: writing MANIFEST.json")
    manifest.write_manifest()


def load_fast_tally(
    results_dir: str,
    check_proofs: bool = True,
    pool: Optional[Pool] = None,
    verbose: bool = False,
) -> Optional[FastTallyEverythingResults]:
    """
    Given the directory name / path-name to a disk represntation of a fast-tally structure, this reads
    it back in, makes sure it's well-formed, and optionally checks the cryptographic proofs. If any
    checks fail, `None` is returned. Errors are logged. Optional `pool` allows for some parallelism
    in the verification process.
    """
    set_serializers()
    set_deserializers()

    if not path.exists(results_dir):
        log_error(f"Path ({results_dir}) not found, cannot load the fast-tally")
        return None

    manifest = make_existing_manifest(results_dir)
    if manifest is None:
        return None

    election_description: Optional[ElectionDescription] = manifest.read_json_file(
        ELECTION_DESCRIPTION, ElectionDescription
    )
    if election_description is None:
        return None

    constants: Optional[ElectionConstants] = manifest.read_json_file(
        CRYPTO_CONSTANTS, ElectionConstants
    )
    if constants is None:
        return None
    if constants != ElectionConstants():
        log_error(
            f"constants are out of date or otherwise don't match the current library: {constants}"
        )
        return None

    cec: Optional[CiphertextElectionContext] = manifest.read_json_file(
        CRYPTO_CONTEXT, CiphertextElectionContext
    )
    if cec is None:
        return None

    encrypted_tally: Optional[SelectionTally] = manifest.read_json_file(
        ENCRYPTED_TALLY, SelectionTally
    )
    if encrypted_tally is None:
        return None

    metadata: Optional[ElectionMetadata] = manifest.read_json_file(
        ELECTION_METADATA, ElectionMetadata
    )
    if metadata is None:
        return None

    ballots_dir = path.join(results_dir, "ballots")
    ballot_files = all_files_in_directory(ballots_dir)

    encrypted_ballots: Optional[List[Optional[CiphertextAcceptedBallot]]] = [
        load_json_helper(".", s, CiphertextAcceptedBallot) for s in ballot_files
    ]
    if encrypted_ballots is None or None in encrypted_ballots:
        # if even one of them fails, we're just going to give up and fail the whole thing
        return None

    # mypy isn't smart enough to notice the type change List[Optional[X]] --> List[X],
    # so we need to cast it, below.
    encrypted_ballots_cast = cast(List[CiphertextAcceptedBallot], encrypted_ballots)

    everything = FastTallyEverythingResults(
        metadata, election_description, encrypted_ballots_cast, encrypted_tally, cec,
    )

    if check_proofs:
        proofs_good = everything.all_proofs_valid(pool, verbose, True)
        if not proofs_good:
            # we don't need to log errors here; that will have happened internally
            return None

    return everything
