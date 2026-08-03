from tests import run_version_matrix as matrix


def test_distribution_uses_ansible_base_only_for_210():
    assert matrix.distribution("2.10") == "ansible-base==2.10.17"
    assert matrix.distribution("2.21") == "ansible-core==2.21.*"


def test_auto_profiles_cover_legacy_reference_and_adjacent_releases():
    assert matrix.effective_profile("2.12", "auto") == "smoke"
    assert matrix.effective_profile("2.19", "auto") == "portable"
    assert matrix.effective_profile("2.20", "auto") == "full"
    assert matrix.effective_profile("2.21", "auto") == "portable"


def test_explicit_profile_is_preserved():
    assert matrix.effective_profile("2.20", "smoke") == "smoke"


def test_every_supported_series_has_a_python_runtime():
    assert set(matrix.DEFAULT_VERSIONS) <= set(matrix.PYTHON_BY_SERIES)
    assert matrix.PYTHON_BY_SERIES["2.10"] == "3.9"
