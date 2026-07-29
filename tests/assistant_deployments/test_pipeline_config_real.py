"""A real client pipeline config still satisfies the shared schema.

``PipelineConfig`` itself lives in unify — the actor has to be able to import the
type it is expected to construct — and its schema tests live there with it. What
stays here is the part that is about *this* repo: that the deployment data checked
in beside these tests still validates, and still means what the deployment code
assumes it means.

That makes this a drift detector in the direction that matters. A change to the
model in unify, or an edit to the client's JSON here, both surface as a failure at
the point where the two meet.
"""

from __future__ import annotations

from pathlib import Path

from unify.common.pipeline.config import PipelineConfig
from unify.data_manager.types.ingest import AutoDerivedColumn


class TestRealConfig:

    def test_client_alpha_config_validates(self):
        """Ensure the actual pipeline_config.json passes schema validation."""
        config_path = (
            Path(__file__).resolve().parents[2]
            / "unify_deploy"
            / "assistant_deployments"
            / "clients"
            / "client_alpha"
            / "deployments"
            / "v0"
            / "data"
            / "pipeline_config.json"
        )
        assert config_path.exists(), f"Config not found at {config_path}"

        cfg = PipelineConfig.from_file(str(config_path))

        assert len(cfg.source_files) == 2
        all_tables = [t for sf in cfg.source_files for t in sf.tables]
        assert len(all_tables) == 6

        repairs = cfg.source_files[0].tables[0]
        assert repairs.context == "ClientAlpha/Repairs2025"
        assert repairs.post_ingest is None

        telem_july = cfg.source_files[1].tables[0]
        assert telem_july.context == "ClientAlpha/Telematics2025/July"
        assert telem_july.post_ingest is not None
        assert len(telem_july.post_ingest.derived_columns) == 3

        assert len(cfg.post_ingest.derived_columns) == 1
        assert isinstance(cfg.post_ingest.derived_columns[0], AutoDerivedColumn)

        eff_repairs = cfg.effective_post_ingest(repairs)
        assert eff_repairs is not None
        assert len(eff_repairs.derived_columns) == 1

        eff_telem = cfg.effective_post_ingest(telem_july)
        assert eff_telem is not None
        assert len(eff_telem.derived_columns) == 4
