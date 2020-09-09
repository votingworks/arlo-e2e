import shutil
from base64 import b64encode
from dataclasses import dataclass
from hashlib import sha256
from pathlib import PurePath
from typing import Dict, Optional, Type, List, Union, Tuple

from electionguard.ballot import CiphertextAcceptedBallot
from electionguard.logs import log_error, log_warning
from electionguard.serializable import WRITE, Serializable
from electionguard.utils import flatmap_optional

from arlo_e2e.utils import (
    load_json_helper,
    load_file_helper,
    compose_filename,
    T,
    mkdir_list_helper,
    decode_json_file_contents,
)


@dataclass(eq=True, unsafe_hash=True)
class FileInfo(Serializable):
    """
    Internal helper class: When we write a file to disk, we need to know its SHA256 hash
    and length (in bytes). This is returned from methods that write some things to disk.
    """

    hash: str
    """
    SHA256 hash of the file, represented as a base64 string
    """

    num_bytes: int
    """
    Length of the file in bytes
    """


@dataclass(eq=True, unsafe_hash=True)
class ManifestExternal(Serializable):
    """
    This class is the on-disk representation of the Manifest class. The only difference is that
    it doesn't have the `root_dir` field, which wouldn't make sense to write to disk.
    """

    hashes: Dict[str, FileInfo]
    bytes_written: int = 0

    def to_manifest(self, root_dir: str) -> "Manifest":
        """
        Converts this to a Manifest class, suitable for working with in-memory.
        """
        return Manifest(root_dir, self.hashes, self.bytes_written)


