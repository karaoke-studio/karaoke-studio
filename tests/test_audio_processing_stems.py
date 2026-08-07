"""模型输出轨（stem）解析：必须给出模型自己声明的名字，且解析失败时不猜。"""

from __future__ import annotations

from krok_helper.audio_processing.separation.stems import (
    choose_stem_for_task,
    parse_model_stems,
)

# 真实样本：pymss catalog 的 inst_v1e.yaml（instruments 在 training 块首）
INST_V1E = """audio:
  chunk_size: 485100
  sample_rate: 44100
model:
  dim: 384
training:
  instruments:
  - other
  - vocals
  target_instrument: other
  use_amp: True

inference:
  batch_size: 1
"""

# 真实样本：karaoke 模型（instruments 夹在 training 块中间，前后都有别的键）
KARAOKE = """audio:
  chunk_size: 352800
training:
  augmentation: false
  batch_size: 4
  instruments:
  - karaoke
  - other
  lr: 1.0e-05
  target_instrument: karaoke
  use_mp3_compress: false
"""


class TestRealConfigs:
    def test_inst_v1e_stems(self) -> None:
        assert parse_model_stems(INST_V1E) == ("other", "vocals")

    def test_karaoke_stems(self) -> None:
        """这个模型不叫 vocals/instrumental —— 正是必须读真实配置的原因。"""
        assert parse_model_stems(KARAOKE) == ("karaoke", "other")

    def test_inline_list_form(self) -> None:
        assert parse_model_stems("training:\n  instruments: [karaoke, other]\n") == (
            "karaoke",
            "other",
        )


class TestFailsClosed:
    """解析不确定时必须返回空，让上层提示换模型，而不是给出可能错误的名字。"""

    def test_no_training_block(self) -> None:
        assert parse_model_stems("audio:\n  chunk_size: 1\n") == ()

    def test_no_instruments_key(self) -> None:
        assert parse_model_stems("training:\n  target_instrument: other\n") == ()

    def test_empty_list(self) -> None:
        assert parse_model_stems("training:\n  instruments:\n  lr: 1\n") == ()

    def test_rejects_non_identifier_items(self) -> None:
        assert parse_model_stems("training:\n  instruments:\n  - {a: 1}\n") == ()

    def test_ignores_instruments_outside_training(self) -> None:
        text = "model:\n  instruments:\n  - bogus\ntraining:\n  lr: 1\n"
        assert parse_model_stems(text) == ()

    def test_absurd_length_is_rejected(self) -> None:
        items = "\n".join(f"  - stem{i}" for i in range(40))
        assert parse_model_stems(f"training:\n  instruments:\n{items}\n") == ()

    def test_empty_input(self) -> None:
        assert parse_model_stems("") == ()


class TestNormalisation:
    def test_strips_comments_and_quotes(self) -> None:
        text = "training:\n  instruments:\n  - 'vocals'  # 主唱\n  - other\n"
        assert parse_model_stems(text) == ("vocals", "other")

    def test_deduplicates_preserving_order(self) -> None:
        text = "training:\n  instruments:\n  - other\n  - vocals\n  - other\n"
        assert parse_model_stems(text) == ("other", "vocals")


class TestStemForTask:
    """外部模型上「某任务用哪条轨」必须从模型实际声明的轨里挑，不能写死。"""

    def test_inst_v1e_naming(self) -> None:
        """回归：曾写死 'instrumental'，被服务拒绝
        (Invalid stem 'instrumental'. Valid stems: ['other', 'vocals'])。"""
        stems = ("other", "vocals")
        assert choose_stem_for_task("vocal", stems) == "vocals"
        assert choose_stem_for_task("instrumental", stems) == "other"

    def test_karaoke_naming(self) -> None:
        stems = ("karaoke", "other")
        assert choose_stem_for_task("harmony", stems) == "other"

    def test_capitalised_naming_is_preserved(self) -> None:
        """becruily / anvuew 用的是首字母大写，返回值必须保持原样。"""
        stems = ("Vocals", "Instrumental")
        assert choose_stem_for_task("vocal", stems) == "Vocals"
        assert choose_stem_for_task("instrumental", stems) == "Instrumental"

    def test_two_stem_fallback_picks_the_non_vocal_track(self) -> None:
        stems = ("weird_name", "vocals")
        assert choose_stem_for_task("instrumental", stems) == "weird_name"
        assert choose_stem_for_task("vocal", stems) == "vocals"

    def test_returns_empty_when_undecidable(self) -> None:
        """三条都不认识时不猜，返回空让上层提示用户改选。"""
        assert choose_stem_for_task("instrumental", ("a", "b", "c")) == ""
        assert choose_stem_for_task("vocal", ("a", "b")) == ""
        assert choose_stem_for_task("vocal", ()) == ""
