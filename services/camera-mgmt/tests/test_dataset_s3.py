"""S3 mirror: bucket validation and prefix normalisation.

Both are pure and both exist to turn operator typos into sentences rather than
raw botocore errors, so they are worth pinning down precisely.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.services import s3_upload as s3  # noqa: E402


@pytest.mark.parametrize("name", [
    "my-bucket",
    "training-data-2026",
    "a.b.c",
    "abc",                      # exactly the 3-character minimum
    "a" * 63,                   # exactly the 63-character maximum
])
def test_valid_bucket_names_pass(name):
    assert s3.validate_bucket(name) == name


@pytest.mark.parametrize("name,because", [
    ("", "empty"),
    ("ab", "shorter than 3"),
    ("a" * 64, "longer than 63"),
    ("My-Bucket", "uppercase is not a DNS label"),
    ("my_bucket", "underscores are not allowed"),
    ("-leading", "must start alphanumeric"),
    ("trailing-", "must end alphanumeric"),
    ("has..dots", "consecutive dots are rejected by S3"),
    ("has spaces", "spaces are not allowed"),
])
def test_invalid_bucket_names_raise_something_readable(name, because):
    with pytest.raises(s3.S3UploadError) as exc:
        s3.validate_bucket(name)
    assert str(exc.value), f"no message for {because}"
    # The message must be guidance, not a regex dump.
    assert "S3UploadError" not in str(exc.value)


def test_bucket_name_is_stripped():
    assert s3.validate_bucket("  my-bucket  ") == "my-bucket"


@pytest.mark.parametrize("given,expected", [
    (None, ""),
    ("", ""),
    ("   ", ""),
    ("datasets", "datasets/"),
    ("datasets/", "datasets/"),
    ("/datasets", "datasets/"),
    ("/datasets/", "datasets/"),
    ("//datasets//", "datasets/"),
    ("a/b/c", "a/b/c/"),
])
def test_prefix_normalisation(given, expected):
    assert s3.normalise_prefix(given) == expected


def test_empty_prefix_means_bucket_root_not_a_slash():
    """A bare "/" prefix would create a directory literally named "" in the
    bucket; the root is the empty string."""
    assert s3.normalise_prefix("/") == ""