@dataclass(eq=True, unsafe_hash=True)
class Manifest:
    """
    This class is a front-end for writing files to disk that can also generate two useful things:
    a series of `index.html` pages for every subdirectory, as well as a top-level file, `MANIFEST.json`,
    which includes a JSON object mapping from filenames to their SHA256 hashes.

    Do not construct this directly. Instead, use `make_fresh_manifest` or `make_existing_manifest`.
    """

    root_dir: str
    hashes: Dict[str, FileInfo]
    bytes_written: int = 0

    # TODO: add a call to this in the tally verification process.
    def all_hashes_unique(self) -> bool:
        """
        Checks that every hash value is unique. If a file hash repeated, then there's
        a chance that something went really wrong, like an identical ballot being repeated.
        """
        expected_num_hashes = len(self.hashes.keys())
        actual_num_hashes = len({v.hash for v in self.hashes.values()})

        if expected_num_hashes != actual_num_hashes:
            log_error(
                f"Expected to find {expected_num_hashes} unique ballot hashes, only found {actual_num_hashes}."
            )
            return False
        else:
            return True

    def to_manifest_external(self) -> ManifestExternal:
        """
        Converts this to a ManifestExternal class, suitable for serializing to disk.
        """
        return ManifestExternal(self.hashes, self.bytes_written)

    def merge_from(self, other: "Manifest") -> None:
        """
        Given a second manifest, reads all its contents and merges them into this manifest
        (i.e., "self" mutates, but "other" doesn't change). This would be useful when multiple
        remote workers are writing files, and you want to merge the results into a single
        manifest object. Note: both manifests must share the same root directory, and any
        overlapping files in the manifests are considered an error unless they're identical.
        """
        assert (
            other.root_dir == self.root_dir
        ), "manifests must share the same root directory"
        self_keys = set(self.hashes.keys())
        other_keys = set(other.hashes.keys())
        shared_keys = self_keys.intersection(other_keys)
        for k in shared_keys:
            if self.hashes[k] != other.hashes[k]:
                msg = f"cannot merge manifests: disagreeing contents for {k}: {self.hashes[k]} vs. {other.hashes[k]}"
                log_error(msg)
                raise RuntimeError(msg)
        for k in other_keys:
            self.hashes[k] = other.hashes[k]
        self.bytes_written += other.bytes_written

    def write_json_file(
        self,
        file_name: str,
        content_obj: Serializable,
        subdirectories: List[str] = None,
    ) -> str:
        """
        Given a filename, subdirectory, and contents of the file, writes the contents out to the file. As a
        side-effect, the full filename and its contents' hash are remembered in `self.hashes`, to be written
        out later with a call to `write_manifest`.

        :param subdirectories: paths to be introduced between `root_dir` and the file; empty-list means no subdirectory
        :param file_name: name of the file, including any suffix
        :param content_obj: any ElectionGuard "Serializable" object
        :returns: the SHA256 hash of `file_contents`
        """

        json_txt = content_obj.to_json(strip_privates=True)
        return self.write_file(file_name, json_txt, subdirectories)

    def write_file(
        self, file_name: str, file_contents: str, subdirectories: List[str] = None
    ) -> str:
        """
        Given a filename, subdirectory, and contents of the file, writes the contents out to the file. As a
        side-effect, the full filename and its contents' hash are remembered in `self.hashes`, to be written
        out later with a call to `write_manifest`.

        :param subdirectories: paths to be introduced between `root_dir` and the file; empty-list means no subdirectory
        :param file_name: name of the file, including any suffix
        :param file_contents: string to be written to the file
        :returns: the SHA256 hash of `file_contents`
        """

        if subdirectories is None:
            subdirectories = []

        manifest_name = compose_manifest_name(file_name, subdirectories)

        mkdir_list_helper(self.root_dir, subdirectories)
        h = sha256_hash(file_contents)
        file_content_bytes = len(file_contents.encode("utf-8"))
        full_name = compose_filename(self.root_dir, file_name, subdirectories)
        with open(full_name, WRITE) as f:
            f.write(file_contents)
        file_info = FileInfo(h, file_content_bytes)

        if manifest_name in self.hashes:
            log_warning(
                f"Writing a file through a manifest that has already been written: {manifest_name}"
            )

        self.bytes_written += file_info.num_bytes
        self.hashes[manifest_name] = file_info
        return file_info.hash

    def write_manifest(self) -> str:
        """
        Writes out `MANIFEST.json` into the existing `root_dir`, providing a mapping from filenames
        to their SHA256 hashes.
        :returns: the SHA256 hash of `MANIFEST.json`, itself
        """

        # As a side-effect, this will also add a hash for the manifest itself into the `hashes` dictionary,
        # but that's something of an oddball case that won't ever matter in practice.
        return self.write_json_file("MANIFEST.json", self.to_manifest_external(), [])

    def _get_hash_required(self, filename: str) -> Optional[FileInfo]:
        """
        Gets the hash for the requested filename (fully composed path, such as we might get from
        `utils.compose_filename`). If absent, logs an error and returns None.
        """
        hash = self.hashes[filename]
        if hash is None:
            log_error(f"No hash available for file: {filename}")
            return None
        return hash

    def read_json_file(
        self,
        file_name: Union[PurePath, str],
        class_handle: Type[Serializable[T]],
        subdirectories: List[str] = None,
    ) -> Optional[T]:
        """
        Reads the requested file, by name, returning its contents as a Python object for the given class handle.
        If no hash for the file is present, if the file doesn't match its known hash, or if the JSON deserialization
        process fails, then `None` will be returned and an error will be logged. If the `file_name`
        is actually a path-like object, the subdirectories are ignored.

        :param subdirectories: Path elements to be introduced between `root_dir` and the file; empty-list means
          no subdirectory. Ignored if the file_name is a path-like object.
        :param file_name: Name of the file, including any suffix, or a path-like object.
        :param class_handle: The class, itself, that we're trying to deserialize to (if None, then you get back
          whatever the JSON becomes, e.g., a dict).
        :returns: The contents of the file, or `None` if there was an error.
        """

        # this loads the file and verifies the hashes
        file_contents = self.read_file(file_name, subdirectories)
        return flatmap_optional(
            file_contents, lambda f: decode_json_file_contents(f, class_handle)
        )

    def validate_contents(self, manifest_file_name: str, file_contents: str) -> bool:
        """
        Checks the manifest for the given file name. Returns True if the name is
        included in the manifest *and* the file_contents match the manifest. If anything
        is not properly validated, a suitable error will be written to the ElectionGuard log.
        """
        if manifest_file_name not in self.hashes:
            log_error(f"File {manifest_file_name} was not in the manifest")
            return False

        file_info: FileInfo = self.hashes[manifest_file_name]
        file_len = len(file_contents.encode("utf-8"))

        if file_len != file_info.num_bytes:
            log_error(
                f"File {manifest_file_name} did not have the expected length (expected: {file_info.num_bytes} bytes, actual: {file_len} bytes)"
            )
            return False

        data_hash = sha256_hash(file_contents)
        if data_hash != file_info.hash:
            log_error(
                f"File {manifest_file_name} did not have the expected hash (expected: {file_info.hash}, actual: {data_hash})"
            )
            return False

        return True

    def read_file(
        self, file_name: Union[PurePath, str], subdirectories: List[str] = None
    ) -> Optional[str]:
        """
        Reads the requested file, by name, returning its contents as a Python string.
        If no hash for the file is present, or if the file doesn't match its known
        hash, then `None` will be returned and an error will be logged. If the file_name
        is actually a path-like object, the subdirectories are ignored.

        :param subdirectories: Path elements to be introduced between `root_dir` and the file; empty-list means
          no subdirectory. Ignored if the file_name is a path-like object.
        :param file_name: Name of the file, including any suffix, or a path-like object.
        :returns: The contents of the file, or `None` if there was an error.
        """
        if isinstance(file_name, PurePath):
            full_name = file_name
        else:
            full_name = compose_filename(self.root_dir, file_name, subdirectories)
        manifest_name = path_to_manifest_name(self.root_dir, full_name)

        file_contents = load_file_helper(self.root_dir, full_name, subdirectories)
        if file_contents is not None and self.validate_contents(
            manifest_name, file_contents
        ):
            return file_contents
        else:
            return None

    def write_ciphertext_ballot(self, ballot: CiphertextAcceptedBallot) -> None:
        """
        Given a manifest and a ciphertext ballot, writes the ballot to disk and updates
        the manifest.
        """
        ballot_name = ballot.object_id
        ballot_name_prefix = ballot_name[0:4]
        self.write_json_file(
            ballot_name + ".json", ballot, ["ballots", ballot_name_prefix]
        )

    def load_ciphertext_ballot(
        self, ballot_id: str
    ) -> Optional[CiphertextAcceptedBallot]:
        """
        Given a manifest and a ballot identifier string, attempts to load the ballot
        from disk. Returns `None` if the ballot doesn't exist or if the hashes fail
        to verify.
        """
        ballot_name_prefix = ballot_id[0:4]
        return self.read_json_file(
            ballot_id + ".json",
            CiphertextAcceptedBallot,
            ["ballots", ballot_name_prefix],
        )


