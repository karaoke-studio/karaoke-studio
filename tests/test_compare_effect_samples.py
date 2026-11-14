"""Invalid numeric output must fail the cross-platform effect gate."""
import struct

import pytest

from scripts import compare_effect_samples as samples


def sample(*, geometry=0.0, particle_x=10.0, particle_count=1):
    """A complete sample stream with one populated particle case."""
    data = bytearray(samples.GEO_EFFECTS * samples.GEO_INDICES * samples.GEO_TIMES
                     * samples.GEO_FIELDS * 4)
    struct.pack_into("<f", data, 0, geometry)
    for case in range(len(samples.PARTICLE_KINDS) * samples.PARTICLE_CASES):
        count = particle_count if case == 0 else 0
        data.extend(struct.pack("<I", count))
        for _ in range(count):
            data.extend(struct.pack("<7f", particle_x, 20.0, 0.0, 4.0, 0.5, 1.0, 1.0))
    for _ in range(samples.NOISE_MASKS):
        data.extend(struct.pack("<I4B", 1, 0, 0, 0, 0))
    data.extend(bytes(samples.COLUMN_IMAGES * samples.COLUMN_BYTES))
    for _ in range(samples.DECOR_MODES):
        data.extend(struct.pack("<3i4B", 1, 1, 0, 0, 0, 0, 0))
    return bytes(data)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")],
                         ids=["nan", "positive-infinity", "negative-infinity"])
@pytest.mark.parametrize("field", ["geometry", "particle_x"])
@pytest.mark.parametrize("side", ["reference", "candidate", "both"])
def test_non_finite_values_fail_even_when_both_sides_match(value, field, side):
    valid, invalid = sample(), sample(**{field: value})
    reference = invalid if side in {"reference", "both"} else valid
    candidate = invalid if side in {"candidate", "both"} else valid
    label = "candidate" if side == "candidate" else "reference"
    with pytest.raises(ValueError, match=rf"{label}: non-finite float .* at byte offset \d+"):
        samples.compare(reference, candidate)


@pytest.mark.parametrize("side", ["reference", "candidate"])
def test_non_finite_unpaired_particles_are_rejected(side):
    empty = sample(particle_count=0)
    invalid = sample(particle_x=float("nan"))
    reference, candidate = (invalid, empty) if side == "reference" else (empty, invalid)
    with pytest.raises(ValueError, match=f"{side}: non-finite float"):
        samples.compare(reference, candidate)


def test_finite_samples_preserve_numeric_tolerances_and_discrete_checks():
    reference = sample()
    assert not samples.compare(reference, reference).failures()
    assert not samples.compare(reference, sample(particle_x=10.00001)).failures()
    assert samples.compare(reference, sample(particle_x=10.1)).failures()
    assert samples.compare(reference, sample(particle_count=0)).count_mismatches


def test_cli_fails_invalid_candidate_and_still_checks_remaining_files(tmp_path, capsys):
    reference, invalid, valid = (tmp_path / name for name in ("reference.bin", "invalid.bin", "valid.bin"))
    reference.write_bytes(sample())
    invalid.write_bytes(sample(geometry=float("nan")))
    valid.write_bytes(sample())
    assert samples.main([str(reference), str(invalid), str(valid)]) == 1
    output = capsys.readouterr().out
    assert "invalid.bin vs reference.bin: FAIL: candidate: non-finite float" in output
    assert "valid.bin vs reference.bin: 1 particles" in output
    assert samples.main([str(reference), str(valid)]) == 0
