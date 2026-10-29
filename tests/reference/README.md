# Frozen reference implementations

`features_m10.py` is `fart/engine/features.py` as merged in PR #10 (commit `dd91c28`, the milestone-10 rule file of the
frozen measurement week), byte for byte. `tests/test_m11_engine_performance.py` pins its sha256 and runs it next to the
milestone-11 implementation on a fixture database to prove that the metas, the coin features and the verdicts are
byte-equal. Never edit it: the proof is only worth something while the reference is the old code.
