"""Typed scenario specs for enterprise pilot simulations."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

IntegrationMode = Literal["mock", "real"]
AlertChannel = Literal["simulated_outbox", "email", "sms", "whatsapp", "teams"]
ComparisonOperator = Literal[">", ">=", "<", "<=", "==", "!="]
ScheduleType = Literal["manual", "interval", "cron", "external_trigger"]
TaskExecutionMode = Literal["live", "offline"]


class IntegrationBinding(BaseModel):
    """Scenario binding to a real or mock integration package."""

    package_slug: str = Field(description="Integration package slug to invoke.")
    mode: IntegrationMode = Field(
        description="Whether the binding uses mock or real IO.",
    )
    required_capabilities: list[str] = Field(default_factory=list)
    schema_version: str = Field(description="Normalized connector output schema.")

    @model_validator(mode="after")
    def _mock_slug_matches_mode(self) -> "IntegrationBinding":
        if self.mode == "mock" and not self.package_slug.endswith("_mock"):
            raise ValueError("mock bindings must use a package slug ending in '_mock'")
        if self.mode == "real" and self.package_slug.endswith("_mock"):
            raise ValueError("real bindings must not use a mock package slug")
        return self


class DataTarget(BaseModel):
    """Materialization target for one normalized connector output table."""

    table: str = Field(description="Key in the connector snapshot tables map.")
    context: str = Field(description="DataManager context for this table.")
    description: str = ""
    unique_key: str | list[str] | None = Field(
        default=None,
        description=(
            "Column(s) used for upsert.  A scalar string keys on a single "
            "column; a list keys on the tuple (composite primary key).  "
            "Composite keys are required for junction tables and "
            "discriminated-union tables in CRM/HRIS-style schemas where "
            "no single natural column is unique."
        ),
    )
    fields: dict[str, Any] = Field(default_factory=dict)

    @field_validator("unique_key")
    @classmethod
    def _unique_key_non_empty(
        cls,
        value: str | list[str] | None,
    ) -> str | list[str] | None:
        if isinstance(value, list):
            if not value:
                raise ValueError("unique_key list must not be empty")
            for k in value:
                if not isinstance(k, str) or not k.strip():
                    raise ValueError(
                        "unique_key list entries must be non-empty strings",
                    )
        elif isinstance(value, str) and not value.strip():
            raise ValueError("unique_key must be a non-empty string when scalar")
        return value


class TimelineEvent(BaseModel):
    """A deterministic scenario tick or live polling action."""

    tick: int = Field(ge=0)
    capability: str
    function: str
    args: dict[str, Any] = Field(default_factory=dict)
    label: str = ""
    disruption: str | None = None


class MonitorRule(BaseModel):
    """Threshold rule evaluated against materialized normalized rows."""

    id: str
    table: str
    field: str
    operator: ComparisonOperator
    threshold: float | int | str | bool
    severity: Literal["info", "warning", "critical"] = "warning"
    route: str
    message_template: str

    @field_validator("message_template")
    @classmethod
    def _message_template_not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message_template must not be empty")
        return value


class AlertRoute(BaseModel):
    """Where monitor alerts should be delivered."""

    id: str
    channel: AlertChannel
    contact_ref: str | None = None
    outbox_context: str | None = None

    @model_validator(mode="after")
    def _route_has_required_target(self) -> "AlertRoute":
        if self.channel == "simulated_outbox" and not self.outbox_context:
            raise ValueError("simulated_outbox routes require outbox_context")
        if self.channel != "simulated_outbox" and not self.contact_ref:
            raise ValueError("real delivery routes require contact_ref")
        return self


class DashboardTileRecipe(BaseModel):
    """Declarative recipe for a live DashboardManager tile."""

    id: str
    title: str
    description: str = ""
    data_binding: dict[str, Any]
    html_template: str = ""
    on_data: str = ""


class DashboardRecipe(BaseModel):
    """Declarative recipe for a dashboard composed from tile recipes."""

    id: str
    title: str
    description: str = ""
    tiles: list[DashboardTileRecipe] = Field(default_factory=list)


class ScheduleSpec(BaseModel):
    """Durable schedule trigger for recurring or externally-triggered work."""

    type: ScheduleType = "manual"
    interval_seconds: int | None = Field(default=None, gt=0)
    cron: str | None = None
    trigger_name: str | None = None

    @model_validator(mode="after")
    def _schedule_has_required_fields(self) -> "ScheduleSpec":
        if self.type == "interval" and self.interval_seconds is None:
            raise ValueError("interval schedules require interval_seconds")
        if self.type == "cron" and not self.cron:
            raise ValueError("cron schedules require cron")
        if self.type == "external_trigger" and not self.trigger_name:
            raise ValueError("external_trigger schedules require trigger_name")
        return self


class ScenarioTickTarget(BaseModel):
    """Private scenario tick selection for a generic task function."""

    assistant_id: str
    scenario_id: str
    tick_policy: Literal["fixed", "incrementing", "timeline_event"] = "incrementing"
    tick: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _fixed_tick_has_tick(self) -> "ScenarioTickTarget":
        if self.tick_policy == "fixed" and self.tick is None:
            raise ValueError("fixed tick_policy requires tick")
        return self


class TaskActivationSpec(BaseModel):
    """Generic TaskScheduler activation that can run a scenario function."""

    task_id: int | None = Field(default=None, gt=0)
    source_task_log_id: int | None = Field(default=None, gt=0)
    entrypoint_function: str
    task_name: str
    task_description: str
    scheduled_for: str | None = None
    visibility_policy: str = "silent_by_default"
    recurrence_hint: str = "recurring"

    @model_validator(mode="after")
    def _task_activation_has_display_text(self) -> "TaskActivationSpec":
        if not self.task_name.strip():
            raise ValueError("task_name must not be empty")
        if not self.task_description.strip():
            raise ValueError("task_description must not be empty")
        return self


class TaskDeliverySpec(BaseModel):
    """Delivery phase for alerts produced by a scheduled scenario run."""

    phase: Literal["simulated_outbox", "real_delivery"] = "simulated_outbox"
    route: str | None = None


class TaskScheduleSpec(BaseModel):
    """Private scenario schedule compiled to generic task activation."""

    id: str
    enabled: bool = True
    execution_mode: TaskExecutionMode = "offline"
    schedule: ScheduleSpec = Field(default_factory=ScheduleSpec)
    target: ScenarioTickTarget
    activation: TaskActivationSpec
    delivery: TaskDeliverySpec = Field(default_factory=TaskDeliverySpec)


class ScenarioSpec(BaseModel):
    """A portable E2E scenario that binds integration outputs to managers."""

    scenario_id: str
    name: str
    description: str
    client: str
    deployment: str
    integration: IntegrationBinding
    data_targets: list[DataTarget]
    timeline: list[TimelineEvent]
    monitors: list[MonitorRule] = Field(default_factory=list)
    alert_routes: list[AlertRoute] = Field(default_factory=list)
    dashboards: list[DashboardRecipe] = Field(default_factory=list)
    tasks: list[TaskScheduleSpec] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _references_are_valid(self) -> "ScenarioSpec":
        target_tables = {target.table for target in self.data_targets}
        for rule in self.monitors:
            if rule.table not in target_tables:
                raise ValueError(
                    f"monitor '{rule.id}' references unknown table '{rule.table}'",
                )

        routes = {route.id for route in self.alert_routes}
        for rule in self.monitors:
            if rule.route not in routes:
                raise ValueError(
                    f"monitor '{rule.id}' references unknown route '{rule.route}'",
                )

        capabilities = set(self.integration.required_capabilities)
        for event in self.timeline:
            if capabilities and event.capability not in capabilities:
                raise ValueError(
                    f"timeline tick {event.tick} references capability "
                    f"'{event.capability}' not declared in integration binding",
                )

        for task in self.tasks:
            if task.target.scenario_id != self.scenario_id:
                raise ValueError(
                    f"task '{task.id}' targets scenario '{task.target.scenario_id}' "
                    f"but is declared under '{self.scenario_id}'",
                )
            if task.delivery.route and task.delivery.route not in routes:
                raise ValueError(
                    f"task '{task.id}' delivery references unknown route "
                    f"'{task.delivery.route}'",
                )
        return self

    def data_target_for(self, table: str) -> DataTarget:
        """Return the configured materialization target for a table."""
        for target in self.data_targets:
            if target.table == table:
                return target
        raise KeyError(f"No data target configured for table '{table}'")


# ---------------------------------------------------------------------------
# Scenario activation — deployment-side template instantiation
# ---------------------------------------------------------------------------


class ScenarioActivation(BaseModel):
    """Deployment-side activation of a generic scenario template.

    Platform integration packages ship reusable scenario templates under
    ``<package>/scenarios/`` with placeholder fields (empty ``client`` /
    ``deployment``, empty ``tasks[*].target.assistant_id``).  A
    ``ScenarioActivation`` instance, declared on
    :class:`droid_deploy.assistant_deployments.deployment_types.SeedLayer` or
    :class:`droid_deploy.assistant_deployments.deployment_types.DeploymentSpec`,
    instructs the framework to load that template, substitute the
    placeholders with per-client values, validate the result as a
    concrete :class:`ScenarioSpec`, and register it into the resolved
    assistant deployment the offline task lane consumes.

    This eliminates the need for ``client_packages/<client>_<integration>_sync/``
    wrapper packages whose only purpose is to copy a template and fill
    placeholders.
    """

    scenario_template: str = Field(
        ...,
        description=(
            "Identifier of the generic scenario YAML to activate, in the "
            "form '<package_slug>/<scenario_filename_stem>'.  E.g. "
            "'hubspot/crm_full_sync_v0' resolves to "
            "packages/hubspot/scenarios/crm_full_sync_v0.yaml.  Templates "
            "referenced here MUST exist; missing templates raise a "
            "FileNotFoundError at materialisation time."
        ),
    )

    assistant_id: str = Field(
        ...,
        description=(
            "Assistant id this activation binds to.  Substituted into "
            "every tasks[*].target.assistant_id at materialisation time."
        ),
    )

    scenario_id_override: str | None = Field(
        default=None,
        description=(
            "Override the materialised scenario's ``scenario_id`` (and "
            "every tasks[*].target.scenario_id to keep them consistent).  "
            "Required when activating the same template for multiple "
            "assistants/clients on the same control-plane to avoid id "
            "collisions.  When omitted, defaults to "
            "'{client}_{template_stem}'."
        ),
    )

    client_override: str | None = Field(
        default=None,
        description=(
            "Stamp into the materialised scenario's ``client`` field.  "
            "Defaults to the slug of the client whose register_layer / "
            "DeploymentSpec hosts this activation."
        ),
    )

    deployment_override: str | None = Field(
        default=None,
        description=(
            "Stamp into the materialised scenario's ``deployment`` field.  "
            "Defaults to the resolved deployment name."
        ),
    )

    tasks_enabled: bool = Field(
        default=False,
        description=(
            "Initial state of every tasks[*].enabled in the materialised "
            "scenario.  Default false (per the a71a840 convention — "
            "operators flip after control-plane seeding).  Set true to "
            "ship enabled."
        ),
    )

    task_description_override: str | None = Field(
        default=None,
        description=(
            "Substitute into tasks[*].activation.task_description when "
            "the template's value is empty or starts with 'REPLACE_ME'.  "
            "Templates that already include a usable description leave "
            "this unset."
        ),
    )

    object_intervals_override: dict[str, int] | None = Field(
        default=None,
        description=(
            "Per-object cadence overrides.  Materialised as a single env "
            "var '<PACKAGE_SLUG_UPPER>_SYNC_OBJECT_INTERVALS' with value "
            "'key1:int,key2:int,...' and merged into the resolved "
            "secrets bundle so the orchestrator picks it up at runtime."
        ),
    )

    config_overrides: dict[str, str] | None = Field(
        default=None,
        description=(
            "Free-form package-config env-var overrides.  Each entry is "
            "materialised as a Secret with the same name on the resolved "
            "assistant deployment.  Useful for per-deployment policy differences "
            "(retention windows, redaction toggles, etc.) without editing "
            "the platform template."
        ),
    )

    @field_validator("scenario_template")
    @classmethod
    def _validate_template_format(cls, v: str) -> str:
        if "/" not in v:
            raise ValueError(
                "scenario_template must be '<package_slug>/<filename_stem>', "
                f"got {v!r}",
            )
        slug, _, stem = v.partition("/")
        if not slug.strip() or not stem.strip():
            raise ValueError(
                "scenario_template package_slug and stem must both be non-empty, "
                f"got {v!r}",
            )
        if stem.endswith(".yaml") or stem.endswith(".yml"):
            raise ValueError(
                "scenario_template stem must not include a file extension; "
                f"got {v!r}.  Use 'hubspot/crm_full_sync_v0' not "
                "'hubspot/crm_full_sync_v0.yaml'.",
            )
        return v

    @field_validator("assistant_id")
    @classmethod
    def _validate_assistant_id(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("assistant_id must be non-empty")
        return v