def make_fresh_manifest(root_dir: str, delete_existing: bool = False) -> Manifest:
    """
    Constructs a fresh `Manifest` instance.
    :param root_dir: a name for the directory about to be filled up with fresh files
    :param delete_existing: if true, will delete any existing files in the given root directory (false by default)
    """
    if delete_existing:
        try:
            shutil.rmtree(root_dir)
        except FileNotFoundError:
            pass

    return Manifest(root_dir=root_dir, hashes={})


def make_existing_manifest(root_dir: str) -> Optional[Manifest]:
    """
    Constructs a `Manifest` instance from a directory that contains a `MANIFEST.json` file.
    If the file is missing or something else goes wrong, you could get `None` as a result.
    :param root_dir: a name for the directory containing `MANIFEST.json` and other files.
    """
    manifest_ex: Optional[ManifestExternal] = load_json_helper(
        root_dir=root_dir, file_name="MANIFEST.json", class_handle=ManifestExternal
    )
    return flatmap_optional(manifest_ex, lambda m: m.to_manifest(root_dir))


def sha256_hash(input: str) -> str:
    """
    Given a string, returns an base64-encoded representation of the 256-bit SHA2-256
    hash of that input string (in utf8).
    """
    h = sha256()
    h.update(input.encode("utf-8"))
    return b64encode(h.digest()).decode("utf-8")


def compose_manifest_name(file_name: str, subdirectories: List[str] = None) -> str:
    """
    Helper function: given a file name, and an optional list of subdirectories that
    go in front (empty-list implies no subdirectory), returns a string corresponding
    to the full filename, properly joined, suitable for use in MANIFEST.json.

    This method is distinct from `compose_filename` because it must give the same
    answer on any platform. This is why it uses vertical bars rather than forward
    or backward slashes.
    """
    if subdirectories is None:
        dirs = [file_name]
    else:
        dirs = subdirectories + [file_name]
    return "|".join(dirs)


def manifest_name_to_filename(manifest_name: str) -> PurePath:
    """
    Helper function: given the name of a file, as it would appear in a MANIFEST.json
    file, get the expected local filesystem name.
    """
    subdirs = manifest_name.split("|")
    return compose_filename(manifest_name, subdirs[-1], subdirs[0:-1])


def path_to_manifest_name(root_dir: str, path: PurePath) -> str:
    """
    Helper function: given the name of a file (or a Path to that file), return the name as
    it would appear in MANIFEST.json.
    """
    elems = list(path.parts)  # need to convert from tuple to list
    assert elems[0] == root_dir, f"unexpected missing root directory in path: {path}"
    return compose_manifest_name(elems[-1], elems[1:-1])
